"""Snapshot of the sandbox state, printed as JSON on stdout.

Not baked into the image: the host pipes this file in and runs it as root with
`python3 -I -` (isolated mode, so files planted in the cwd cannot shadow stdlib
modules). Stdlib only, read-only (the only side effect is the probe's own SQL
statements in the Postgres log, tagged app=probe).

Output has two parts:
  state     content that must be byte-identical across fresh containers
            (config/code/data files, crontabs, DB schema + table contents, roles,
            supervisor program states)
  volatile  content that changes on its own while the server runs
            (log files, PIDs, uptimes, process list, timestamps)
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

PROBE_VERSION = 1

STATE_TREES = [
    "/etc",
    "/opt/acme",
    "/srv",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/var/spool/cron",
    "/var/cache/acme",
    "/home",
]
STATE_FILES = ["/var/lib/dpkg/status"]
VOLATILE_TREES = ["/var/log"]
# Managed by Docker per container (resolv.conf is copied from the host).
EXCLUDE = {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/etc/mtab"}

HEAD_BYTES = 4096
DB_NAME = "acme"


def _owner(st: os.stat_result) -> tuple[str, str]:
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return user, group


def _hash_file(path: str) -> tuple[str, str]:
    """Return (sha256 of whole file, sha256 of first HEAD_BYTES)."""
    full = hashlib.sha256()
    head = hashlib.sha256()
    seen = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 16):
            full.update(chunk)
            if seen < HEAD_BYTES:
                head.update(chunk[: HEAD_BYTES - seen])
                seen += len(chunk)
    return full.hexdigest(), head.hexdigest()


def describe(path: str, with_head: bool = False) -> dict[str, Any]:
    st = os.lstat(path)
    user, group = _owner(st)
    entry: dict[str, Any] = {"mode": oct(stat.S_IMODE(st.st_mode)), "owner": user, "group": group}
    if stat.S_ISLNK(st.st_mode):
        entry["type"] = "l"
        entry["target"] = os.readlink(path)
    elif stat.S_ISDIR(st.st_mode):
        entry["type"] = "d"
    elif stat.S_ISREG(st.st_mode):
        entry["type"] = "f"
        entry["size"] = st.st_size
        try:
            full, head = _hash_file(path)
            entry["sha256"] = full
            if with_head:
                entry["head_sha256"] = head
        except OSError as exc:
            entry["error"] = type(exc).__name__
    else:
        entry["type"] = "other"
    return entry


def walk(roots: list[str], with_head: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for root in roots:
        if not os.path.lexists(root):
            continue
        out[root] = describe(root, with_head)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                path = os.path.join(dirpath, name)
                if path in EXCLUDE:
                    continue
                try:
                    out[path] = describe(path, with_head)
                except FileNotFoundError:
                    continue
    return dict(sorted(out.items()))


def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _psql_env() -> dict[str, str]:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PGAPPNAME": "probe", "LANG": "C.UTF-8"}
    return env


def _as_postgres(args: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["runuser", "-u", "postgres", "--", *args], env=_psql_env())


def _strip_restrict(dump: str) -> str:
    # pg_dump >= 17.6 emits \restrict/\unrestrict lines with a random key on every run.
    return "\n".join(line for line in dump.splitlines() if not line.startswith(("\\restrict", "\\unrestrict")))


def _psql(sql: str, db: str = DB_NAME) -> list[list[str]]:
    res = _as_postgres(["psql", "-X", "-A", "-t", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return [line.split("\t") for line in res.stdout.splitlines() if line]


def db_snapshot() -> dict[str, Any]:
    try:
        schema = _as_postgres(["pg_dump", "--schema-only", DB_NAME])
        if schema.returncode != 0:
            return {"error": schema.stderr.strip()}
        roles = _as_postgres(["pg_dumpall", "--globals-only"])
        databases = [r[0] for r in _psql("SELECT datname FROM pg_database ORDER BY datname")]
        tables = _psql(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
        )
        table_info: dict[str, Any] = {}
        for schema_name, table in tables:
            ident = f'"{schema_name}"."{table}"'
            count, digest = _psql(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '') FROM {ident} t"
            )[0]
            table_info[f"{schema_name}.{table}"] = {"rows": int(count), "md5": digest}
        sequences = {
            f"{s}.{n}": {"last_value": v}
            for s, n, v in _psql(
                "SELECT schemaname, sequencename, coalesce(last_value::text, 'null') FROM pg_sequences ORDER BY 1, 2"
            )
        }
        schema_text = _strip_restrict(schema.stdout)
        roles_text = _strip_restrict(roles.stdout)
        return {
            "databases": databases,
            "schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
            "schema": schema_text,
            "roles_sha256": hashlib.sha256(roles_text.encode()).hexdigest(),
            "roles": roles_text,
            "tables": table_info,
            "sequences": sequences,
        }
    except Exception as exc:  # noqa: BLE001 - a broken DB is a valid observation
        return {"error": f"{type(exc).__name__}: {exc}"}


def services() -> tuple[dict[str, Any], dict[str, Any]]:
    res = _run(["supervisorctl", "status"])
    if not res.stdout.strip():
        err = {"error": res.stderr.strip() or f"supervisorctl exit {res.returncode}"}
        return err, err
    state: dict[str, Any] = {}
    volatile: dict[str, Any] = {}
    for line in res.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        detail = parts[2] if len(parts) > 2 else ""
        state[name] = status
        volatile[name] = {"status": status, "detail": detail}
    return state, volatile


def processes() -> list[str]:
    res = _run(["ps", "-eo", "user:16,args", "--no-headers"])
    ignore = ("ps -eo", "python3 -I -", "runuser", "supervisorctl status")
    return sorted(
        " ".join(line.split()) for line in res.stdout.splitlines() if not any(x in line for x in ignore)
    )


def main() -> int:
    svc_state, svc_volatile = services()
    files = walk(STATE_TREES)
    for path in STATE_FILES:
        if os.path.lexists(path):
            files[path] = describe(path)
    snapshot = {
        "probe_version": PROBE_VERSION,
        "state": {
            "files": dict(sorted(files.items())),
            "db": db_snapshot(),
            "services": svc_state,
        },
        "volatile": {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "logs": walk(VOLATILE_TREES, with_head=True),
            "services": svc_volatile,
            "processes": processes(),
        },
    }
    json.dump(snapshot, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
