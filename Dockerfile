FROM python:3.14-slim

# Tesseract brings English and orientation data; Turkish comes from tessdata_best, as in the Windows setup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tzdata \
    && rm -rf /var/lib/apt/lists/*
ADD https://github.com/tesseract-ocr/tessdata_best/raw/main/tur.traineddata /usr/share/tesseract-ocr/5/tessdata/

# KAP times are compared with the local clock, so the container runs on Turkish time.
ENV TZ=Europe/Istanbul \
    TESSERACT_CMD=/usr/bin/tesseract \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    PYTHONUNBUFFERED=1
# glibc gives each thread its own heap and keeps what is freed there, so with dozens of worker threads
# memory grows until the Actor runs out of it. Two heaps hand freed memory back.
ENV MALLOC_ARENA_MAX=2

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py companies.txt ./

# Same as Ctrl+C, so `docker stop` lets the scripts write their outputs and close the database.
STOPSIGNAL SIGINT
# Apify runs the image as it is; compose.yaml starts the watcher instead.
CMD ["python", "kap_apify.py"]
