"""FastAPI application: REST API + web interface for IMAP backup/restore."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, crypto, db, mailstore
from .parsing import parse_credentials
from .runner import default_port, refresh_mailbox, runner

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("imapbackup")

STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------- models


class ServerSpec(BaseModel):
    host: str = Field(min_length=1)
    port: int | None = None
    security: Literal["ssl", "starttls", "plain"] = "ssl"
    user: str | None = None

    @property
    def resolved_port(self) -> int:
        return self.port or default_port(self.security)


class Options(BaseModel):
    dry_run: bool = False
    automap: bool = True
    insecure_tls: bool = False
    exclude: str = ""
    folders: str = ""
    extra_args: str = ""


class BulkImport(BaseModel):
    source: ServerSpec
    credentials: str
    action: Literal["backup", "check", "import"] = "backup"
    options: Options = Options()


class JobRequest(BaseModel):
    account_ids: list[int]
    kind: Literal["backup", "check"] = "backup"
    options: Options = Options()


class RestoreRequest(BaseModel):
    account_ids: list[int]
    target: ServerSpec
    user_mode: Literal["email", "source", "mapping"] = "email"
    password_mode: Literal["single", "mapping"] = "single"
    single_password: str = ""
    mapping: str = ""
    options: Options = Options()


# ------------------------------------------------------------------ lifecycle


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    runner.start()
    runner.recover()
    task = asyncio.create_task(_periodic_rescan())
    log.info("imapbackup %s ready (concurrency=%s, store=%s)",
             config.VERSION, config.MAX_CONCURRENT_JOBS, config.MAILDIR_ROOT)
    try:
        yield
    finally:
        task.cancel()
        await runner.shutdown()
        db.close()


async def _periodic_rescan() -> None:
    while True:
        try:
            await asyncio.sleep(config.RESCAN_INTERVAL)
            busy = {
                row["account_id"]
                for row in db.query(
                    "SELECT DISTINCT account_id FROM jobs"
                    " WHERE status IN ('queued','running')"
                )
            }
            for row in db.query("SELECT id, local_user FROM accounts"):
                if row["id"] not in busy:
                    await refresh_mailbox(row["id"], row["local_user"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("mailbox rescan failed")


app = FastAPI(title="IMAP Backup Console", version=config.VERSION,
              lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """Block state changes triggered from another site.

    A cross-site form POST cannot set a custom header, and a cross-site fetch
    with one needs a CORS preflight that this app never answers. Browsers set
    Sec-Fetch-Site themselves, so it cannot be forged either.
    """
    if request.method not in SAFE_METHODS:
        same_site = request.headers.get("sec-fetch-site", "") in ("same-origin", "none")
        asked = request.headers.get("x-requested-with", "").lower() == "imapbackup"
        if not (same_site or asked):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Cross-site request blocked. Send the header "
                              "'X-Requested-With: imapbackup'."
                },
            )
    return await call_next(request)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.AUTH_USER and request.url.path != "/api/health":
        header = request.headers.get("authorization", "")
        authorized = False
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = decoded.partition(":")
                authorized = secrets.compare_digest(
                    user, config.AUTH_USER
                ) and secrets.compare_digest(password, config.AUTH_PASSWORD)
            except (binascii.Error, UnicodeDecodeError):
                authorized = False
        if not authorized:
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="IMAP Backup Console"'},
            )
    return await call_next(request)


# ----------------------------------------------------------------- serializers


def _json(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def job_dict(row) -> dict:
    options = _json(row["options"])
    target = options.get("target") or {}
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "email": row["email"] if "email" in row.keys() else None,
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "error": row["error"],
        "dry_run": bool(options.get("dry_run")),
        "target_host": target.get("host"),
        "target_user": target.get("user"),
        "progress": _json(row["progress"]),
        "stats": _json(row["stats"]),
        "has_log": bool(row["log_file"]) and Path(row["log_file"]).exists(),
    }


def account_dict(row, latest: dict | None = None) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "local_user": row["local_user"],
        "source": {
            "host": row["src_host"],
            "port": row["src_port"],
            "security": row["src_security"],
            "user": row["src_user"],
        },
        "created_at": row["created_at"],
        "last_backup_at": row["last_backup_at"],
        "mailbox": {
            "bytes": row["mailbox_bytes"],
            "messages": row["mailbox_messages"],
            "folders": row["mailbox_folders"],
            "scanned_at": row["scanned_at"],
        },
        "latest_job": latest,
    }


def _account_or_404(account_id: int):
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return row


def _unique_local_user(email: str) -> str:
    base = mailstore.sanitize_local_user(email)
    candidate = base
    suffix = 2
    while db.query_one(
        "SELECT id FROM accounts WHERE local_user = ?", (candidate,)
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _create_job(account_id: int, kind: str, options: dict,
                target_password: str | None = None) -> int:
    job_id = db.execute(
        "INSERT INTO jobs (account_id, kind, status, created_at, options,"
        " target_password) VALUES (?, ?, 'queued', ?, ?, ?)",
        (
            account_id,
            kind,
            db.now(),
            json.dumps(options),
            crypto.encrypt(target_password) if target_password else None,
        ),
    )
    runner.enqueue(job_id)
    return job_id


# ------------------------------------------------------------------ endpoints


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": config.VERSION}


@app.get("/api/state")
async def state() -> dict:
    latest_rows = db.query(
        "SELECT j.*, a.email AS email FROM jobs j"
        " JOIN accounts a ON a.id = j.account_id"
        " WHERE j.id IN (SELECT MAX(id) FROM jobs GROUP BY account_id)"
    )
    latest = {row["account_id"]: job_dict(row) for row in latest_rows}

    accounts = [
        account_dict(row, latest.get(row["id"]))
        for row in db.query("SELECT * FROM accounts ORDER BY email COLLATE NOCASE")
    ]
    jobs = [
        job_dict(row)
        for row in db.query(
            "SELECT j.*, a.email AS email FROM jobs j"
            " JOIN accounts a ON a.id = j.account_id"
            " ORDER BY j.id DESC LIMIT 80"
        )
    ]
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for row in db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        counts[row["status"]] = row["n"]

    return {
        "accounts": accounts,
        "jobs": jobs,
        "counts": counts,
        "totals": {
            "accounts": len(accounts),
            "bytes": sum(a["mailbox"]["bytes"] for a in accounts),
            "messages": sum(a["mailbox"]["messages"] for a in accounts),
        },
        "disk": mailstore.disk_usage(),
        "config": {
            "version": config.VERSION,
            "concurrency": config.MAX_CONCURRENT_JOBS,
            "store": str(config.MAILDIR_ROOT),
            "allow_extra_args": config.ALLOW_EXTRA_ARGS,
        },
    }


@app.post("/api/accounts/bulk")
async def bulk_import(payload: BulkImport) -> dict:
    entries, errors = parse_credentials(payload.credentials)
    if not entries and not errors:
        raise HTTPException(status_code=400, detail="No credentials supplied")

    port = payload.source.resolved_port
    created = updated = 0
    job_ids: list[int] = []
    options = payload.options.model_dump()

    for entry in entries:
        login = entry["login"]
        existing = db.query_one(
            "SELECT * FROM accounts WHERE email = ? AND src_host = ? AND src_user = ?",
            (entry["email"], payload.source.host, login),
        )
        if existing is not None:
            db.execute(
                "UPDATE accounts SET src_password=?, src_port=?, src_security=?,"
                " updated_at=? WHERE id=?",
                (
                    crypto.encrypt(entry["password"]),
                    port,
                    payload.source.security,
                    db.now(),
                    existing["id"],
                ),
            )
            account_id = existing["id"]
            updated += 1
        else:
            local_user = _unique_local_user(entry["email"])
            account_id = db.execute(
                "INSERT INTO accounts (email, local_user, src_host, src_port,"
                " src_security, src_user, src_password, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry["email"],
                    local_user,
                    payload.source.host,
                    port,
                    payload.source.security,
                    login,
                    crypto.encrypt(entry["password"]),
                    db.now(),
                    db.now(),
                ),
            )
            created += 1

        if payload.action in ("backup", "check"):
            job_ids.append(_create_job(account_id, payload.action, options))

    return {
        "created": created,
        "updated": updated,
        "jobs": job_ids,
        "errors": errors,
    }


@app.post("/api/jobs")
async def start_jobs(payload: JobRequest) -> dict:
    options = payload.options.model_dump()
    job_ids = []
    for account_id in payload.account_ids:
        _account_or_404(account_id)
        job_ids.append(_create_job(account_id, payload.kind, options))
    return {"jobs": job_ids}


@app.post("/api/restore")
async def start_restore(payload: RestoreRequest) -> dict:
    mapping: dict[str, dict] = {}
    errors: list[str] = []
    if payload.password_mode == "mapping" or payload.user_mode == "mapping":
        entries, errors = parse_credentials(payload.mapping)
        mapping = {entry["email"].lower(): entry for entry in entries}

    target = payload.target
    job_ids: list[int] = []
    skipped: list[str] = []

    for account_id in payload.account_ids:
        account = _account_or_404(account_id)
        entry = mapping.get(account["email"].lower())

        if payload.password_mode == "single":
            password = payload.single_password
        else:
            password = entry["password"] if entry else ""
        if not password:
            skipped.append(f"{account['email']}: no target password supplied")
            continue

        if payload.user_mode == "mapping":
            user = (entry or {}).get("login") or account["email"]
        elif payload.user_mode == "source":
            user = account["src_user"]
        else:
            user = account["email"]

        if not mailstore.maildir_path(account["local_user"]).is_dir():
            skipped.append(f"{account['email']}: nothing backed up yet")
            continue

        options = payload.options.model_dump()
        options["target"] = {
            "host": target.host,
            "port": target.resolved_port,
            "security": target.security,
            "user": user,
        }
        job_ids.append(_create_job(account_id, "restore", options, password))

    return {"jobs": job_ids, "skipped": skipped, "errors": errors}


@app.get("/api/accounts/{account_id}")
async def account_detail(account_id: int) -> dict:
    account = _account_or_404(account_id)
    report = await asyncio.to_thread(mailstore.scan, account["local_user"])
    db.execute(
        "UPDATE accounts SET mailbox_bytes=?, mailbox_messages=?,"
        " mailbox_folders=?, scanned_at=? WHERE id=?",
        (report["bytes"], report["messages"], len(report["folders"]),
         db.now(), account_id),
    )
    account = _account_or_404(account_id)
    jobs = [
        job_dict(row)
        for row in db.query(
            "SELECT j.*, a.email AS email FROM jobs j"
            " JOIN accounts a ON a.id = j.account_id"
            " WHERE j.account_id = ? ORDER BY j.id DESC LIMIT 25",
            (account_id,),
        )
    ]
    data = account_dict(account)
    data["folders"] = report["folders"]
    data["jobs"] = jobs
    return data


@app.post("/api/accounts/{account_id}/rescan")
async def rescan(account_id: int) -> dict:
    account = _account_or_404(account_id)
    report = await refresh_mailbox(account_id, account["local_user"])
    return {"bytes": report["bytes"], "messages": report["messages"],
            "folders": len(report["folders"])}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, purge: bool = Query(False)) -> dict:
    account = _account_or_404(account_id)
    for row in db.query(
        "SELECT id, status FROM jobs WHERE account_id = ?", (account_id,)
    ):
        if row["status"] in ("queued", "running"):
            await runner.cancel(row["id"])
    for row in db.query(
        "SELECT log_file FROM jobs WHERE account_id = ? AND log_file IS NOT NULL",
        (account_id,),
    ):
        Path(row["log_file"]).unlink(missing_ok=True)
    db.execute("DELETE FROM jobs WHERE account_id = ?", (account_id,))
    db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    if purge:
        await asyncio.to_thread(mailstore.delete, account["local_user"])
    return {"deleted": account_id, "purged": purge}


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: int) -> dict:
    row = db.query_one(
        "SELECT j.*, a.email AS email FROM jobs j"
        " JOIN accounts a ON a.id = j.account_id WHERE j.id = ?",
        (job_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_dict(row)


@app.get("/api/jobs/{job_id}/log")
async def job_log(job_id: int, offset: int = Query(0, ge=0)) -> dict:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    path = Path(row["log_file"]) if row["log_file"] else None
    if path is None or not path.exists():
        return {"offset": 0, "size": 0, "data": "", "status": row["status"]}

    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(400_000)
    return {
        "offset": offset + len(chunk),
        "size": size,
        "data": chunk.decode("utf-8", errors="replace"),
        "status": row["status"],
    }


@app.get("/api/jobs/{job_id}/log/download")
async def job_log_download(job_id: int):
    row = db.query_one(
        "SELECT j.*, a.email AS email FROM jobs j"
        " JOIN accounts a ON a.id = j.account_id WHERE j.id = ?",
        (job_id,),
    )
    if row is None or not row["log_file"] or not Path(row["log_file"]).exists():
        raise HTTPException(status_code=404, detail="No log for this job")
    safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", row["email"])
    name = f"imapsync-{row['kind']}-{safe}-job{row['id']}.log"
    return FileResponse(row["log_file"], media_type="text/plain", filename=name)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict:
    ok = await runner.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job is not cancellable")
    return {"cancelled": job_id}


# ------------------------------------------------------------------------- ui


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
