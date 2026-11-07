FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tgclone ./tgclone
VOLUME ["/app/data"]
ENV TGCLONE_CONFIG=/app/data/tgclone.config.json
CMD ["python", "-m", "tgclone", "live"]
