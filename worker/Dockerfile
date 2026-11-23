FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# fonts-noto-core carries Noto Sans Tamil, which the Tamil channels need -
# DejaVu has no Tamil glyphs and would render every Tamil caption as boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates \
      fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

CMD ["python", "-m", "app.main"]
