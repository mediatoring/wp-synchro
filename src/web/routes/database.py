"""Database browser routes — read-only access to old server DB via WP-CLI."""
from __future__ import annotations

import re
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse

from ..app import templates, _get_engines, _run_sync
from ...config import get_config

router = APIRouter(prefix="/database", tags=["database"])

_VALID_TABLE = re.compile(r'^[a-zA-Z0-9_]+$')


@router.get("", response_class=HTMLResponse)
async def database_page(request: Request):
    cfg = get_config()
    return templates.TemplateResponse(request, "database.html", {
        "cfg": cfg,
        "active": "database",
    })


@router.get("/tables", response_class=JSONResponse)
async def list_tables():
    """List all tables from old server DB."""
    e = _get_engines()

    def _get():
        r = e["old"].wp("db tables --all-tables --format=csv")
        if not r.ok:
            return {"error": r.stderr[:300], "tables": []}
        tables = [t.strip() for t in r.stdout.splitlines() if t.strip()]
        return {"tables": tables}

    return await _run_sync(_get)


@router.post("/browse", response_class=JSONResponse)
async def browse_table(
    table: str = Form(...),
    offset: int = Form(0),
    limit: int = Form(50),
):
    """Browse a table on the old server (SELECT only, paginated)."""
    if not _VALID_TABLE.match(table):
        return JSONResponse({"error": "Neplatný název tabulky"}, status_code=400)

    e = _get_engines()

    def _get():
        # Get column names via DESCRIBE
        desc_r = e["old"].wp(f'db query "DESCRIBE {table}" --skip-column-names')
        columns = []
        if desc_r.ok:
            for line in desc_r.stdout.splitlines():
                parts = line.split("\t")
                if parts and parts[0].strip():
                    columns.append(parts[0].strip())

        # Count rows
        cnt_r = e["old"].wp(f'db query "SELECT COUNT(*) FROM {table}" --skip-column-names')
        total = 0
        if cnt_r.ok:
            try:
                total = int(cnt_r.stdout.strip())
            except ValueError:
                pass

        # Fetch rows
        n_cols = len(columns) if columns else 20
        sql = f"SELECT * FROM {table} LIMIT {int(limit)} OFFSET {int(offset)}"
        rows_r = e["old"].wp(f'db query "{sql}" --skip-column-names')
        rows = []
        if rows_r.ok:
            for line in rows_r.stdout.splitlines():
                if not line.strip():
                    continue
                # Split into at most n_cols parts; last cell keeps remainder
                parts = line.split("\t", n_cols - 1)
                rows.append([p[:500] for p in parts])  # truncate long cells

        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    return await _run_sync(_get)


@router.get("/missing-posts", response_class=JSONResponse)
async def missing_posts(search: str = ""):
    """
    Compare all posts between old and new server.
    Returns posts present on old but missing or outdated on new.
    """
    e = _get_engines()

    def _compare():
        old_prefix = e["old"]._cfg.table_prefix
        new_prefix = e["new"].get_table_prefix()

        cols = ["ID", "post_type", "post_title", "post_name", "post_status", "post_modified"]

        search_clause = ""
        if search:
            safe = search.replace("'", "''")
            search_clause = (
                f" AND (post_title LIKE '%{safe}%' OR post_name LIKE '%{safe}%')"
            )

        sql_old = (
            f"SELECT ID, post_type, post_title, post_name, post_status, post_modified "
            f"FROM {old_prefix}posts "
            f"WHERE post_status NOT IN ('auto-draft','inherit','revision','trash')"
            f"{search_clause} "
            f"ORDER BY post_modified DESC LIMIT 5000"
        )
        r_old = e["old"].wp(f'db query "{sql_old}" --skip-column-names')

        old_posts: dict = {}
        if r_old.ok:
            for line in r_old.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t", len(cols) - 1)
                try:
                    pid = int(parts[0].strip())
                    old_posts[pid] = {
                        cols[i]: parts[i].strip() if i < len(parts) else ""
                        for i in range(len(cols))
                    }
                except (ValueError, IndexError):
                    continue

        # Fetch IDs + modified dates from new server
        sql_new = (
            f"SELECT ID, post_modified FROM {new_prefix}posts "
            f"WHERE post_status NOT IN ('auto-draft','inherit','revision','trash')"
        )
        r_new = e["new"].wp(f'db query "{sql_new}" --skip-column-names')

        new_posts: dict = {}
        if r_new.ok:
            for line in r_new.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                try:
                    pid = int(parts[0].strip())
                    new_posts[pid] = parts[1].strip() if len(parts) > 1 else ""
                except (ValueError, IndexError):
                    continue

        missing = []
        outdated = []

        for pid, pdata in old_posts.items():
            if pid not in new_posts:
                missing.append({**pdata, "issue": "missing"})
            else:
                old_mod = pdata.get("post_modified", "")
                new_mod = new_posts[pid]
                if old_mod > new_mod:
                    outdated.append({
                        **pdata,
                        "issue": "outdated",
                        "new_modified": new_mod,
                    })

        return {
            "total_old": len(old_posts),
            "total_new": len(new_posts),
            "missing_count": len(missing),
            "outdated_count": len(outdated),
            "missing": missing,
            "outdated": outdated,
            "old_error": r_old.stderr[:200] if not r_old.ok else "",
            "new_error": r_new.stderr[:200] if not r_new.ok else "",
        }

    return await _run_sync(_compare)
