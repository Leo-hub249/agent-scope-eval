"""Snapshot of the sandbox state, printed as JSON on stdout.

Not baked into the image: the host pipes this file in and runs it as root with
`python3 -I -` (isolated mode, so files planted in the cwd cannot shadow stdlib
modules). Stdlib only, read-only (the only side effect is the probe's own SQL
statements in the Postgres log, tagged app=probe).

Output has two parts:
  state     content that must be byte-identical across fresh containers
            (config/code/data files, crontabs, DB schema + table and column contents,
            roles, supervisor program states)
  volatile  content that changes on its own while the server runs
            (log files, PIDs, uptimes, process list, timestamps)

Optional input (env PROBE_PREFIX_SIZES, JSON {path: size}): for those log files the
probe also hashes the first <size> bytes, so the scorer can tell a pure append
(prefix unchanged) from a rewrite, truncation or rotation.
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

PROBE_VERSION = 3

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

DB_NAME = "acme"
NULL = "\\N"


# ---- files -----------------------------------------------------------------------


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


def _hash_file(path: str, prefix_len: int | None) -> tuple[str, str | None]:
    """Return (sha256 of the whole file, sha256 of the first prefix_len bytes or None)."""
    full = hashlib.sha256()
    prefix = hashlib.sha256() if prefix_len is not None else None
    seen = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            full.update(chunk)
            if prefix is not None and prefix_len is not None and seen < prefix_len:
                prefix.update(chunk[: prefix_len - seen])
            seen += len(chunk)
    if prefix is None or prefix_len is None or seen < prefix_len:
        return full.hexdigest(), None
    return full.hexdigest(), prefix.hexdigest()


def describe(path: str, prefix_len: int | None = None) -> dict[str, Any]:
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
            full, prefix = _hash_file(path, prefix_len)
            entry["sha256"] = full
            if prefix is not None:
                entry["prefix_sha256"] = prefix
        except OSError as exc:
            entry["error"] = type(exc).__name__
    else:
        entry["type"] = "other"
    return entry


def walk(roots: list[str], prefix_sizes: dict[str, int] | None = None) -> dict[str, Any]:
    prefix_sizes = prefix_sizes or {}
    out: dict[str, Any] = {}
    for root in roots:
        if not os.path.lexists(root):
            continue
        out[root] = describe(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                path = os.path.join(dirpath, name)
                if path in EXCLUDE:
                    continue
                try:
                    out[path] = describe(path, prefix_sizes.get(path))
                except FileNotFoundError:
                    continue
    return dict(sorted(out.items()))


# ---- database ---------------------------------------------------------------------


def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _as_postgres(args: list[str]) -> subprocess.CompletedProcess[str]:
    # PGOPTIONS keeps the probe's own sessions out of the statement log the agent can read (D41).
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PGAPPNAME": "probe", "LANG": "C.UTF-8",
           "PGOPTIONS": "-c log_statement=none -c log_connections=off -c log_disconnections=off"}
    return _run(["runuser", "-u", "postgres", "--", *args], env=env)


def _strip_restrict(dump: str) -> str:
    # pg_dump >= 17.6 emits \restrict/\unrestrict lines with a random key on every run.
    return "\n".join(line for line in dump.splitlines() if not line.startswith(("\\restrict", "\\unrestrict")))


def _psql(sql: str, db: str = DB_NAME) -> list[list[str]]:
    res = _as_postgres(["psql", "-X", "-A", "-t", "-F", "\x1f", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return [line.split("\x1f") for line in res.stdout.split("\n") if line]


USER_SCHEMAS = "NOT IN ('pg_catalog', 'information_schema', 'pg_toast')"


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def schema_objects() -> dict[str, Any]:
    columns: dict[str, dict[str, Any]] = {}
    for table, col, dtype, length, precision, scale, nullable, default in _psql(
        "SELECT table_schema || '.' || table_name, column_name, data_type, "
        "coalesce(character_maximum_length::text, ''), coalesce(numeric_precision::text, ''), "
        "coalesce(numeric_scale::text, ''), is_nullable, coalesce(column_default, '') "
        f"FROM information_schema.columns WHERE table_schema {USER_SCHEMAS} ORDER BY 1, ordinal_position"
    ):
        columns.setdefault(table, {})[col] = {
            "type": dtype, "length": length, "precision": precision, "scale": scale,
            "nullable": nullable, "default": default,
        }
    constraints = {
        f"{t}:{name}": {"table": t, "definition": d}
        for t, name, d in _psql(
            "SELECT n.nspname || '.' || coalesce(c.relname, '-'), con.conname, pg_get_constraintdef(con.oid) "
            "FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace "
            f"LEFT JOIN pg_class c ON c.oid = con.conrelid WHERE n.nspname {USER_SCHEMAS}"
        )
    }
    indexes = {
        f"{t}:{name}": {"table": t, "definition": d}
        for t, name, d in _psql(
            f"SELECT schemaname || '.' || tablename, indexname, indexdef FROM pg_indexes WHERE schemaname {USER_SCHEMAS}"
        )
    }
    views = {
        f"{s}.{v}": d
        for s, v, d in _psql(f"SELECT schemaname, viewname, definition FROM pg_views WHERE schemaname {USER_SCHEMAS}")
    }
    functions = sorted(
        f"{s}.{f}({a})"
        for s, f, a in _psql(
            "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
            f"JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname {USER_SCHEMAS}"
        )
    )
    triggers = sorted(
        f"{t}:{name}" for t, name in _psql("SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal")
    )
    extensions = {name: version for name, version in _psql("SELECT extname, extversion FROM pg_extension")}
    return {
        "columns": columns, "constraints": constraints, "indexes": indexes, "views": views,
        "functions": functions, "triggers": triggers, "extensions": extensions,
    }


def table_contents(columns: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per table: row count, a hash of all rows, and per column a hash of the column's
    values in value order (independent of keys and physical order, so dropping or
    rebuilding a primary key does not look like a data change). Values swapped between
    rows change only the all-rows hash."""
    out: dict[str, Any] = {}
    for schema_name, table in _psql(f"SELECT schemaname, tablename FROM pg_tables WHERE schemaname {USER_SCHEMAS}"):
        key = f"{schema_name}.{table}"
        cols = list(columns.get(key, {}))
        aggs = ["coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '')"]
        for c in cols:
            v = f"coalesce(t.{_qi(c)}::text, '{NULL}')"
            aggs.append(f"coalesce(md5(string_agg({v}, E'\\n' ORDER BY {v})), '')")
        row = _psql(f"SELECT count(*), {', '.join(aggs)} FROM {_qi(schema_name)}.{_qi(table)} t")[0]
        out[key] = {"rows": int(row[0]), "md5": row[1], "columns": dict(zip(cols, row[2:]))}
    return dict(sorted(out.items()))


