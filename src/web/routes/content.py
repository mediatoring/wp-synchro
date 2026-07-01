"""Motor B (database content sync) routes."""
from __future__ import annotations

import json
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse

from ..app import templates, _get_engines, _run_sync, submit_background
from ...config import get_config

router = APIRouter(prefix="/content", tags=["content"])


@router.get("", response_class=HTMLResponse)
async def content_page(request: Request):
    cfg = get_config()
    return templates.TemplateResponse(request, "content.html", {
        "cfg": cfg,
        "post_types": [],   # loaded async via /content/post-types
        "excluded_types": cfg.excluded_post_types,
        "active": "content",
    })


@router.get("/post-types", response_class=JSONResponse)
async def get_post_types():
    e = _get_engines()
    def _get():
        try:
            return e["motor_b"].get_active_post_types()
        except Exception:
            return []
    return await _run_sync(_get)


@router.post("/compute-delta", response_class=JSONResponse)
async def compute_delta(post_types_json: str = Form("[]")):
    """
    Compute content delta for the selected post types.
    Returns counts + lists of new/modified/deleted posts.
    """
    e = _get_engines()
    try:
        selected_types = json.loads(post_types_json)
    except (json.JSONDecodeError, ValueError):
        selected_types = []

    def _compute():
        if not selected_types:
            types = e["motor_b"].get_active_post_types()
        else:
            types = selected_types

        deltas = e["motor_b"].compute_all_deltas(types)
        result = []
        for d in deltas:
            result.append({
                "post_type": d.post_type,
                "new_count": len(d.new_posts),
                "modified_count": len(d.modified_posts),
                "deleted_count": len(d.deleted_posts),
                "new_posts": [_diff_to_dict(p) for p in d.new_posts[:200]],
                "modified_posts": [_diff_to_dict(p) for p in d.modified_posts[:200]],
                "deleted_posts": [_diff_to_dict(p) for p in d.deleted_posts[:200]],
            })
        return result

    return await _run_sync(_compute)


@router.post("/sync", response_class=JSONResponse)
async def sync_content(
    post_types_json: str = Form("[]"),
    include_new: bool = Form(True),
    include_modified: bool = Form(True),
    dry_run: bool = Form(True),
):
    """
    Start content sync in background. Returns job_id immediately.
    Poll /content/job-status/{job_id} for live progress.
    """
    e = _get_engines()
    try:
        selected_types = json.loads(post_types_json)
    except (json.JSONDecodeError, ValueError):
        selected_types = []

    job_id = e["db"].create_job("motor_b", "dry_run" if dry_run else "sync")

    def _sync_bg():
        try:
            types = selected_types or e["motor_b"].get_active_post_types()
            deltas = e["motor_b"].compute_all_deltas(types)
            total_ok = 0
            total_err = 0
            per_type = []
            for delta in deltas:
                r = e["motor_b"].sync_delta(
                    delta, job_id, dry_run=dry_run,
                    include_new=include_new, include_modified=include_modified,
                )
                total_ok += r.get("ok", 0)
                total_err += r.get("error", 0)
                per_type.append({"post_type": delta.post_type, **r})
            summary = {"dry_run": dry_run, "total_ok": total_ok, "total_error": total_err, "per_type": per_type}
            e["db"].finish_job(job_id, summary, error="" if total_err == 0 else f"{total_err} errors")
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


@router.post("/backup-db", response_class=JSONResponse)
async def backup_db():
    """Create a DB backup on the new server before any destructive operation."""
    e = _get_engines()
    cfg = get_config()

    def _backup():
        import time
        backup_dir = f"{e['cfg'].new_server.wp_root}/../backups"
        ok, path_or_err = e["new"].backup_database(backup_dir)
        job_id = e["db"].create_job("backup", "backup")
        if ok:
            e["db"].finish_job(job_id, {"backup_path": path_or_err})
        else:
            e["db"].finish_job(job_id, {}, error=path_or_err)
        return {"ok": ok, "path": path_or_err if ok else None, "error": path_or_err if not ok else None, "job_id": job_id}

    return await _run_sync(_backup)


@router.post("/delete-mirror", response_class=JSONResponse)
async def delete_mirror_content(
    post_types_json: str = Form("[]"),
    backup_job_id: int = Form(0),
):
    """
    Move posts deleted from old server to trash on new server.
    Requires a recent successful backup job ID.
    """
    e = _get_engines()

    def _delete():
        # Verify the backup job exists and succeeded
        if backup_job_id:
            bj = e["db"].get_job(backup_job_id)
            if not bj or bj.status != "done" or bj.job_type != "backup":
                return {"error": f"Backup job {backup_job_id} not found or not successful"}
        else:
            return {"error": "backup_job_id is required"}

        try:
            types = json.loads(post_types_json) or e["motor_b"].get_active_post_types()
        except (json.JSONDecodeError, ValueError):
            types = e["motor_b"].get_active_post_types()

        job_id = e["db"].create_job("motor_b", "delete")
        deltas = e["motor_b"].compute_all_deltas(types)

        trashed = 0
        errors = 0
        for delta in deltas:
            for diff in delta.deleted_posts:
                ok = e["motor_b"].delete_post_on_new(diff.post_id, job_id, backup_confirmed=True)
                if ok:
                    trashed += 1
                else:
                    errors += 1

        summary = {"trashed": trashed, "errors": errors}
        e["db"].finish_job(job_id, summary, error="" if errors == 0 else f"{errors} errors")
        return {"job_id": job_id, **summary}

    return await _run_sync(_delete)


@router.post("/verify-polylang", response_class=JSONResponse)
async def verify_polylang(post_ids_json: str = Form("[]"), post_types_json: str = Form("[]")):
    """
    Verify Polylang language + translation group for recently synced posts.
    If no post_ids given, use all posts that have been mapped in this session.
    """
    e = _get_engines()

    def _verify():
        try:
            post_ids = json.loads(post_ids_json)
        except (json.JSONDecodeError, ValueError):
            post_ids = []

        if not post_ids:
            # Use all known mapped IDs
            maps = e["db"].get_all_id_maps()
            post_ids = [m["new_id"] for m in maps]

        if not post_ids:
            return {"error": "No post IDs to verify"}

        job_id = e["db"].create_job("polylang_verify", "verify")
        result = e["motor_b"].verify_polylang(post_ids, job_id)
        e["db"].finish_job(job_id, result)
        return {"job_id": job_id, **result}

    return await _run_sync(_verify)


def _diff_to_dict(diff) -> dict:
    return {
        "post_id": diff.post_id,
        "post_type": diff.post_type,
        "post_name": diff.post_name,
        "post_status": diff.post_status,
        "old_modified": diff.old_modified,
        "new_modified": diff.new_modified,
        "language": diff.language,
    }
