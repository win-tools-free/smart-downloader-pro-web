
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# FFmpeg + tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime
RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir \
    "yt-dlp[default]" \
    fastapi \
    "uvicorn[standard]" \
    pydantic

COPY app.py .
COPY static ./static

RUN mkdir -p /app/server_downloads

# Verify runtime during Docker build
RUN deno --version \
    && ffmpeg -version \
    && python -c "import yt_dlp; print('yt-dlp:', yt_dlp.version.__version__)" \
    && python -c "import yt_dlp_ejs; print('yt-dlp-ejs: OK')"

EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

