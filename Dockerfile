FROM python:3.12-slim

WORKDIR /app

# System dependencies for yt-dlp
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY sync_auth.py .

# Data volume for auth state and generated files
RUN mkdir -p /data/output
VOLUME /data

ENV PORT=3200
ENV NLM_STORAGE_PATH=/data/storage_state.json
ENV NLM_OUTPUT_DIR=/data/output

EXPOSE 3200

CMD ["python", "server.py"]
