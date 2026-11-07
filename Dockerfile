FROM python:3.12-slim
# ffmpeg -> watermark plugin · tesseract (+ Hindi) -> OCR plugin
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tgclone ./tgclone
VOLUME ["/app/data"]
ENV TGCLONE_CONFIG=/app/data/tgclone.config.json
CMD ["python", "-m", "tgclone", "live"]
