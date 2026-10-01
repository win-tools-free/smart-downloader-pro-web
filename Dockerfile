FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# ------------------------------------------------------------
# System packages
# ------------------------------------------------------------

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*


# ------------------------------------------------------------
# Deno JavaScript runtime
# ------------------------------------------------------------

RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno


# ------------------------------------------------------------
# App directory
# ------------------------------------------------------------

WORKDIR /app


# ------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------

COPY requirements.txt .

RUN pip install --no-cache-dir \
    "yt-dlp[default]" \
    yt-dlp-ejs \
    fastapi \
    "uvicorn[standard]" \
    pydantic


# ------------------------------------------------------------
# Application
# ------------------------------------------------------------

COPY app.py .
COPY static ./static


# ------------------------------------------------------------
# Download directory
# ------------------------------------------------------------

RUN mkdir -p /app/server_downloads


# ------------------------------------------------------------
# Verify installation during Docker build
# ------------------------------------------------------------

RUN echo "===== DENO =====" \
    && deno --version \
    && echo "===== FFMPEG =====" \
    && ffmpeg -version \
    && echo "===== FFPROBE =====" \
    && ffprobe -version \
    && echo "===== YT-DLP =====" \
    && python -c "import yt_dlp; print('yt-dlp:', yt_dlp.version.__version__)" \
    && echo "===== YT-DLP-EJS =====" \
    && python -c "import yt_dlp_ejs; print('yt-dlp-ejs: OK')"


# ------------------------------------------------------------
# Render port
# ------------------------------------------------------------

EXPOSE 10000


# ------------------------------------------------------------
# Start FastAPI
# ------------------------------------------------------------

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
