"""
NotebookLM Research Microservice

A FastAPI service that wraps the unofficial notebooklm-py library,
exposing Google NotebookLM's capabilities as a REST API.

Endpoints:
  POST /notebook              - Create notebook + ingest sources
  POST /notebook/{id}/ask     - Ask a question against notebook sources
  POST /notebook/{id}/audio   - Generate podcast-style audio overview
  POST /notebook/{id}/infographic - Generate infographic
  POST /notebook/{id}/slides  - Generate slide deck
  POST /notebook/{id}/report  - Generate written report
  POST /youtube/search        - Search YouTube via yt-dlp
  GET  /notebooks             - List all notebooks
  GET  /notebook/{id}         - Get notebook details + sources
  GET  /notebook/{id}/status/{job_id} - Poll job status
  GET  /health                - Health check

Auth: Uses Google browser cookies stored in storage_state.json.
      Refresh with: python sync_auth.py (run locally, SCP to VPS)
"""

import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

API_SECRET = os.environ.get("NLM_API_SECRET", "")
STORAGE_PATH = Path(os.environ.get("NLM_STORAGE_PATH", "/data/storage_state.json"))
OUTPUT_DIR = Path(os.environ.get("NLM_OUTPUT_DIR", "/data/output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="NotebookLM Research Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory job tracking ────────────────────────────────────────────────────

jobs: dict[str, dict] = {}

# ── Auth helper ───────────────────────────────────────────────────────────────

def check_secret(x_nlm_secret: Optional[str] = Header(None)):
    if API_SECRET and x_nlm_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid API secret")

async def get_client():
    """Create an authenticated NotebookLM client."""
    try:
        from notebooklm.client import NotebookLMClient
        client = await NotebookLMClient.from_storage(STORAGE_PATH)
        return client
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"NotebookLM auth expired or invalid. Run sync_auth.py to refresh. Error: {str(e)}"
        )

# ── Models ────────────────────────────────────────────────────────────────────

class CreateNotebookRequest(BaseModel):
    title: str
    sources: list[str] = []  # URLs (web pages, YouTube) or text content
    source_type: str = "url"  # "url" or "text"

class AskRequest(BaseModel):
    question: str

class AudioRequest(BaseModel):
    format: str = "deep-dive"  # deep-dive, brief, critique, debate
    length: str = "medium"     # short, medium, long

class InfographicRequest(BaseModel):
    orientation: str = "landscape"  # landscape, portrait, square
    detail: str = "medium"         # low, medium, high

class SlidesRequest(BaseModel):
    format: str = "detailed"  # detailed, presenter
    length: str = "medium"    # short, medium, long

class ReportRequest(BaseModel):
    format: str = "blog"  # blog, briefing, study_guide, custom
    custom_prompt: Optional[str] = None

class YouTubeSearchRequest(BaseModel):
    query: str
    count: int = 10
    months: int = 6

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    auth_exists = STORAGE_PATH.exists()
    auth_valid = False
    if auth_exists:
        try:
            async with await get_client() as client:
                notebooks = await client.notebooks.list()
                auth_valid = True
        except Exception:
            pass
    return {
        "status": "ok" if auth_valid else "degraded",
        "auth_file_exists": auth_exists,
        "auth_valid": auth_valid,
        "output_dir": str(OUTPUT_DIR),
    }

# ── Notebooks CRUD ────────────────────────────────────────────────────────────

@app.get("/notebooks")
async def list_notebooks(x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    async with await get_client() as client:
        notebooks = await client.notebooks.list()
        return {
            "notebooks": [
                {
                    "id": nb.id if hasattr(nb, 'id') else str(i),
                    "title": nb.title,
                    "sources_count": nb.sources_count,
                    "created_at": nb.created_at.isoformat() if nb.created_at else None,
                }
                for i, nb in enumerate(notebooks)
            ]
        }

@app.post("/notebook")
async def create_notebook(req: CreateNotebookRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    async with await get_client() as client:
        # Create notebook
        notebook = await client.notebooks.create(req.title)

        # Add sources
        added_sources = []
        for source in req.sources:
            try:
                if req.source_type == "url":
                    result = await client.sources.add_url(notebook, source)
                else:
                    result = await client.sources.add_text(notebook, source, title=f"Source {len(added_sources)+1}")
                added_sources.append({"source": source, "status": "added"})
            except Exception as e:
                added_sources.append({"source": source, "status": "failed", "error": str(e)})

        return {
            "notebook_id": notebook.id if hasattr(notebook, 'id') else notebook.title,
            "title": notebook.title,
            "sources": added_sources,
        }

@app.get("/notebook/{notebook_id}")
async def get_notebook(notebook_id: str, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    async with await get_client() as client:
        notebooks = await client.notebooks.list()
        notebook = _find_notebook(notebooks, notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        sources = await client.sources.list(notebook)
        return {
            "title": notebook.title,
            "sources_count": notebook.sources_count,
            "sources": [
                {
                    "title": s.title if hasattr(s, 'title') else "Unknown",
                    "type": str(s.type) if hasattr(s, 'type') else "unknown",
                    "status": str(s.status) if hasattr(s, 'status') else "unknown",
                }
                for s in sources
            ],
        }

# ── Ask / Research ────────────────────────────────────────────────────────────

@app.post("/notebook/{notebook_id}/ask")
async def ask_notebook(notebook_id: str, req: AskRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    async with await get_client() as client:
        notebooks = await client.notebooks.list()
        notebook = _find_notebook(notebooks, notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        result = await client.chat.send_message(notebook, req.question)
        return {
            "answer": result.text if hasattr(result, 'text') else str(result),
            "references": result.references if hasattr(result, 'references') else [],
        }

# ── Artifact Generation (async jobs) ─────────────────────────────────────────

@app.post("/notebook/{notebook_id}/audio")
async def generate_audio(notebook_id: str, req: AudioRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "processing", "type": "audio", "created_at": datetime.utcnow().isoformat()}
    asyncio.create_task(_generate_audio(notebook_id, req, job_id))
    return {"job_id": job_id, "status": "processing", "poll_url": f"/notebook/{notebook_id}/status/{job_id}"}

async def _generate_audio(notebook_id: str, req: AudioRequest, job_id: str):
    try:
        async with await get_client() as client:
            notebooks = await client.notebooks.list()
            notebook = _find_notebook(notebooks, notebook_id)
            if not notebook:
                jobs[job_id] = {"status": "failed", "error": "Notebook not found"}
                return

            from notebooklm.types import AudioFormat, AudioLength
            format_map = {"deep-dive": AudioFormat.DEEP_DIVE, "brief": AudioFormat.BRIEFING, "critique": AudioFormat.CRITIQUE, "debate": AudioFormat.DEBATE}
            length_map = {"short": AudioLength.SHORT, "medium": AudioLength.MEDIUM, "long": AudioLength.LONG}

            audio = await client.artifacts.generate_audio(
                notebook,
                format=format_map.get(req.format, AudioFormat.DEEP_DIVE),
                length=length_map.get(req.length, AudioLength.MEDIUM),
            )

            output_path = OUTPUT_DIR / f"{job_id}.mp3"
            await client.artifacts.download(audio, output_path)

            jobs[job_id] = {"status": "complete", "file": str(output_path), "download_url": f"/download/{job_id}.mp3"}
    except Exception as e:
        jobs[job_id] = {"status": "failed", "error": str(e)}

@app.post("/notebook/{notebook_id}/infographic")
async def generate_infographic(notebook_id: str, req: InfographicRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "processing", "type": "infographic", "created_at": datetime.utcnow().isoformat()}
    asyncio.create_task(_generate_infographic(notebook_id, req, job_id))
    return {"job_id": job_id, "status": "processing", "poll_url": f"/notebook/{notebook_id}/status/{job_id}"}

async def _generate_infographic(notebook_id: str, req: InfographicRequest, job_id: str):
    try:
        async with await get_client() as client:
            notebooks = await client.notebooks.list()
            notebook = _find_notebook(notebooks, notebook_id)
            if not notebook:
                jobs[job_id] = {"status": "failed", "error": "Notebook not found"}
                return

            from notebooklm.types import InfographicOrientation, InfographicDetail
            orient_map = {"landscape": InfographicOrientation.LANDSCAPE, "portrait": InfographicOrientation.PORTRAIT, "square": InfographicOrientation.SQUARE}
            detail_map = {"low": InfographicDetail.LOW, "medium": InfographicDetail.MEDIUM, "high": InfographicDetail.HIGH}

            infographic = await client.artifacts.generate_infographic(
                notebook,
                orientation=orient_map.get(req.orientation, InfographicOrientation.LANDSCAPE),
                detail=detail_map.get(req.detail, InfographicDetail.MEDIUM),
            )

            output_path = OUTPUT_DIR / f"{job_id}.png"
            await client.artifacts.download(infographic, output_path)

            jobs[job_id] = {"status": "complete", "file": str(output_path), "download_url": f"/download/{job_id}.png"}
    except Exception as e:
        jobs[job_id] = {"status": "failed", "error": str(e)}

@app.post("/notebook/{notebook_id}/slides")
async def generate_slides(notebook_id: str, req: SlidesRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "processing", "type": "slides", "created_at": datetime.utcnow().isoformat()}
    asyncio.create_task(_generate_slides(notebook_id, req, job_id))
    return {"job_id": job_id, "status": "processing", "poll_url": f"/notebook/{notebook_id}/status/{job_id}"}

async def _generate_slides(notebook_id: str, req: SlidesRequest, job_id: str):
    try:
        async with await get_client() as client:
            notebooks = await client.notebooks.list()
            notebook = _find_notebook(notebooks, notebook_id)
            if not notebook:
                jobs[job_id] = {"status": "failed", "error": "Notebook not found"}
                return

            from notebooklm.types import SlideDeckFormat, SlideDeckLength
            format_map = {"detailed": SlideDeckFormat.DETAILED, "presenter": SlideDeckFormat.PRESENTER}
            length_map = {"short": SlideDeckLength.SHORT, "medium": SlideDeckLength.MEDIUM, "long": SlideDeckLength.LONG}

            slides = await client.artifacts.generate_slides(
                notebook,
                format=format_map.get(req.format, SlideDeckFormat.DETAILED),
                length=length_map.get(req.length, SlideDeckLength.MEDIUM),
            )

            output_path = OUTPUT_DIR / f"{job_id}.pdf"
            await client.artifacts.download(slides, output_path)

            jobs[job_id] = {"status": "complete", "file": str(output_path), "download_url": f"/download/{job_id}.pdf"}
    except Exception as e:
        jobs[job_id] = {"status": "failed", "error": str(e)}

@app.post("/notebook/{notebook_id}/report")
async def generate_report(notebook_id: str, req: ReportRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    async with await get_client() as client:
        notebooks = await client.notebooks.list()
        notebook = _find_notebook(notebooks, notebook_id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")

        from notebooklm.types import ReportFormat
        format_map = {"blog": ReportFormat.BLOG_POST, "briefing": ReportFormat.BRIEFING_DOC, "study_guide": ReportFormat.STUDY_GUIDE}

        if req.format == "custom" and req.custom_prompt:
            report = await client.artifacts.generate_report(notebook, custom_prompt=req.custom_prompt)
        else:
            report = await client.artifacts.generate_report(
                notebook,
                format=format_map.get(req.format, ReportFormat.BLOG_POST),
            )

        return {"report": report.text if hasattr(report, 'text') else str(report)}

# ── YouTube Search ────────────────────────────────────────────────────────────

@app.post("/youtube/search")
async def youtube_search(req: YouTubeSearchRequest, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    try:
        cmd = [
            "yt-dlp",
            f"ytsearch{req.count}:{req.query}",
            "--dump-json",
            "--flat-playlist",
            "--no-download",
            "--dateafter", f"today-{req.months * 30}days",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"yt-dlp error: {result.stderr[:200]}")

        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                videos.append({
                    "title": data.get("title"),
                    "channel": data.get("channel") or data.get("uploader"),
                    "views": data.get("view_count"),
                    "duration": data.get("duration"),
                    "upload_date": data.get("upload_date"),
                    "url": f"https://www.youtube.com/watch?v={data.get('id')}" if data.get("id") else data.get("url"),
                })
            except json.JSONDecodeError:
                continue

        return {"query": req.query, "count": len(videos), "videos": videos}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="YouTube search timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="yt-dlp not installed")

# ── Job Status + File Download ────────────────────────────────────────────────

@app.get("/notebook/{notebook_id}/status/{job_id}")
async def job_status(notebook_id: str, job_id: str, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/download/{filename}")
async def download_file(filename: str, x_nlm_secret: Optional[str] = Header(None)):
    check_secret(x_nlm_secret)
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_notebook(notebooks, notebook_id: str):
    """Find a notebook by ID or title (case-insensitive partial match)."""
    for nb in notebooks:
        if hasattr(nb, 'id') and str(nb.id) == notebook_id:
            return nb
    for nb in notebooks:
        if notebook_id.lower() in nb.title.lower():
            return nb
    return None

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3200))
    uvicorn.run(app, host="0.0.0.0", port=port)
