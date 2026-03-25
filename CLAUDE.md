# NotebookLM Research Microservice

## What This Is

A Docker-hosted REST API that wraps Google NotebookLM's capabilities. It runs on a VPS (same Hostinger server as the TTS microservice) and exposes endpoints for creating notebooks, ingesting sources, generating research artifacts (podcasts, infographics, slide decks, reports), and searching YouTube.

## Architecture

```
Portfolio Site (Vercel)
    ↓ POST /notebook, /ask, /audio, etc.
NLM Microservice (Hostinger VPS / Docker / Traefik)
    ↓ Uses notebooklm-py (unofficial Python library)
Google NotebookLM (processes sources, generates content)
```

## Key Files

- `server.py` - FastAPI service with all endpoints
- `sync_auth.py` - Script to sync local Google auth cookies to the VPS
- `docker-compose.yml` - Docker config with Traefik labels for `nlm.imadefire.com`
- `Dockerfile` - Python 3.12 slim with ffmpeg for yt-dlp
- `requirements.txt` - Python dependencies

## Auth Model

NotebookLM has no official API. The `notebooklm-py` library uses Google browser cookies which expire every 2-4 weeks. When they expire:

1. Run locally: `python sync_auth.py --relogin`
2. This opens a Chrome window for Google sign-in
3. After login, it syncs the cookies to the VPS container via SCP

To just check if auth is still valid: `python sync_auth.py --check`

## API Endpoints

All endpoints require the `x-nlm-secret` header if `NLM_API_SECRET` is set.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Auth status and service health |
| GET | /notebooks | List all notebooks |
| POST | /notebook | Create notebook + ingest source URLs/text |
| GET | /notebook/{id} | Get notebook details and sources |
| POST | /notebook/{id}/ask | Ask a question (RAG query) |
| POST | /notebook/{id}/audio | Generate podcast audio (returns job_id) |
| POST | /notebook/{id}/infographic | Generate infographic PNG (returns job_id) |
| POST | /notebook/{id}/slides | Generate slide deck PDF (returns job_id) |
| POST | /notebook/{id}/report | Generate written report (sync) |
| GET | /notebook/{id}/status/{job_id} | Poll async job status |
| GET | /download/{filename} | Download generated file |
| POST | /youtube/search | Search YouTube via yt-dlp |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| PORT | 3200 | Server port |
| NLM_API_SECRET | (empty) | Shared secret for auth header |
| NLM_STORAGE_PATH | /data/storage_state.json | Path to Google auth cookies |
| NLM_OUTPUT_DIR | /data/output | Directory for generated files |

## Deployment

### First time setup on VPS:

```bash
# On VPS
cd /root
git clone <this-repo> nlm-service
cd nlm-service

# Set the API secret
echo "NLM_API_SECRET=your-secret-here" > .env

# Build and start
docker compose up -d --build

# Check logs
docker compose logs -f
```

### Sync auth cookies (run from local machine):

```bash
# First time or when auth expires
python sync_auth.py --relogin --host YOUR_VPS_IP

# Just sync (if local auth is still valid)
python sync_auth.py --host YOUR_VPS_IP
```

### DNS Setup:

Point `nlm.imadefire.com` to your VPS IP address. Traefik handles SSL via Let's Encrypt automatically.

## Local Development

```bash
pip install -r requirements.txt
python server.py
# Runs on http://localhost:3200
```

## Commands for AI Agents

```bash
# Check service health
curl https://nlm.imadefire.com/health -H "x-nlm-secret: YOUR_SECRET"

# Create a notebook with YouTube sources
curl -X POST https://nlm.imadefire.com/notebook \
  -H "Content-Type: application/json" \
  -H "x-nlm-secret: YOUR_SECRET" \
  -d '{"title":"AI Research","sources":["https://youtube.com/watch?v=..."],"source_type":"url"}'

# Ask a question
curl -X POST https://nlm.imadefire.com/notebook/NOTEBOOK_ID/ask \
  -H "Content-Type: application/json" \
  -H "x-nlm-secret: YOUR_SECRET" \
  -d '{"question":"What are the top 3 trends discussed?"}'

# Generate a podcast
curl -X POST https://nlm.imadefire.com/notebook/NOTEBOOK_ID/audio \
  -H "Content-Type: application/json" \
  -H "x-nlm-secret: YOUR_SECRET" \
  -d '{"format":"deep-dive","length":"medium"}'
# Returns: {"job_id":"abc123","poll_url":"/notebook/.../status/abc123"}

# Poll for completion
curl https://nlm.imadefire.com/notebook/NOTEBOOK_ID/status/abc123 \
  -H "x-nlm-secret: YOUR_SECRET"
# Returns: {"status":"complete","download_url":"/download/abc123.mp3"}

# Download the file
curl -O https://nlm.imadefire.com/download/abc123.mp3 \
  -H "x-nlm-secret: YOUR_SECRET"

# Search YouTube
curl -X POST https://nlm.imadefire.com/youtube/search \
  -H "Content-Type: application/json" \
  -H "x-nlm-secret: YOUR_SECRET" \
  -d '{"query":"agentic AI 2026","count":10,"months":3}'
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 503 "auth expired" | Run `python sync_auth.py --relogin --host VPS_IP` from local machine |
| 401 on endpoints | Check `x-nlm-secret` header matches `NLM_API_SECRET` env var |
| Audio/infographic job stuck | Check container logs: `docker compose logs nlm` |
| yt-dlp errors | Update: `docker exec nlm-service pip install --upgrade yt-dlp` |
| Container won't start | Check Traefik network: `docker network ls | grep root_default` |
