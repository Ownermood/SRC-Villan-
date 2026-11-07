"""Background jobs (history clone / link save) with live progress and cancel."""

import asyncio
import html
import itertools
import logging
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("tgclone")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    full = round(frac * width)
    return "▰" * full + "▱" * (width - full)


def human_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


current_job: ContextVar[Optional["Job"]] = ContextVar("current_job", default=None)


def stage(text: str) -> None:
    """Show what is happening right now on the running job's card (no-op outside a job)."""
    job = current_job.get()
    if job is not None:
        job.stage = text


def note(text: str) -> None:
    job = current_job.get()
    if job is not None:
        job.note = text


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, title: str, key: str, start_id: int = 0, end_id: int = 0) -> None:
        self.id = next(Job._ids)
        self.kind = kind  # "clone" | "save"
        self.title = title
        self.key = key  # same key cannot run twice (prevents duplicate sends)
        self.start_id = start_id  # progress = position of last_id inside (start_id, end_id]
        self.end_id = end_id  # 0 = unknown
        self.done = 0
        self.failed = 0
        self.last_id = 0
        self.started = time.time()
        self.cancelled = False
        self.state = "running"  # running | done | failed | stopped
        self.error = ""
        self.file = ""  # current file transfer line
        self.stage = ""  # what the bot is doing right now (shown on the card)
        self.note = ""  # last noteworthy event (retry, caption split, wait...)
        self.failed_ids: List[int] = []  # source posts that could not be sent
        self.task: Optional[asyncio.Task] = None
        self._finished = False
        self._file_t = 0.0
        self._xfer = None
        self._samples = []

    # called by send() during download/upload
    def file_progress(self, direction: str, cur: int, total: int) -> None:
        now = time.time()
        key = (direction, total)
        if key != self._xfer or not self._samples or cur < self._samples[-1][1]:
            self._xfer = key  # a new download / upload started: restart the clock
            self._samples = []
        self._samples.append((now, cur))
        self._samples = [s for s in self._samples if now - s[0] <= 10] or self._samples[-2:]
        if now - self._file_t < 1 and cur < total:
            return
        self._file_t = now
        t0, c0 = self._samples[0]
        speed = (cur - c0) / (now - t0) if now - t0 > 0.2 else 0
        icon = "⬇️" if direction == "down" else "⬆️"
        eta = f" · {human_time((total - cur) / speed)} left" if speed > 1024 and total else ""
        self.file = (f"{icon} {bar(cur / total if total else 0, 10)} "
                     f"{human_size(cur)} / {human_size(total)} · "
                     f"{human_size(speed) + '/s' if speed else 'measuring…'}{eta}")
        if cur >= total:
            self.file = ""

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def render(self) -> str:
        icon = {"running": "⏳", "done": "✅", "failed": "❌", "stopped": "⛔"}[self.state]
        rate = self.done / self.elapsed if self.elapsed > 0 else 0
        lines = [f"{icon} <b>#{self.id} {self.kind}</b> · {self.title}"]
        if self.end_id > self.start_id:
            frac = 1.0 if self.state == "done" else \
                (self.last_id - self.start_id) / (self.end_id - self.start_id)
            lines.append(f"{bar(frac)} {max(0.0, min(frac, 1.0)) * 100:.0f}%"
                         f"  · id {self.last_id or self.start_id}/{self.end_id}")
        lines.append(f"📨 {self.done} done · ⚠️ {self.failed} failed")
        extra = f" · {rate:.1f}/s" if self.state == "running" and rate else ""
        lines.append(f"⏱ {human_time(self.elapsed)}{extra}")
        if self.state == "running":
            if self.stage:
                lines.append(f"🔎 {html.escape(self.stage)}")
            if self.file:
                lines.append(self.file)
        if self.note:
            lines.append(f"ℹ️ {html.escape(self.note[:150])}")
        if self.failed_ids and self.state != "running":
            ids = ", ".join(map(str, self.failed_ids[:15])) + (" …" if len(self.failed_ids) > 15 else "")
            lines.append(f"⚠️ Not sent: {ids}")
        if self.error:
            lines.append(f"❗ {html.escape(self.error[:200])}")
        return "\n".join(lines)


class JobManager:
    def __init__(self, max_jobs: int = 3) -> None:
        self.max_jobs = max_jobs
        self.jobs: Dict[int, Job] = {}

    def running(self):
        return [j for j in self.jobs.values() if j.state == "running"]

    def check(self, job: Job) -> None:
        if any(j.key == job.key for j in self.running()):
            raise ValueError("This clone/save is already running.")
        if len(self.running()) >= self.max_jobs:
            raise ValueError(f"Max {self.max_jobs} jobs running. Stop one first.")

    def start(self, job: Job, fn: Callable[[Job], Awaitable[None]],
              on_finish: Optional[Callable[[Job], Awaitable[None]]] = None) -> Job:
        self.check(job)

        async def runner():
            current_job.set(job)  # stage()/note() anywhere in this job update its card
            try:
                await fn(job)
                job.state = "stopped" if job.cancelled else "done"
            except asyncio.CancelledError:
                job.state = "stopped"
            except Exception as e:
                log.exception("Job #%s failed", job.id)
                job.state, job.error = "failed", str(e)
            await finish()

        async def finish():
            if job._finished:
                return
            job._finished = True
            job.file = ""
            if on_finish:
                try:
                    await on_finish(job)
                except Exception:
                    log.exception("on_finish failed")
            self._trim()

        def on_done(task: asyncio.Task) -> None:
            # cancelled before it ever ran: runner() never executed
            if not job._finished:
                if job.state == "running":
                    job.state = "stopped"
                asyncio.ensure_future(finish())

        self.jobs[job.id] = job
        job.task = asyncio.create_task(runner())
        job.task.add_done_callback(on_done)
        return job

    def stop(self, job_id: Optional[int] = None) -> int:
        """Stop one job (or all running). Returns how many were stopped."""
        targets = [self.jobs[job_id]] if job_id in self.jobs else ([] if job_id else self.running())
        n = 0
        for j in targets:
            if j.state == "running":
                j.cancelled = True
                if j.task:
                    j.task.cancel()
                n += 1
        return n

    def _trim(self, keep: int = 20) -> None:
        finished = [j for j in self.jobs.values() if j.state != "running"]
        for j in finished[:-keep]:
            self.jobs.pop(j.id, None)
