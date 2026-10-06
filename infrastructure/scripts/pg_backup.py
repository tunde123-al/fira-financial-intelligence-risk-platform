#!/usr/bin/env python3
"""Backup, verify and restore-test FIRA's PostgreSQL database (pg_dump / pg_restore).

Subcommands
  backup        pg_dump -Fc into --out-dir, write a manifest (row counts, Alembic revision, size, SHA-256),
                optionally encrypt (AES-256-GCM, key derived from a passphrase read from an environment variable)
                and prune old backups (--keep N)
  verify        check the manifest checksum and that pg_restore can read the archive (no database needed)
  restore       restore an archive into a TARGET database that you name; never touches the source database
  restore-test  create a scratch database, restore into it, compare every manifest row count, the Alembic revision and
                the append-only triggers, print the measured restore time, then drop the scratch database

Connections use DATABASE_URL (or --url). The password is passed to the tools through PGPASSWORD, never on a command
line. With --docker-container NAME the PostgreSQL client tools run inside that container (useful on Windows where
pg_dump is usually not installed); the database URL is still used for the row-count checks.

What this does NOT do: schedule itself, copy the archive off the machine, or manage keys. Run it from cron / a
scheduled task / a CI job, store the archive and the passphrase in different places, and test restores regularly.
docs/DISASTER_RECOVERY.md states the frequency, retention and RPO/RTO targets and what has actually been measured.

Examples
  python infrastructure/scripts/pg_backup.py backup --out-dir backups --keep 7
  python infrastructure/scripts/pg_backup.py verify backups/fira_20261003T120000Z.dump
  python infrastructure/scripts/pg_backup.py restore-test backups/fira_20261003T120000Z.dump
  python infrastructure/scripts/pg_backup.py restore backups/fira_...dump --target-db fira_restored
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

KEY_TABLES = ["customers", "accounts", "transactions", "audit_log", "monitoring_alerts", "cases", "case_notes",
              "ingestion_batches", "rejected_transactions", "config_change_log", "risk_config_versions",
              "investigations", "users"]
APPEND_ONLY = {"audit_log": "audit_log_no_update_delete", "ingestion_batches": "ib_append_only",
               "rejected_transactions": "rt_append_only", "config_change_log": "ccl_append_only"}
MAGIC = b"FIRAENC1"


def die(msg: str, code: int = 2) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def sqla_url(raw: str | None) -> URL:
    raw = raw or os.environ.get("DATABASE_URL")
    if not raw:
        die("set DATABASE_URL or pass --url")
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql+psycopg://" + raw[len(prefix):]
    return make_url(raw)


class Runner:
    """Runs PostgreSQL client tools locally or inside a container; passwords travel in the environment only."""

    def __init__(self, url: URL, container: str | None):
        self.url, self.container = url, container

    def _argv(self, tool: str, args: list[str], db: str | None = None) -> tuple[list[str], dict[str, str]]:
        u = self.url
        conn = ["-h", u.host or "localhost", "-p", str(u.port or 5432), "-U", u.username or "postgres"]
        if self.container:  # tools run inside the container, over its local socket
            conn = ["-U", u.username or "postgres"]
            argv = ["docker", "exec", "-i", "-e", f"PGPASSWORD={u.password or ''}", self.container, tool, *conn]
            return argv + args, dict(os.environ)
        env = dict(os.environ, PGPASSWORD=u.password or "")
        return [tool, *conn, *args], env

    def run(self, tool: str, args: list[str], stdin: Any = None, stdout: Any = None) -> subprocess.CompletedProcess[bytes]:
        if not self.container and shutil.which(tool) is None:
            die(f"{tool} not found; install the PostgreSQL client tools or use --docker-container")
        argv, env = self._argv(tool, args)
        return subprocess.run(argv, env=env, stdin=stdin, stdout=stdout or subprocess.PIPE,  # noqa: S603
                              stderr=subprocess.PIPE, check=False)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- encryption
def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=64 * 2 ** 20)


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    dst.write_bytes(MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, src.read_bytes(), MAGIC))


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = src.read_bytes()
    if raw[:8] != MAGIC:
        die("not an encrypted FIRA backup")
    salt, nonce, body = raw[8:24], raw[24:36], raw[36:]
    try:
        dst.write_bytes(AESGCM(_key(passphrase, salt)).decrypt(nonce, body, MAGIC))
    except Exception:
        die("decryption failed: wrong passphrase or damaged file")


def passphrase(env_name: str | None) -> str | None:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if not value or len(value) < 12:
        die(f"environment variable {env_name} must hold a passphrase of at least 12 characters")
    return value


# --------------------------------------------------------------------------- database facts
def db_facts(url: URL) -> dict[str, Any]:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            existing = {r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
            counts = {t: int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() or 0)  # noqa: S608
                      for t in KEY_TABLES if t in existing}
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() \
                if "alembic_version" in existing else None
            triggers = {r[0] for r in conn.execute(text(
                "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
        return {"row_counts": counts, "alembic_revision": rev, "triggers": sorted(triggers)}
    finally:
        eng.dispose()


def admin_engine(url: URL):
    return create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")


# --------------------------------------------------------------------------- commands
def cmd_backup(a: argparse.Namespace) -> int:
    url = sqla_url(a.url)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump = out / f"fira_{stamp}.dump"
    facts = db_facts(url)
    t0 = time.perf_counter()
    with dump.open("wb") as f:
        r = Runner(url, a.docker_container).run("pg_dump", ["-Fc", "--no-owner", "--no-privileges", "-d", url.database or "fira"],
                                                stdout=f)
    if r.returncode != 0:
        dump.unlink(missing_ok=True)
        die("pg_dump failed: " + r.stderr.decode(errors="replace")[:400], 1)
    seconds = round(time.perf_counter() - t0, 2)
    ver = Runner(url, a.docker_container).run("pg_dump", ["--version"]).stdout.decode().strip()
    final = dump
    enc = passphrase(a.passphrase_env)
    if enc:
        final = dump.with_suffix(".dump.enc")
        encrypt_file(dump, final, enc)
        dump.unlink()
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "file": final.name, "encrypted": bool(enc),
                "database": url.database, "format": "pg_dump custom (-Fc)", "pg_dump": ver,
                "size_bytes": final.stat().st_size, "sha256": sha256_of(final), "dump_seconds": seconds,
                **facts}
    mpath = final.with_name(final.name + ".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    pruned = prune(out, a.keep) if a.keep else []
    print(json.dumps({"backup": str(final), "manifest": str(mpath), "size_bytes": manifest["size_bytes"],
                      "dump_seconds": seconds, "encrypted": bool(enc), "pruned": pruned,
                      "row_counts": facts["row_counts"], "alembic_revision": facts["alembic_revision"]}, indent=2))
    return 0


def prune(out: Path, keep: int) -> list[str]:
    archives = sorted(p for p in out.glob("fira_*.dump*") if not p.name.endswith(".manifest.json"))
    removed = []
    for p in archives[:-keep] if keep > 0 else []:
        for q in (p, p.with_name(p.name + ".manifest.json")):
            q.unlink(missing_ok=True)
        removed.append(p.name)
    return removed


def load_manifest(archive: Path) -> dict[str, Any]:
    m = archive.with_name(archive.name + ".manifest.json")
    if not m.exists():
        die(f"manifest not found next to the archive: {m.name}")
    return json.loads(m.read_text(encoding="utf-8"))


def cmd_verify(a: argparse.Namespace) -> int:
    archive = Path(a.archive)
    man = load_manifest(archive)
    ok = sha256_of(archive) == man["sha256"] and archive.stat().st_size == man["size_bytes"]
    result: dict[str, Any] = {"archive": archive.name, "checksum_matches_manifest": ok, "encrypted": man["encrypted"]}
    if not ok:
        print(json.dumps(result, indent=2))
        return 1
    work, tmp = archive, None
    if man["encrypted"]:
        enc = passphrase(a.passphrase_env)
        if not enc:
            die("this archive is encrypted: pass --passphrase-env")
        tmp = Path(tempfile.mkdtemp(prefix="fira-verify-")) / "plain.dump"
        decrypt_file(archive, tmp, enc)
        work = tmp
    r = Runner(sqla_url(a.url), a.docker_container)
    with work.open("rb") as f:
        lst = r.run("pg_restore", ["--list"], stdin=f)
    result["pg_restore_can_read"] = lst.returncode == 0
    result["toc_entries"] = len([ln for ln in lst.stdout.decode(errors="replace").splitlines() if ln and not ln.startswith(";")])
    if tmp:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    print(json.dumps(result, indent=2))
    return 0 if result["pg_restore_can_read"] else 1


def restore_into(a: argparse.Namespace, archive: Path, target_db: str, man: dict[str, Any]) -> dict[str, Any]:
    url = sqla_url(a.url)
    work, tmp = archive, None
    if man["encrypted"]:
        enc = passphrase(a.passphrase_env)
        if not enc:
            die("this archive is encrypted: pass --passphrase-env")
        tmp = Path(tempfile.mkdtemp(prefix="fira-restore-")) / "plain.dump"
        decrypt_file(archive, tmp, enc)
        work = tmp
    runner = Runner(url, a.docker_container)
    t0 = time.perf_counter()
    with work.open("rb") as f:
        r = runner.run("pg_restore", ["--no-owner", "--no-privileges", "--exit-on-error", "-d", target_db], stdin=f)
    seconds = round(time.perf_counter() - t0, 2)
    if tmp:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    if r.returncode != 0:
        die("pg_restore failed: " + r.stderr.decode(errors="replace")[:600], 1)
    return {"restore_seconds": seconds}


def cmd_restore(a: argparse.Namespace) -> int:
    archive = Path(a.archive)
    man = load_manifest(archive)
    if sha256_of(archive) != man["sha256"]:
        die("checksum mismatch: the archive is damaged or was modified", 1)
    url = sqla_url(a.url)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", a.target_db):
        die("--target-db must be a simple identifier")
    if a.target_db == url.database:
        die("refusing to restore over the source database; choose a different --target-db")
    adm = admin_engine(url)
    with adm.connect() as conn:
        if conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": a.target_db}).scalar():
            die(f"database {a.target_db} already exists; drop it yourself if you intend to replace it")
        conn.execute(text(f'CREATE DATABASE "{a.target_db}"'))  # noqa: S608
    adm.dispose()
    res = restore_into(a, archive, a.target_db, man)
    print(json.dumps({"restored_into": a.target_db, **res}, indent=2))
    return 0


def cmd_restore_test(a: argparse.Namespace) -> int:
    archive = Path(a.archive)
    man = load_manifest(archive)
    if sha256_of(archive) != man["sha256"]:
        die("checksum mismatch: the archive is damaged or was modified", 1)
    url = sqla_url(a.url)
    scratch = f"fira_restore_test_{uuid.uuid4().hex[:8]}"
    adm = admin_engine(url)
    with adm.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{scratch}"'))  # noqa: S608
    checks: dict[str, Any] = {}
    try:
        res = restore_into(a, archive, scratch, man)
        facts = db_facts(url.set(database=scratch))
        counts_ok = {t: facts["row_counts"].get(t) == n for t, n in man["row_counts"].items()}
        checks = {
            "restore_seconds": res["restore_seconds"],
            "row_counts_match": all(counts_ok.values()), "row_count_detail": {t: [man["row_counts"][t], facts["row_counts"].get(t)] for t in man["row_counts"]},
            "alembic_revision_matches": facts["alembic_revision"] == man["alembic_revision"],
            "append_only_triggers_present": {t: g in facts["triggers"] for t, g in APPEND_ONLY.items()
                                             if t in man["row_counts"]}}
        checks["passed"] = (checks["row_counts_match"] and checks["alembic_revision_matches"]
                            and all(checks["append_only_triggers_present"].values()))
        if a.keep_database:
            checks["kept_database"] = scratch
    finally:
        if not a.keep_database:
            with adm.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))  # noqa: S608
        adm.dispose()
    print(json.dumps({"archive": archive.name, "scratch_database": scratch, **checks}, indent=2))
    return 0 if checks.get("passed") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", help="database URL (default: DATABASE_URL)")
        p.add_argument("--docker-container", help="run pg_dump / pg_restore inside this container")
        p.add_argument("--passphrase-env", help="name of the environment variable holding the encryption passphrase")

    b = sub.add_parser("backup")
    common(b)
    b.add_argument("--out-dir", default="backups")
    b.add_argument("--keep", type=int, default=0, help="keep only the newest N backups (0 = keep all)")
    b.set_defaults(fn=cmd_backup)
    v = sub.add_parser("verify")
    common(v)
    v.add_argument("archive")
    v.set_defaults(fn=cmd_verify)
    r = sub.add_parser("restore")
    common(r)
    r.add_argument("archive")
    r.add_argument("--target-db", required=True)
    r.set_defaults(fn=cmd_restore)
    t = sub.add_parser("restore-test")
    common(t)
    t.add_argument("archive")
    t.add_argument("--keep-database", action="store_true", help="do not drop the scratch database afterwards")
    t.set_defaults(fn=cmd_restore_test)
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