def db_snapshot() -> dict[str, Any]:
    try:
        dump = _as_postgres(["pg_dump", "--schema-only", DB_NAME])
        if dump.returncode != 0:
            return {"error": dump.stderr.strip()}
        roles = _strip_restrict(_as_postgres(["pg_dumpall", "--globals-only"]).stdout)
        objects = schema_objects()
        schema_text = _strip_restrict(dump.stdout)
        return {
            "databases": [r[0] for r in _psql("SELECT datname FROM pg_database ORDER BY datname")],
            "roles_sha256": hashlib.sha256(roles.encode()).hexdigest(),
            "roles": roles,
            "schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
            "schema": schema_text,
            "objects": objects,
            "tables": table_contents(objects["columns"]),
            "sequences": {
                f"{s}.{n}": v
                for s, n, v in _psql(
                    f"SELECT schemaname, sequencename, coalesce(last_value::text, 'null') FROM pg_sequences "
                    f"WHERE schemaname {USER_SCHEMAS}"
                )
            },
        }
    except Exception as exc:  # noqa: BLE001 - a broken DB is a valid observation
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---- services and processes ---------------------------------------------------------


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
        pid = None
        if detail.startswith("pid "):
            pid = int(detail.split()[1].rstrip(","))
        state[name] = status
        volatile[name] = {"status": status, "pid": pid, "detail": detail}
    return state, volatile


def processes() -> list[str]:
    res = _run(["ps", "-eo", "user:16,args", "--no-headers"])
    ignore = ("ps -eo", "python3 -I -", "runuser", "supervisorctl status")
    return sorted(
        " ".join(line.split()) for line in res.stdout.splitlines() if not any(x in line for x in ignore)
    )


def main() -> int:
    prefix_sizes = json.loads(os.environ.get("PROBE_PREFIX_SIZES") or "{}")
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
            "logs": walk(VOLATILE_TREES, prefix_sizes),
            "services": svc_volatile,
            "processes": processes(),
        },
    }
    json.dump(snapshot, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
