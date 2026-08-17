"""Job queue: runs imapsync as a subprocess and tracks its progress."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import stat
import tempfile
import time
from pathlib import Path

from . import config, crypto, db, mailstore
from .parsing import OutputParser, parse_list

log = logging.getLogger("imapbackup.runner")

DEFAULT_PORTS = {"ssl": 993, "starttls": 143, "plain": 143}

TERMINAL = ("done", "failed", "cancelled")


def default_port(security: str) -> int:
    return DEFAULT_PORTS.get(security, 993)


def _leg(index: int, host: str, port: int, security: str, user: str,
         passfile: Path) -> list[str]:
    args = [
        f"--host{index}", host,
        f"--port{index}", str(port),
        f"--user{index}", user,
        f"--passfile{index}", str(passfile),
    ]
    if security == "ssl":
        args.append(f"--ssl{index}")
    elif security == "starttls":
        args.append(f"--tls{index}")
    else:
        args.append(f"--nossl{index}")
        args.append(f"--notls{index}")
    return args


def _write_passfile(directory: Path, name: str, password: str) -> Path:
    path = directory / name
    path.write_text(password, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


class JobRunner:
    def __init__(self) -> None:
        self._sem: asyncio.Semaphore | None = None
        self._tasks: dict[int, asyncio.Task] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled: set[int] = set()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)

    def recover(self) -> None:
        """Clean up after a restart and pick the queue back up."""
        stale = db.query("SELECT id FROM jobs WHERE status = 'running'")
        for row in stale:
            db.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                ("Interrupted by a service restart.", db.now(), row["id"]),
            )
        for row in db.query("SELECT id FROM jobs WHERE status='queued' ORDER BY id"):
            self.enqueue(row["id"])

    async def shutdown(self) -> None:
        for job_id, proc in list(self._procs.items()):
            if proc.returncode is None:
                proc.terminate()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ---------------------------------------------------------------- queue

    def enqueue(self, job_id: int) -> None:
        if job_id in self._tasks:
            return
        task = asyncio.create_task(self._guard(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))

    def is_active(self, job_id: int) -> bool:
        return job_id in self._tasks

    async def cancel(self, job_id: int) -> bool:
        row = db.query_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
        if row is None or row["status"] in TERMINAL:
            return False
        self._cancelled.add(job_id)
        proc = self._procs.get(job_id)
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
        else:
            db.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, error=? WHERE id=?",
                (db.now(), "Cancelled before it started.", job_id),
            )
        return True

    # -------------------------------------------------------------- running

    async def _guard(self, job_id: int) -> None:
        assert self._sem is not None
        try:
            async with self._sem:
                if job_id in self._cancelled:
                    db.execute(
                        "UPDATE jobs SET status='cancelled', finished_at=?, error=?"
                        " WHERE id=? AND status='queued'",
                        (db.now(), "Cancelled before it started.", job_id),
                    )
                    return
                await self._execute(job_id)
        except asyncio.CancelledError:
            # Only reached when the service itself shuts down; a user cancel
            # goes through cancel() and finishes the normal way.
            db.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, error=?"
                " WHERE id=? AND status NOT IN ('done','failed','cancelled')",
                (
                    db.now(),
                    "Interrupted by a service restart - start it again, "
                    "imapsync continues where it left off.",
                    job_id,
                ),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surface anything in the UI
            log.exception("job %s crashed", job_id)
            db.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (f"{type(exc).__name__}: {exc}", db.now(), job_id),
            )
        finally:
            self._cancelled.discard(job_id)
            self._procs.pop(job_id, None)

    def _build_command(self, job, account, options: dict,
                       workdir: Path) -> tuple[list[str], str]:
        kind = job["kind"]
        local_user = account["local_user"]
        local_pass = config.DOVECOT_MASTER_PASSWORD
        if not local_pass:
            raise RuntimeError("DOVECOT_MASTER_PASSWORD is not configured")

        local_file = _write_passfile(workdir, "local.pass", local_pass)
        local_leg_args = (
            config.DOVECOT_HOST, config.DOVECOT_PORT, "plain",
            local_user, local_file,
        )

        if kind == "restore":
            target = options.get("target") or {}
            if not target.get("host"):
                raise RuntimeError("No target server configured for this restore")
            target_pass = crypto.decrypt(job["target_password"] or "")
            if not target_pass:
                raise RuntimeError("No target password stored for this restore")
            remote_file = _write_passfile(workdir, "remote.pass", target_pass)
            args = _leg(1, *local_leg_args)
            args += _leg(
                2, target["host"], int(target["port"]), target["security"],
                target.get("user") or account["email"], remote_file,
            )
            direction = (
                f"local:{local_user} -> {target['host']}:{target['port']}"
                f" as {target.get('user') or account['email']}"
            )
        else:
            source_pass = crypto.decrypt(account["src_password"])
            remote_file = _write_passfile(workdir, "remote.pass", source_pass)
            args = _leg(
                1, account["src_host"], int(account["src_port"]),
                account["src_security"], account["src_user"], remote_file,
            )
            args += _leg(2, *local_leg_args)
            direction = (
                f"{account['src_host']}:{account['src_port']}"
                f" as {account['src_user']} -> local:{local_user}"
            )

        cmd = [config.IMAPSYNC_BIN] + args + [
            "--nolog",
            "--noreleasecheck",
            "--timeout1", str(config.IMAP_TIMEOUT),
            "--timeout2", str(config.IMAP_TIMEOUT),
            "--errorsmax", "200",
            "--tmpdir", str(workdir),
        ]

        if kind == "check":
            cmd.append("--justlogin")
        else:
            if options.get("automap", True):
                cmd.append("--automap")
            if options.get("dry_run"):
                cmd.append("--dry")
            for pattern in parse_list(options.get("exclude", "")):
                cmd += ["--exclude", pattern]
            for folder in parse_list(options.get("folders", "")):
                cmd += ["--folder", folder]

        if options.get("insecure_tls"):
            cmd += ["--sslargs1", "SSL_verify_mode=0",
                    "--sslargs2", "SSL_verify_mode=0"]

        extra = (options.get("extra_args") or "").strip()
        if extra:
            cmd += shlex.split(extra)

        return cmd, direction

    async def _execute(self, job_id: int) -> None:
        job = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            return
        account = db.query_one(
            "SELECT * FROM accounts WHERE id = ?", (job["account_id"],)
        )
        if account is None:
            db.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                ("Account no longer exists.", db.now(), job_id),
            )
            return

        options = json.loads(job["options"] or "{}")
        log_file = config.LOG_DIR / f"job-{job_id:06d}-{job['kind']}.log"
        workdir = Path(tempfile.mkdtemp(prefix=f"job-{job_id}-", dir="/tmp"))
        os.chmod(workdir, stat.S_IRWXU)

        parser = OutputParser()
        started = db.now()
        db.execute(
            "UPDATE jobs SET status='running', started_at=?, log_file=?,"
            " error=NULL WHERE id=?",
            (started, str(log_file), job_id),
        )

        try:
            cmd, direction = self._build_command(job, account, options, workdir)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        printable = " ".join(shlex.quote(part) for part in cmd)
        header = (
            f"# imapsync {job['kind']} job #{job_id}\n"
            f"# account : {account['email']}\n"
            f"# route   : {direction}\n"
            f"# started : {started}\n"
            f"# command : {printable}\n"
            f"#{'-' * 70}\n"
        )

        rc = -1
        try:
            with open(log_file, "w", encoding="utf-8", errors="replace") as sink:
                sink.write(header)
                sink.flush()

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(workdir),
                    env={**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
                )
                self._procs[job_id] = proc

                assert proc.stdout is not None
                pending = b""
                last_push = 0.0
                while True:
                    chunk = await proc.stdout.read(16384)
                    if not chunk:
                        break
                    sink.write(chunk.decode("utf-8", errors="replace"))
                    sink.flush()
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > 1_000_000:  # pathological single line
                        pending = pending[-4096:]
                    for raw in lines:
                        parser.feed(raw.decode("utf-8", errors="replace"))
                    now = time.monotonic()
                    if now - last_push > 1.5:
                        last_push = now
                        db.execute(
                            "UPDATE jobs SET progress=? WHERE id=?",
                            (json.dumps(parser.progress), job_id),
                        )

                if pending:
                    parser.feed(pending.decode("utf-8", errors="replace"))
                rc = await proc.wait()
                sink.write(f"\n#{'-' * 70}\n# imapsync exited with code {rc}\n")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._procs.pop(job_id, None)

        cancelled = job_id in self._cancelled
        if cancelled:
            status = "cancelled"
            error = "Cancelled by user."
        elif rc == 0:
            status = "done"
            error = None
        else:
            status = "failed"
            error = parser.summary_error() or f"imapsync exited with code {rc}"

        db.execute(
            "UPDATE jobs SET status=?, finished_at=?, exit_code=?, progress=?,"
            " stats=?, error=? WHERE id=?",
            (
                status,
                db.now(),
                rc,
                json.dumps(parser.progress),
                json.dumps(parser.stats),
                error,
                job_id,
            ),
        )

        if job["kind"] == "backup" and status == "done" and not options.get("dry_run"):
            db.execute(
                "UPDATE accounts SET last_backup_at = ? WHERE id = ?",
                (db.now(), account["id"]),
            )
        if job["kind"] in ("backup", "restore"):
            await refresh_mailbox(account["id"], account["local_user"])


async def refresh_mailbox(account_id: int, local_user: str) -> dict:
    report = await asyncio.to_thread(mailstore.scan, local_user)
    db.execute(
        "UPDATE accounts SET mailbox_bytes=?, mailbox_messages=?,"
        " mailbox_folders=?, scanned_at=? WHERE id=?",
        (
            report["bytes"],
            report["messages"],
            len(report["folders"]),
            db.now(),
            account_id,
        ),
    )
    return report


runner = JobRunner()
