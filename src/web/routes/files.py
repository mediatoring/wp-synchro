"""Motor A (file sync) routes."""
from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse

from ..app import templates, _get_engines, _run_sync, submit_background
from ...config import get_config

router = APIRouter(prefix="/files", tags=["files"])


@router.get("", response_class=HTMLResponse)
async def files_page(request: Request):
    cfg = get_config()
    return templates.TemplateResponse(request, "files.html", {
        "cfg": cfg,
        "sync_dirs": cfg.sync_dirs,
        "active": "files",
    })


@router.post("/compute-delta", response_class=JSONResponse)
async def compute_delta(include_deletes: bool = Form(False)):
    """Compute file delta across all sync dirs (dry-run, read-only)."""
    e = _get_engines()

    def _compute():
        deltas = e["motor_a"].compute_all_deltas(include_deletes=include_deletes)
        return [
            {
                "source": d.sync_dir_source,
                "dest": d.sync_dir_dest,
                "new_count": len(d.new_files),
                "modified_count": len(d.modified_files),
                "deleted_count": len(d.deleted_files),
                "new_files": [{"path": f.relative_path, "size": f.size_bytes} for f in d.new_files[:100]],
                "modified_files": [{"path": f.relative_path, "size": f.size_bytes} for f in d.modified_files[:100]],
                "deleted_files": [{"path": f.relative_path} for f in d.deleted_files[:100]],
            }
            for d in deltas
        ]

    return await _run_sync(_compute)


@router.post("/sync", response_class=JSONResponse)
async def sync_files(dry_run: bool = Form(True)):
    """
    Start file sync in background. Returns job_id immediately.
    Poll /files/job-status/{job_id} for live progress.
    """
    e = _get_engines()
    job_id = e["db"].create_job("motor_a", "dry_run" if dry_run else "sync")

    def _sync_bg():
        try:
            results = e["motor_a"].sync_all_files(job_id, dry_run=dry_run)
            summary = {
                "dry_run": dry_run,
                "dirs": results,
                "total_uploaded_new": sum(r.get("uploaded_new", 0) for r in results if "error" not in r),
                "total_uploaded_modified": sum(r.get("uploaded_modified", 0) for r in results if "error" not in r),
            }
            e["db"].finish_job(job_id, summary)
        except Exception as ex:
            e["db"].finish_job(job_id, {}, error=str(ex))

    submit_background(_sync_bg)
    return {"job_id": job_id, "status": "running"}


@router.get("/job-status/{job_id}", response_class=JSONResponse)
async def job_status(job_id: int):
    """Poll job status + recent log lines for live progress display."""
    e = _get_engines()
    job = e["db"].get_job(job_id)
    logs = e["db"].get_job_logs(job_id, limit=50)
    return {
        "job_id": job_id,
        "status": job.status if job else "unknown",
        "summary": job.summary if job else None,
        "error_msg": job.error_msg if job else None,
        "logs": logs,
    }


@router.post("/delete-mirror", response_class=JSONResponse)
async def delete_mirror(sync_dir_index: int = Form(0), backup_confirmed: bool = Form(False)):
    """
    Perform mirror deletion on new server.
    REQUIRES: backup must have been created first (backup_confirmed=True).
    """
    if not backup_confirmed:
        return JSONResponse(
            {"error": "Backup must be confirmed before mirror delete. Create a DB backup first."},
            status_code=400,
        )

    e = _get_engines()

    def _delete():
        job_id = e["db"].create_job("motor_a", "delete")
        recent_jobs = e["db"].list_jobs(limit=10)
        has_backup = any(
            j.job_type == "backup" and j.status == "done"
            for j in recent_jobs
        )
        if not has_backup:
            e["db"].finish_job(job_id, {}, error="No recent backup found — aborting mirror delete")
            return {"error": "No recent successful backup found. Run DB backup first.", "job_id": job_id}

        result = e["motor_a"].delete_mirror(
            sync_dir_index=sync_dir_index,
            job_id=job_id,
            backup_path="confirmed",
        )
        e["db"].finish_job(job_id, result)
        return {"job_id": job_id, **result}

    return await _run_sync(_delete)
