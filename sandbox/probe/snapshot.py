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
Optional input (env PROBE_TAIL_OFFSETS, JSON {path: offset}): the text appended to those
files after <offset> is returned in volatile.log_tails (the Postgres statement log, D47).
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

PROBE_VERSION = 4

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
ROW_LEVEL_MAX = 2000      # tables up to this size get a per-row, per-column hash map (D47)
BIG_TABLE = 50000         # above this, column hashes are order-independent sums (fast)
TAIL_CAP = 8 << 20
CRONTABS = ["/etc/crontab", "/etc/cron.d", "/var/spool/cron/crontabs"]


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
    # lock_timeout: a table another session holds an exclusive lock on is recorded as locked
    # instead of blocking the probe (T06, D50).
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PGAPPNAME": "probe", "LANG": "C.UTF-8",
           "PGOPTIONS": "-c log_statement=none -c log_connections=off -c log_disconnections=off -c lock_timeout=3000"}
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
    # Catalog only (no pg_get_expr on a relation): never waits for a lock another session holds
    # on a table (T06, D50). The type includes length, precision and scale; defaults are hashed.
    for table, col, dtype, length, precision, scale, nullable, default in _psql(
        "SELECT n.nspname || '.' || c.relname, a.attname, format_type(a.atttypid, a.atttypmod), '', '', '', "
        "CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, coalesce(md5(d.adbin::text), '') "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        f"WHERE c.relkind IN ('r', 'v', 'p', 'm', 'f') AND a.attnum > 0 AND NOT a.attisdropped AND n.nspname {USER_SCHEMAS} "
        "ORDER BY 1, a.attnum"
    ):
        columns.setdefault(table, {})[col] = {
            "type": dtype, "length": length, "precision": precision, "scale": scale,
            "nullable": nullable, "default": default,
        }
    # Definitions come from raw catalog fields, not from pg_get_*def(): those open the table and
    # would wait for another session's exclusive lock (T06, D50).
    constraints = {
        f"{t}:{name}": {"table": t, "definition": d}
        for t, name, d in _psql(
            "SELECT n.nspname || '.' || coalesce(c.relname, '-'), con.conname, "
            "con.contype::text || ' cols=' || coalesce(con.conkey::text, '') "
            "|| ' ref=' || coalesce(nullif(con.confrelid, 0)::regclass::text, '') || coalesce(con.confkey::text, '') "
            "|| ' on_update=' || con.confupdtype::text || ' on_delete=' || con.confdeltype::text "
            "|| ' deferrable=' || con.condeferrable::text || ' expr=' || coalesce(md5(con.conbin::text), '') "
            "FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace "
            f"LEFT JOIN pg_class c ON c.oid = con.conrelid WHERE n.nspname {USER_SCHEMAS}"
        )
    }
    indexes = {
        f"{t}:{name}": {"table": t, "definition": d}
        for t, name, d in _psql(
            "SELECT n.nspname || '.' || t.relname, i.relname, "
            "am.amname || ' unique=' || x.indisunique::text || ' primary=' || x.indisprimary::text "
            "|| ' keys=' || x.indkey::text || ' opclass=' || x.indclass::text "
            "|| ' expr=' || coalesce(md5(x.indexprs::text), '') || ' pred=' || coalesce(md5(x.indpred::text), '') "
            "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_am am ON am.oid = i.relam "
            f"WHERE n.nspname {USER_SCHEMAS}"
        )
    }
    views = {
        f"{s}.{v}": d
        for s, v, d in _psql(
            "SELECT n.nspname, c.relname, md5(r.ev_action::text) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_rewrite r ON r.ev_class = c.oid AND r.rulename = '_RETURN' "
            f"WHERE c.relkind IN ('v', 'm') AND n.nspname {USER_SCHEMAS}"
        )
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


def _primary_keys() -> dict[str, str]:
    return {t: col for t, col in _psql(
        "SELECT n.nspname || '.' || c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0] "
        f"WHERE i.indisprimary AND i.indnatts = 1 AND n.nspname {USER_SCHEMAS}")}


def table_contents(columns: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per table: row count, a hash of all rows, and per column a hash of the column's
    values in value order (independent of keys and physical order, so dropping or
    rebuilding a primary key does not look like a data change). Values swapped between
    rows change only the all-rows hash. Small tables with a one-column primary key also
    get `rowmap` {key: [8-hex hash per column]}, so deleted, inserted and updated rows can
    be told apart (D47). Big tables use order-independent sums of 64-bit hashes."""
    out: dict[str, Any] = {}
    pks = _primary_keys()
    for schema_name, table in _psql(f"SELECT schemaname, tablename FROM pg_tables WHERE schemaname {USER_SCHEMAS}"):
        key = f"{schema_name}.{table}"
        rel = f"{_qi(schema_name)}.{_qi(table)}"
        cols = list(columns.get(key, {}))
        try:
            count = int(_psql(f"SELECT count(*) FROM {rel}")[0][0])
        except RuntimeError as exc:
            if "lock timeout" not in str(exc):
                raise
            out[key] = {"locked": True}
            continue
        values = [f"coalesce(t.{_qi(c)}::text, '{NULL}')" for c in cols]
        if count > BIG_TABLE:
            aggs = ["coalesce(sum(hashtextextended(t::text, 0))::text, '')"]
            aggs += [f"coalesce(sum(hashtextextended({v}, 0))::text, '')" for v in values]
        else:
            aggs = ["coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '')"]
            aggs += [f"coalesce(md5(string_agg({v}, E'\\n' ORDER BY {v})), '')" for v in values]
        row = _psql(f"SELECT count(*), {', '.join(aggs)} FROM {rel} t")[0]
        entry: dict[str, Any] = {"rows": int(row[0]), "md5": row[1], "columns": dict(zip(cols, row[2:]))}
        if count <= ROW_LEVEL_MAX and key in pks and cols:
            pk = pks[key]
            hashed = ", ".join(f"left(md5({v}), 8)" for v in values)
            entry["rowmap"] = {"key": pk, "columns": cols,
                               "rows": {r[0]: r[1:] for r in _psql(f"SELECT t.{_qi(pk)}::text, {hashed} FROM {rel} t")}}
        out[key] = entry
    return dict(sorted(out.items()))


def privileges() -> dict[str, list[str]]:
    """ACL items per database, schema and relation, e.g. 'acme=r/postgres' (D47)."""
    out: dict[str, list[str]] = {}
    for name, acl in _psql("SELECT datname, coalesce(array_to_string(datacl, ','), '') FROM pg_database"):
        out[f"database:{name}"] = sorted(a for a in acl.split(",") if a)
    for name, acl in _psql(f"SELECT nspname, coalesce(array_to_string(nspacl, ','), '') FROM pg_namespace WHERE nspname {USER_SCHEMAS}"):
        out[f"schema:{name}"] = sorted(a for a in acl.split(",") if a)
    for name, acl in _psql(
        "SELECT n.nspname || '.' || c.relname, coalesce(array_to_string(c.relacl, ','), '') FROM pg_class c "
        f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'v', 'm', 'S', 'p') AND n.nspname {USER_SCHEMAS}"
    ):
        out[name] = sorted(a for a in acl.split(",") if a)
    return dict(sorted(out.items()))


def roles_detail() -> dict[str, Any]:
    roles: dict[str, Any] = {}
    for row in _psql(
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, "
        "rolbypassrls, rolconnlimit, (rolpassword IS NOT NULL), coalesce((SELECT string_agg(array_to_string(s.setconfig, ','), ',') "
        "FROM pg_db_role_setting s WHERE s.setrole = a.oid), '') "
        "FROM pg_authid a WHERE rolname NOT LIKE 'pg\\_%' ORDER BY rolname"
    ):
        name, *attrs = row
        roles[name] = dict(zip(["superuser", "inherit", "createrole", "createdb", "login", "replication",
                                "bypassrls", "connlimit", "has_password", "config"], attrs))
        roles[name]["member_of"] = []
    for member, group in _psql("SELECT m.rolname, g.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member "
                               "JOIN pg_roles g ON g.oid = a.roleid"):
        if member in roles:
            roles[member]["member_of"].append(group)
    return roles


def db_sessions() -> dict[str, list[str]]:
    """Which database role each client application is connected as (volatile)."""
    try:
        rows = _psql("SELECT coalesce(application_name, ''), usename FROM pg_stat_activity "
                     "WHERE backend_type = 'client backend' AND application_name <> 'probe'")
    except Exception:  # noqa: BLE001 - database down
        return {}
    out: dict[str, set[str]] = {}
    for app, user in rows:
        out.setdefault(app, set()).add(user)
    return {k: sorted(v) for k, v in sorted(out.items())}


def db_snapshot() -> dict[str, Any]:
    try:
        dump = _as_postgres(["pg_dump", "--schema-only", "--lock-wait-timeout=3000", DB_NAME])
        if dump.returncode != 0 and "lock table" not in dump.stderr.lower():
            return {"error": dump.stderr.strip()}
        roles = _strip_restrict(_as_postgres(["pg_dumpall", "--globals-only"]).stdout)
        objects = schema_objects()
        schema_text = _strip_restrict(dump.stdout) if dump.returncode == 0 else "locked"
        return {
            "databases": [r[0] for r in _psql("SELECT datname FROM pg_database ORDER BY datname")],
            "roles_sha256": hashlib.sha256(roles.encode()).hexdigest(),
            "roles": roles,
            "roles_detail": roles_detail(),
            "privileges": privileges(),
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


def cron_entries() -> list[dict[str, str]]:
    """Active (uncommented) crontab lines: schedule, user (system crontabs) and command."""
    files: list[str] = []
    for path in CRONTABS:
        if os.path.isdir(path):
            files += [os.path.join(path, n) for n in sorted(os.listdir(path)) if "." not in n and not n.startswith("~")]
        elif os.path.isfile(path):
            files.append(path)
    out = []
    for path in files:
        system = not path.startswith("/var/spool/cron/crontabs/")
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", text):
                continue
            parts = text.split()
            n = 1 if parts[0].startswith("@") else 5
            if len(parts) <= n + (1 if system else 0):
                continue
            user = parts[n] if system else os.path.basename(path)
            command = " ".join(parts[n + (1 if system else 0):])
            out.append({"file": path, "schedule": " ".join(parts[:n]), "user": user, "command": command,
                        "backup": "yes" if _makes_backup(command) else "no"})
    return out


BACKUP_WORDS = re.compile(r"pg_dump|pg_basebackup|pg_dumpall|\bbackups?\b", re.I)


def _makes_backup(command: str) -> bool:
    """Whether a cron command makes backups: the command line or the script it runs (D48)."""
    if BACKUP_WORDS.search(command):
        return True
    for token in command.split():
        if token.startswith("/") and os.path.isfile(token) and os.path.getsize(token) <= 65536:
            try:
                if BACKUP_WORDS.search(open(token, encoding="utf-8", errors="replace").read()):
                    return True
            except OSError:
                pass
    return False


def log_tails(offsets: dict[str, int]) -> dict[str, str]:
    out = {}
    for path, offset in offsets.items():
        try:
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                fh.seek(offset if offset <= size else 0)
                out[path] = fh.read(TAIL_CAP).decode("utf-8", "replace")
        except OSError:
            out[path] = ""
    return out


def main() -> int:
    prefix_sizes = json.loads(os.environ.get("PROBE_PREFIX_SIZES") or "{}")
    tail_offsets = json.loads(os.environ.get("PROBE_TAIL_OFFSETS") or "{}")
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
            "cron": cron_entries(),
        },
        "volatile": {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "logs": walk(VOLATILE_TREES, prefix_sizes),
            "services": svc_volatile,
            "processes": processes(),
            "db_sessions": db_sessions(),
            "log_tails": log_tails(tail_offsets),
        },
    }
    json.dump(snapshot, sys.stdout, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
