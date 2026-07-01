"""Log viewer routes."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..app import templates, _get_engines

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "active": "logs",
    })


@router.get("/recent", response_class=JSONResponse)
async def recent_logs():
    e = _get_engines()
    return e["db"].get_recent_logs(limit=200)


@router.get("/job/{job_id}", response_class=JSONResponse)
async def job_logs(job_id: int):
    e = _get_engines()
    job = e["db"].get_job(job_id)
    logs = e["db"].get_job_logs(job_id, limit=1000)
    return {
        "job": {
            "id": job.id,
            "job_type": job.job_type,
            "mode": job.mode,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "summary": job.summary,
            "error_msg": job.error_msg,
        } if job else None,
        "logs": logs,
    }


@router.get("/download", response_class=PlainTextResponse)
async def download_logs():
    """Download all recent logs as plain text."""
    e = _get_engines()
    logs = e["db"].get_recent_logs(limit=5000)
    import datetime
    lines = []
    for entry in logs:
        ts = datetime.datetime.fromtimestamp(entry["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} [{entry['level']}] [{entry['job_type']}/{entry['mode']}] {entry['message']}")
    return "\n".join(lines)
