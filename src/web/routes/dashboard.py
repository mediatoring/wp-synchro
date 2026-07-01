"""Dashboard route — connection tests, overview."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..app import templates, _get_engines
from ...config import get_config

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/test-connection", response_class=JSONResponse)
async def test_connections():
    """Test SSH connections to both servers. Called from dashboard via JS."""
    e = _get_engines()
    old_ok, old_msg = e["old"].test_connection()
    new_ok, new_msg = e["new"].test_connection()

    # Also check wp-cli availability on both
    if old_ok:
        wp_old = e["old"].wp("--version")
        old_wp_ver = wp_old.stdout.strip().split("\n")[0] if wp_old.ok else f"UNAVAILABLE: {wp_old.stderr[:80]}"
    else:
        old_wp_ver = "N/A (SSH failed)"

    if new_ok:
        wp_new = e["new"].wp("--version")
        new_wp_ver = wp_new.stdout.strip().split("\n")[0] if wp_new.ok else f"UNAVAILABLE: {wp_new.stderr[:80]}"
    else:
        new_wp_ver = "N/A (SSH failed)"

    return {
        "old": {
            "ssh": old_ok,
            "ssh_msg": old_msg,
            "wp_version": old_wp_ver,
            "host": e["cfg"].old_server.ssh_host,
        },
        "new": {
            "ssh": new_ok,
            "ssh_msg": new_msg,
            "wp_version": new_wp_ver,
            "host": e["cfg"].new_server.ssh_host,
        },
    }


@router.get("/recent-jobs", response_class=JSONResponse)
async def recent_jobs():
    e = _get_engines()
    jobs = e["db"].list_jobs(limit=20)
    return [
        {
            "id": j.id,
            "job_type": j.job_type,
            "mode": j.mode,
            "status": j.status,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "summary": j.summary,
            "error_msg": j.error_msg,
        }
        for j in jobs
    ]
