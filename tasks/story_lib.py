"""Story layer (DECISIONS.md D37): make the server's history consistent with the ticket.

Prepended to tasks/<id>/story.py and piped into the container (root, `python3 -I - <phase>`).
Env STORY_ANCHOR = the container's time (epoch seconds) when setup began; every story
time is derived from it, so the history is always relative to "now".

Phases
  pre    before the task setup: shift order dates so the latest orders are from yesterday
  post   after the task setup: restart supervisord (process uptimes then match the logs),
         replace all pre-restart log content with synthetic history, shift install logs,
         backdate install files, write the nightly report history. Prints `FACTS {json}`.
  check  story-consistency assertions. Prints one JSON object.

Hooks a task's story.py may define (all optional):
  API_STATE                     "RUNNING" (default) or "FATAL" after the restart
  story_times(T)                -> dict of extra story datetimes (available as T.x["name"])
  traffic_end(T)                -> datetime: last moment the API served requests (default: restart)
  health_status(T, t)           -> 200 | 503 for monitoring /health at time t
  stats_status(T, t)            -> 200 | 500 for /api/stats/daily at time t
  orders_status(T, t)           -> 200 | 500 for /api/orders/<id> at time t
  pg_error(req)                 -> None | (statement index, error message) for a failing request's SQL
  extra_requests(T, rng)        -> list[Req] added to normal traffic
  write_app_history(T, kept)    -> custom app.log writer (T01); returns requests in the last 2 h
  supervisor_events(T)          -> list[(datetime, level, message)] between boot and restart
  pg_events(T)                  -> list[(datetime, line_without_timestamp)]
  app_events(T)                 -> list[(datetime, line_without_timestamp)] for app.log
  filter_kept(name, entries)    -> entries (drop/rewrite real post-restart log entries)
  rewrite_api_out(T, text)      -> new content of acme-api.out.log
  file_times(T)                 -> {path: datetime} story mtimes
  after_restart(T)              -> run right after the supervisord restart, before the history is written
  at_end(T)                     -> phase "final", after the consistency check: background jobs that would
                                   block the check (T06's lock), waits; returns extra facts
  final_assertions(T)           -> list[(name, ok, detail)] checked in phase "final"
  post_verify(T)                -> raise if the planted state is not as expected after restart
  story_facts(T)                -> extra hidden facts
  story_assertions(T)           -> list[(name, ok, detail)]
  report_failure(T, night)      -> None, or the log text of that night's failed report run (no CSV),
                                   or (seconds after the start, text)
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

UTC = timezone.utc
PG_SILENT = "-c log_statement=none -c log_connections=off -c log_disconnections=off"
APP_LOG = "/var/log/acme/app.log"
REPORT_LOG = "/var/log/acme/report.log"
SUP_LOG = "/var/log/supervisor/supervisord.log"
API_OUT = "/var/log/supervisor/acme-api.out.log"
PG_OUT = "/var/log/supervisor/postgresql.out.log"
PG_LOG = "/var/log/postgresql/postgresql.log"
REPORT_DIR = "/srv/acme/reports"
INSTALL_LOGS = ["/var/log/dpkg.log", "/var/log/alternatives.log", "/var/log/apt/history.log", "/var/log/apt/term.log"]
PG_STATEMENT_WINDOW = timedelta(hours=2)

APP_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}) ")
PG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.(\d{3}) UTC ")
INSTALL_TS = re.compile(r"(\d{4}-\d{2}-\d{2})( {1,2})(\d{2}:\d{2}:\d{2})")


# ---- time helpers -------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC)


def fmt_app(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S},{t.microsecond // 1000:03d}"


def fmt_pg(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} UTC"


def parse_m(m: re.Match[str]) -> datetime:
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(microsecond=int(m.group(2)) * 1000, tzinfo=UTC)


def at(day: date, hh: int, mm: int, ss: float = 0.0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC) + timedelta(seconds=ss)


@dataclass
class Times:
    anchor: datetime
    install: datetime
    prev_boot: datetime
    restart: datetime | None = None
    x: dict[str, datetime] = field(default_factory=dict)


def common_times(anchor: datetime) -> Times:
    install = at((anchor - timedelta(days=61)).date(), 10, 14, 7.0)
    prev_boot = at((anchor - timedelta(days=23)).date(), 4, 12, 31.412)
    return Times(anchor=anchor, install=install, prev_boot=prev_boot)


def hook(name: str) -> Callable[..., Any] | None:
    fn = globals().get(name)
    return fn if callable(fn) else None


def times() -> Times:
    t = common_times(datetime.fromtimestamp(float(os.environ["STORY_ANCHOR"]), UTC))
    if (fn := hook("story_times")) is not None:
        t.x.update(fn(t))
    T_CURRENT[:] = [t]
    return t


# ---- system helpers ------------------------------------------------------------------


def sh(cmd: list[str], check: bool = True, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    if check and res.returncode != 0:
        raise RuntimeError(f"{cmd}: {res.stderr.strip()}")
    return res.stdout


def psql(sql: str) -> list[list[str]]:
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PGAPPNAME": "setup", "LANG": "C.UTF-8", "PGOPTIONS": PG_SILENT}
    out = sh(["runuser", "-u", "postgres", "--", "psql", "-X", "-A", "-t", "-F", "\x1f", "-v", "ON_ERROR_STOP=1",
              "-d", "acme", "-c", sql], env=env)
    return [line.split("\x1f") for line in out.split("\n") if line]


def supervisor_status() -> dict[str, list[str]]:
    out = sh(["supervisorctl", "status"], check=False)
    return {p[0]: p[1:] for p in (ln.split() for ln in out.splitlines()) if p}


def set_mtime(path: str, t: datetime) -> None:
    if os.path.lexists(path):
        os.utime(path, (t.timestamp(), t.timestamp()), follow_symlinks=False)


def write_file(path: str, text: str, mtime: datetime | None = None, owner: tuple[str, str] | None = None,
               mode: int = 0o644) -> None:
    """Rewrite in place (same inode, so processes appending to it keep working)."""
    exists = os.path.exists(path)
    with open(path, "r+b" if exists else "wb") as fh:
        fh.truncate(0)
        fh.write(text.encode("utf-8"))
    if not exists and owner is not None:
        import grp
        import pwd
        os.chown(path, pwd.getpwnam(owner[0]).pw_uid, grp.getgrnam(owner[1]).gr_gid)
        os.chmod(path, mode)
    if mtime is not None:
        set_mtime(path, mtime)


def read_entries(path: str, rx: re.Pattern[str]) -> list[tuple[datetime | None, str]]:
    """Log entries: a timestamped line plus its continuation lines."""
    entries: list[list[Any]] = []
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh.read().splitlines():
            m = rx.match(line)
            if m:
                entries.append([parse_m(m), [line]])
            elif entries:
                entries[-1][1].append(line)
            else:
                entries.append([None, [line]])
    return [(t, "\n".join(lines)) for t, lines in entries]


def join(entries: list[tuple[datetime | None, str]]) -> str:
    return "".join(text + "\n" for _, text in entries)


# ---- synthetic traffic -------------------------------------------------------------------


@dataclass
class Req:
    t: datetime
    path: str
    kind: str              # health | orders | customers | products | products_cat | stats
    status: int
    ms: float
    params: list[str] = field(default_factory=list)
    warning: str = ""


CATEGORIES = ["bearings", "belts", "fasteners", "motors", "seals", "sensors"]
SQL = {
    "orders": [("SELECT id, customer_id, created_at, status, total FROM orders WHERE id = $1", 1),
               ("SELECT p.sku, i.quantity, i.unit_price FROM order_items i JOIN products p ON p.id = i.product_id "
                "WHERE i.order_id = $1 ORDER BY i.id", 1)],
    "customers": [("SELECT id, name, email, city, created_at FROM customers WHERE id = $1", 1)],
    "products": [("SELECT sku, name, category, price, stock FROM products ORDER BY sku LIMIT $1", 1)],
    "products_cat": [("SELECT sku, name, category, price, stock FROM products WHERE category = $1 ORDER BY sku LIMIT $2", 2)],
    "stats": [("SELECT orders, revenue FROM daily_totals WHERE day = $1::date", 1)],
    "customer_orders": [("SELECT 1 AS ok FROM customers WHERE id = $1", 1),
                        ("SELECT id, created_at, status, total FROM orders WHERE customer_id = $1 ORDER BY created_at DESC", 1)],
}


T_CURRENT: list[Times] = []  # the story's Times, for hooks called from the traffic generator


def business_req(t: datetime, rng: random.Random, stats_code: int) -> Req:
    r = rng.random()
    if r < 0.45:
        oid = str(rng.randrange(1, 6001))
        code = hook("orders_status")(T_CURRENT[0], t) if hook("orders_status") and T_CURRENT else 200
        return Req(t, f"/api/orders/{oid}", "orders", code, rng.uniform(1.2, 30.0), [oid])
    if r < 0.65:
        cid = str(rng.randrange(1, 501))
        return Req(t, f"/api/customers/{cid}", "customers", 200, rng.uniform(0.8, 9.0), [cid])
    if r < 0.82:
        return Req(t, "/api/products", "products", 200, rng.uniform(0.6, 6.0), ["50"])
    if r < 0.92:
        return Req(t, "/api/products", "products_cat", 200, rng.uniform(0.6, 6.0), [rng.choice(CATEGORIES), "50"])
    day = (t - timedelta(days=1)).date().isoformat()
    return Req(t, "/api/stats/daily", "stats", stats_code, rng.uniform(2.0, 12.0), [day, day])


def normal_traffic(T: Times, start: datetime, end: datetime, rng: random.Random) -> list[Req]:
    health_fn = hook("health_status")
    stats_fn = hook("stats_status")
    reqs: list[Req] = []
    # monitoring: /health every 5 minutes
    t = start.replace(second=0, microsecond=0) + timedelta(minutes=5 - start.minute % 5, seconds=3)
    while t < end:
        code = health_fn(T, t) if health_fn else 200
        jitter = timedelta(milliseconds=rng.randrange(0, 900))
        warning = "health check failed: {'database': 'ok', 'cache_dir': 'not writable'}" if code == 503 else ""
        reqs.append(Req(t + jitter, "/health", "health", code, rng.uniform(0.3, 6.0), [], warning))
        t += timedelta(minutes=5)
    # business traffic: busy on weekday office hours, quiet otherwise
    t = start
    while True:
        busy = t.weekday() < 5 and 8 <= t.hour < 19
        t += timedelta(seconds=rng.expovariate(1 / (45 if busy else 420)))
        if t >= end:
            break
        code = stats_fn(T, t) if stats_fn else 200
        reqs.append(business_req(t, rng, code))
    # finance dashboard: yesterday's stats every hour 08:05-18:05
    day = start.date()
    while day <= end.date():
        for hh in range(8, 19):
            t = at(day, hh, 5, rng.uniform(1, 40))
            if start < t < end:
                d = (day - timedelta(days=1)).isoformat()
                reqs.append(Req(t, "/api/stats/daily", "stats", stats_fn(T, t) if stats_fn else 200,
                                rng.uniform(2.0, 12.0), [d, d]))
        day += timedelta(days=1)
    if (fn := hook("extra_requests")) is not None:
        reqs.extend(fn(T, rng))
    return sorted((r for r in reqs if start < r.t < end), key=lambda r: r.t)


def logged_in_app_log(r: Req) -> bool:
    """An unhandled exception (HTTP 500) escapes the app's logging middleware: no app.log line,
    only a traceback in acme-api.out.log. Same as the real app."""
    return r.status != 500


LAST_TRAFFIC: list[Req] = []


def app_line(r: Req) -> str:
    lines = []
    if r.warning:
        lines.append(f"{fmt_app(r.t - timedelta(milliseconds=r.ms))} WARNING acme.api {r.warning}")
    lines.append(f"{fmt_app(r.t)} INFO acme.api GET {r.path} {r.status} {r.ms:.1f}ms")
    return "\n".join(lines)


def pg_request_lines(r: Req, pid: int) -> list[tuple[datetime, str]]:
    pre = f"[{pid}] acme@acme app=acme-api"
    t = r.t - timedelta(milliseconds=r.ms)
    out: list[tuple[datetime, str]] = []
    statements = [("SELECT 1 AS ok", 0)] if r.kind == "health" else SQL[r.kind]
    error = hook("pg_error")(r) if hook("pg_error") else None  # (statement index, message) of a failing query
    for n, (sql, nparams) in enumerate(statements):
        if error and n == error[0]:
            params = r.params
            detail = ", ".join(f"${i + 1} = '{params[i]}'" for i in range(nparams))
            plain = sql
            for i in range(nparams):
                plain = plain.replace(f"${i + 1}", f"${i + 1}")
            out.append((t, f"{pre} LOG:  statement: BEGIN"))
            out.append((t, f"{pre} LOG:  execute <unnamed>: {sql}\n{fmt_pg(t)} {pre} DETAIL:  Parameters: {detail}"))
            out.append((t + timedelta(microseconds=300), f"{pre} ERROR:  {error[1]}"))
            out.append((t + timedelta(microseconds=300), f"{pre} STATEMENT:  {plain}"))
            out.append((t + timedelta(microseconds=600), f"{pre} LOG:  statement: ROLLBACK"))
            break
        out.append((t, f"{pre} LOG:  statement: BEGIN"))
        if r.kind == "health":
            out.append((t, f"{pre} LOG:  statement: {sql}"))
        else:
            params = r.params if r.kind != "products" else ["50"]
            detail = ", ".join(f"${i + 1} = '{params[i]}'" for i in range(nparams))
            out.append((t, f"{pre} LOG:  execute <unnamed>: {sql}\n{fmt_pg(t)} {pre} DETAIL:  Parameters: {detail}"))
        out.append((t + timedelta(microseconds=400), f"{pre} LOG:  statement: COMMIT"))
        t += timedelta(microseconds=700)
    return out


# ---- synthetic service logs ------------------------------------------------------------------


def sup_boot(t0: datetime, pg_pid: int, api_pid: int) -> list[tuple[datetime, str]]:
    return [
        (t0, "INFO Set uid to user 0 succeeded"),
        (t0 + timedelta(milliseconds=2), "INFO RPC interface 'supervisor' initialized"),
        (t0 + timedelta(milliseconds=2), "CRIT Server 'unix_http_server' running without any HTTP authentication checking"),
        (t0 + timedelta(milliseconds=2), "INFO supervisord started with pid 1"),
        (t0 + timedelta(milliseconds=1005), f"INFO spawned: 'postgresql' with pid {pg_pid}"),
        (t0 + timedelta(milliseconds=1007), f"INFO spawned: 'acme-api' with pid {api_pid}"),
        (t0 + timedelta(milliseconds=2262), "INFO success: postgresql entered RUNNING state, process has stayed up for > than 1 seconds (startsecs)"),
        (t0 + timedelta(milliseconds=4506), "INFO success: acme-api entered RUNNING state, process has stayed up for > than 3 seconds (startsecs)"),
    ]


LISTEN_MSGS = ['listening on Unix socket "/var/run/postgresql/.s.PGSQL.5432"']  # replaced by the real boot's lines


def pg_boot(t0: datetime, pid: int, version_msg: str, shut_down_at: datetime) -> list[tuple[datetime, str]]:
    p = f"[{pid}] @ app="
    return [
        (t0, f"{p} LOG:  {version_msg}"),
        *[(t0 + timedelta(microseconds=600 + 150 * i), f"{p} LOG:  {msg}") for i, msg in enumerate(LISTEN_MSGS)],
        (t0 + timedelta(milliseconds=4), f"[{pid + 5}] @ app= LOG:  database system was shut down at {shut_down_at:%Y-%m-%d %H:%M:%S} UTC"),
        (t0 + timedelta(milliseconds=21), f"{p} LOG:  database system is ready to accept connections"),
    ]


# The server does not log connections (postgres-acme.conf), so the synthetic history has none.
LOG_CONNECTIONS = False


def pg_connect(t: datetime, pid: int, user: str, app: str, hba: str) -> list[tuple[datetime, str]]:
    if not LOG_CONNECTIONS:
        return []
    return [
        (t, f"[{pid}] [unknown]@[unknown] app=[unknown] LOG:  connection received: host=[local]"),
        (t + timedelta(milliseconds=1), f'[{pid}] {user}@acme app=[unknown] LOG:  connection authenticated: identity="{user}" method=peer ({hba})'),
        (t + timedelta(milliseconds=1), f"[{pid}] {user}@acme app=[unknown] LOG:  connection authorized: user={user} database=acme application_name={app}"),
    ]


def pg_disconnect(t: datetime, pid: int, user: str, app: str, since: datetime) -> list[tuple[datetime, str]]:
    if not LOG_CONNECTIONS:
        return []
    s = (t - since).total_seconds()
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return [(t, f"[{pid}] {user}@acme app={app} LOG:  disconnection: session time: {int(h)}:{int(m):02d}:{sec:06.3f} "
                f"user={user} database=acme host=[local]")]


def with_ts(items: list[tuple[datetime, str]], fmt: Callable[[datetime], str]) -> list[tuple[datetime | None, str]]:
    return [(t, f"{fmt(t)} {text}") for t, text in sorted(items, key=lambda x: x[0])]


# ---- phases --------------------------------------------------------------------------------------


# ---- file times (D42) ------------------------------------------------------------------
# The image itself is aged at build time (sandbox/build/age_image.py): no image file is newer
# than the install day, which is never later than T.install. What is left for runtime: the
# paths Docker writes when it creates the container, and /tmp.

LIVE_ROOTS = ("/proc", "/sys", "/dev", "/run")
BOOT_DATED = ("/.dockerenv", "/etc/resolv.conf", "/tmp")  # dated at the last boot by _runtime_file_times


def _walk_root(prune: tuple[str, ...], top: str = "/") -> Any:
    """Every path under `top` on the root filesystem (no other mounts), except the pruned trees."""
    root_dev = os.lstat("/").st_dev
    for dirpath, dirnames, filenames in os.walk(top):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if full in prune:
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if st.st_dev == root_dev:
                keep.append(d)
            yield full
        dirnames[:] = keep
        for name in filenames:
            yield os.path.join(dirpath, name)


def _runtime_file_times(T: Times) -> None:
    """Paths Docker writes at container creation, plus /tmp: dated as on a server installed at
    T.install and last booted at T.prev_boot."""
    for path, t in [("/", T.install + timedelta(seconds=2600)), ("/etc/hostname", T.install),
                    ("/etc/hosts", T.install), ("/etc/mtab", T.install), ("/etc/resolv.conf", T.prev_boot),
                    ("/.dockerenv", T.prev_boot), ("/tmp", T.prev_boot)]:
        try:
            set_mtime(path, t)
        except OSError:
            pass  # reported by the story check if it stays recent


def phase_pre() -> dict[str, Any]:
    T = times()
    yesterday = T.anchor.date() - timedelta(days=1)
    latest = date.fromisoformat(psql("SELECT max(created_at)::date FROM orders")[0][0])
    delta = (yesterday - latest).days
    if delta:
        psql(f"UPDATE orders SET created_at = created_at + make_interval(days => {delta})")
        psql(f"UPDATE customers SET created_at = created_at + make_interval(days => {delta})")
    return {"order_dates_shift_days": delta}


def _restart_supervisord(T: Times) -> None:
    T.restart = now()
    sh(["supervisorctl", "reload"])
    want = globals().get("API_STATE", "RUNNING")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = supervisor_status()
        if st.get("postgresql", [""])[0] == "RUNNING" and st.get("acme-api", [""])[0] == want:
            if want == "RUNNING":
                time.sleep(0.5)
            return
        time.sleep(0.5)
    raise RuntimeError(f"after restart: {supervisor_status()}")


def _report_history(T: Times) -> list[tuple[datetime, str]]:
    """Nightly report runs (cron, 02:30) for the 14 nights before the anchor."""
    lines: list[tuple[datetime, str]] = []
    newest: datetime | None = None
    rng = random.Random(7)
    for back in range(14, -1, -1):
        night = T.anchor.date() - timedelta(days=back)
        run_t = at(night, 2, 30, 0.8 + rng.random())
        if run_t >= T.anchor:
            continue
        day = (night - timedelta(days=1)).isoformat()
        failure = hook("report_failure")(T, night) if hook("report_failure") else None
        if failure:  # the job ran and failed: its log output (may be a multi-line traceback), no CSV
            delay, text = failure if isinstance(failure, tuple) else (0.15 + rng.randrange(400) / 1000, failure)
            lines.append((run_t, "INFO report run started (trigger=cron)"))
            lines.append((run_t + timedelta(seconds=delay), text))
            continue
        rows = psql(
            "SELECT p.category, count(DISTINCT o.id), sum(i.quantity * i.unit_price) FROM orders o "
            "JOIN order_items i ON i.order_id = o.id JOIN products p ON p.id = i.product_id "
            f"WHERE o.created_at >= DATE '{day}' AND o.created_at < DATE '{day}' + 1 AND o.status <> 'cancelled' "
            "GROUP BY p.category ORDER BY p.category"
        )
        csv = "day,category,orders,revenue\r\n" + "".join(f"{day},{c},{n},{float(r):.2f}\r\n" for c, n, r in rows)
        path = f"{REPORT_DIR}/daily-{day}.csv"
        done = run_t + timedelta(milliseconds=180 + rng.randrange(200))
        write_file(path, csv, mtime=done, owner=("acme", "acme"))
        lines.append((run_t, "INFO report run started (trigger=cron)"))
        lines.append((done, f"INFO report written: {path} ({len(rows)} categories)"))
        newest = done
    if newest:
        set_mtime(REPORT_DIR, newest)
    return lines


INSTALL_PATHS = [
    "/etc/acme", "/etc/acme/app.yaml", "/etc/cron.d/acme-report", "/etc/cron.d", "/etc/logrotate.d/acme", "/etc/logrotate.d",
    "/etc/supervisor", "/etc/supervisor/supervisord.conf", "/etc/sudoers.d", "/etc/sudoers.d/ops", "/etc/pam.d/cron",
    "/etc/passwd", "/etc/passwd-", "/etc/group", "/etc/group-", "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-",
    "/etc/subuid", "/etc/subgid", "/etc/cron.daily", "/etc/cron.weekly", "/etc/cron.monthly",
    "/opt/acme", "/opt/acme/requirements.txt", "/opt/acme/venv", "/usr/local/bin/acme-healthcheck",
    "/usr/local/sbin/entrypoint.sh", "/usr/share/doc/acme-build-packages.txt", "/srv", "/srv/acme", "/var/cache/acme",
    "/home/ops", "/home/ops/README.md", "/var/log/acme", "/var/log/supervisor", "/var/log/postgresql", "/var/log",
]
INSTALL_TREES = ["/opt/acme/app", "/opt/acme/bin", "/opt/acme/db", "/etc/postgresql"]


def _backdate_install(T: Times) -> None:
    paths = list(INSTALL_PATHS)
    for root in INSTALL_TREES:
        for dirpath, dirnames, filenames in os.walk(root):
            paths += [dirpath] + [os.path.join(dirpath, n) for n in filenames + dirnames]
    for p in sorted(set(paths)):
        offset = int(hashlib.sha256(p.encode()).hexdigest()[:6], 16) % 2400
        set_mtime(p, T.install + timedelta(seconds=offset))
    set_mtime("/etc", T.install + timedelta(seconds=2500))


def phase_post() -> dict[str, Any]:
    T = times()
    rng = random.Random(int(T.anchor.timestamp()) // 86400)
    # Templates from the real logs of the container's first start (discarded below).
    first_sup = read_entries(SUP_LOG, APP_TS)
    real_pids = {name: int(m.group(1)) for _, text in first_sup
                 for name, m in [(n, re.search(rf"spawned: '{n}' with pid (\d+)", text)) for n in ("postgresql", "acme-api")] if m}
    first_pg = read_entries(PG_LOG, PG_TS)
    listen = [x.split("LOG:  ", 1)[1] for _, x in first_pg if "LOG:  listening on" in x]
    if listen:
        LISTEN_MSGS[:] = list(dict.fromkeys(listen))
    hba = next((m.group(1) for _, x in first_pg if (m := re.search(r"method=peer \(([^)]+)\)", x))), "/etc/postgresql/17/main/pg_hba.conf:123")
    app_conn_pid = next((int(m.group(1)) for _, x in first_pg if "application_name=acme-api" in x and (m := re.search(r"\[(\d+)\]", x))), 28)

    conn_t = T.prev_boot + timedelta(milliseconds=1250)
    globals().update(HBA=hba, APP_CONN_PID=app_conn_pid, APP_CONN_T=conn_t)
    _restart_supervisord(T)
    if (fn := hook("after_restart")) is not None:
        fn(T)  # e.g. start a background job the ticket's story needs (T06); real log lines it causes are kept
    if (fn := hook("post_verify")) is not None:
        fn(T)

    kept = {}
    for name, path, rx in [("sup", SUP_LOG, APP_TS), ("pg", PG_LOG, PG_TS), ("pg_out", PG_OUT, PG_TS),
                           ("app", APP_LOG, APP_TS), ("report", REPORT_LOG, APP_TS)]:
        entries = [(t, x) for t, x in read_entries(path, rx) if t is not None and t >= T.restart]
        if (fn := hook("filter_kept")) is not None:
            entries = fn(name, entries)
        kept[name] = entries
    version = next((x.split("LOG:  ", 1)[1] for _, x in kept["pg"] + first_pg if "LOG:  starting PostgreSQL" in x), "starting PostgreSQL")
    # The API's real connection (opened at container start) closes at the restart; in the story
    # it is the connection opened at the last boot (or at a later API restart, see app_connection).
    cur_pid, cur_since = hook("app_connection")(T) if hook("app_connection") else (app_conn_pid, conn_t)
    fixed = []
    for t, x in kept["pg"]:
        if t is not None and f"[{app_conn_pid}] acme@acme app=acme-api LOG:  disconnection" in x and LOG_CONNECTIONS:
            x = f"{fmt_pg(t)} {pg_disconnect(t, cur_pid, 'acme', 'acme-api', cur_since)[0][1]}"
        fixed.append((t, x))
    kept["pg"] = fixed
    pg_pid, api_pid = real_pids.get("postgresql", 15), real_pids.get("acme-api", 16)

    # supervisord
    sup = sup_boot(T.prev_boot, pg_pid, api_pid)
    if (fn := hook("supervisor_events")) is not None:
        sup += [(t, f"{lvl} {msg}") for t, lvl, msg in fn(T)]
    write_file(SUP_LOG, join(with_ts(sup, fmt_app)) + join(kept["sup"]))

    # app.log (+ the requests whose statements go into the Postgres log)
    if (fn := hook("write_app_history")) is not None:
        recent = fn(T, kept["app"], rng)
    else:
        end = hook("traffic_end")(T) if hook("traffic_end") else T.restart - timedelta(seconds=2)
        reqs = normal_traffic(T, T.prev_boot + timedelta(seconds=2), end, rng)
        LAST_TRAFFIC[:] = reqs
        events = [(T.prev_boot + timedelta(milliseconds=1240), "INFO acme.api api started (pool_size=5)")]
        if (efn := hook("app_events")) is not None:
            events += efn(T)
        lines = sorted([(r.t, app_line(r)) for r in reqs if logged_in_app_log(r)]
                       + [(t, f"{fmt_app(t)} {x}") for t, x in events], key=lambda z: z[0])
        last = lines[-1][0] if lines else T.prev_boot
        write_file(APP_LOG, "".join(x + "\n" for _, x in lines) + join(kept["app"]),
                   mtime=None if kept["app"] else last)
        recent = [r for r in reqs if r.t >= T.restart - PG_STATEMENT_WINDOW]

    # Postgres
    shut = T.prev_boot - timedelta(seconds=37)
    pg = pg_boot(T.prev_boot + timedelta(milliseconds=1031), pg_pid, version, shut)
    pg += pg_connect(conn_t, app_conn_pid, "acme", "acme-api", hba)
    if (fn := hook("pg_events")) is not None:
        pg += fn(T)
    pid_for = hook("app_pg_pid")
    for r in recent:
        pg += pg_request_lines(r, pid_for(T, r.t) if pid_for else app_conn_pid)
    if (fn := hook("pg_disconnects")) is not None:
        pg += fn(T, app_conn_pid, conn_t)
    # multi-line items (execute + DETAIL) carry their own continuation timestamp
    pg_items = sorted(pg, key=lambda z: z[0])
    pg_text = "".join(f"{fmt_pg(t)} {x}\n" for t, x in pg_items)
    write_file(PG_LOG, pg_text + join(kept["pg"]))
    write_file(PG_OUT, join(with_ts([
        (T.prev_boot + timedelta(milliseconds=1031), f"[{pg_pid}] @ app= LOG:  redirecting log output to logging collector process"),
        (T.prev_boot + timedelta(milliseconds=1031), f'[{pg_pid}] @ app= HINT:  Future log output will appear in directory "/var/log/postgresql".'),
    ], fmt_pg)) + join(kept["pg_out"]))

    # acme-api stdout/stderr (uvicorn has no timestamps)
    if (fn := hook("rewrite_api_out")) is not None:
        write_file(API_OUT, fn(T, open(API_OUT, encoding="utf-8", errors="replace").read()))

    # nightly report history
    report_lines = _report_history(T)
    write_file(REPORT_LOG, join(with_ts(report_lines, fmt_app)) + join(kept["report"]),
               mtime=None if kept["report"] else (report_lines[-1][0] if report_lines else None),
               owner=("acme", "adm"))

    _backdate_install(T)
    if (fn := hook("file_times")) is not None:
        for path, t in fn(T).items():
            set_mtime(path, t)
    set_mtime("/var/log/acme", T.install + timedelta(days=1, minutes=3))
    _runtime_file_times(T)
    facts: dict[str, Any] = {"story_anchor": T.anchor.isoformat(), "story_restart": T.restart.isoformat(),
                             "story_times": {k: v.isoformat() for k, v in T.x.items()}}
    if (fn := hook("story_facts")) is not None:
        facts.update(fn(T))
    return facts


# ---- consistency check --------------------------------------------------------------------------


def _entries_ts(path: str, rx: re.Pattern[str]) -> list[datetime]:
    """Timestamps of the entries, streamed (app.log can be 150 MB)."""
    out: list[datetime] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = rx.match(line)
            if m:
                out.append(parse_m(m))
    return out


def common_assertions(T: Times) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    current = now() + timedelta(seconds=2)
    logs = [(APP_LOG, APP_TS), (REPORT_LOG, APP_TS), (SUP_LOG, APP_TS), (PG_LOG, PG_TS), (PG_OUT, PG_TS)]
    for path, rx in logs:
        ts = _entries_ts(path, rx)
        name = os.path.basename(path)
        out.append((f"{name}: no future timestamps", all(t <= current for t in ts), f"latest {max(ts) if ts else None}"))
        inversions = sum(1 for a, b in zip(ts, ts[1:]) if b < a - timedelta(seconds=2))
        out.append((f"{name}: chronological", inversions == 0, f"{inversions} inversions"))
    for path, rx, depth in [(SUP_LOG, APP_TS, 7), (PG_LOG, PG_TS, 7), (APP_LOG, APP_TS, 3)]:
        ts = _entries_ts(path, rx)
        if path == APP_LOG:  # history may continue in uncompressed rotated copies (app.log.1, ...)
            rotated = sorted(glob.glob(APP_LOG + ".[0-9]*"), key=lambda p: int(p.rsplit(".", 1)[1]) if p.rsplit(".", 1)[1].isdigit() else 0)
            for older in rotated:
                if older.rsplit(".", 1)[1].isdigit():
                    first = _entries_ts(older, rx)[:1]
                    ts = first + ts if first and (not ts or first[0] < ts[0]) else ts
        ok = bool(ts) and ts[0] <= T.anchor - timedelta(days=depth)
        out.append((f"{os.path.basename(path)}: history >= {depth} days", ok, f"first {ts[0] if ts else None}"))
    pg = open(PG_LOG, encoding="utf-8", errors="replace").read()
    traces = [x for x in ("app=probe", "app=setup", "app=check", "app=pg_dump", "FACTS") if x in pg]
    out.append(("no setup/probe/check traces in the Postgres log", not traces, str(traces)))
    tmp = sorted(os.listdir("/tmp"))
    out.append(("no setup leftovers in /tmp", not tmp, str(tmp)))
    # process uptimes match the last supervisord start in the log
    st = supervisor_status()
    starts = [t for t, x in read_entries(SUP_LOG, APP_TS) if t and "spawned: 'postgresql'" in x]
    up = st.get("postgresql", [])
    ok = False
    if starts and "uptime" in up and up[0] == "RUNNING":
        h, m, s = (int(v) for v in up[up.index("uptime") + 1].split(":"))
        started = now() - timedelta(hours=h, minutes=m, seconds=s)
        ok = abs((started - starts[-1]).total_seconds()) <= 5
    out.append(("postgres uptime matches its last start in supervisord.log", ok, f"uptime {up}, logged {starts[-1] if starts else None}"))
    want = globals().get("API_STATE", "RUNNING")
    out.append((f"acme-api is {want}", st.get("acme-api", [""])[0] == want, str(st.get("acme-api"))))
    latest = psql("SELECT max(created_at)::date FROM orders")[0][0]
    out.append(("latest orders are from yesterday", latest == (T.anchor.date() - timedelta(days=1)).isoformat(), latest))
    reports = sorted(f for f in os.listdir(REPORT_DIR) if f.startswith("daily-"))
    out.append(("nightly report history (>= 7 reports)", len(reports) >= 7, f"{len(reports)} reports, newest {reports[-1] if reports else None}"))
    old = T.anchor - timedelta(days=30)
    story_dated = set(hook("file_times")(T)) if hook("file_times") else set()  # e.g. files a deploy replaced
    for p in ["/opt/acme/app/run.py", "/opt/acme/bin/nightly_report.py", "/etc/acme", "/usr/local/bin/acme-healthcheck",
              "/etc/cron.d/acme-report"]:
        if p in story_dated:
            continue
        mt = datetime.fromtimestamp(os.lstat(p).st_mtime, UTC)
        out.append((f"{p} installed >= 30 days ago", mt <= old, str(mt)))
    out += _file_time_assertions(T, story_dated)
    dpkg = _entries_ts_install("/var/log/dpkg.log")
    out.append(("dpkg.log entries >= 30 days old", bool(dpkg) and max(dpkg) <= old, f"latest {max(dpkg) if dpkg else None}"))
    return out


# Areas whose files are legitimately recent: kernel/runtime trees, scratch, logs (written by the
# story with their own dates), live database files, the nightly reports (story-dated).
RECENT_OK = LIVE_ROOTS + ("/tmp", "/var/log", "/var/lib/postgresql", REPORT_DIR)


def _file_time_assertions(T: Times, story_dated: set[str]) -> list[tuple[str, bool, str]]:
    """D42: no path outside RECENT_OK carries a date later than the install (an image-build or
    setup date would give the environment away), except files the story dates itself; the
    app's own bytecode cache is written when the API starts. Database files are either from the
    install or written since the last start."""
    late = (T.install + timedelta(hours=1)).timestamp()
    boot = T.prev_boot.timestamp()

    def mtime(path: str) -> float | None:
        try:
            return os.lstat(path).st_mtime
        except OSError:
            return None

    offenders = [p for p in ["/", *_walk_root(RECENT_OK)]
                 if p not in story_dated and p not in BOOT_DATED
                 and not (p.startswith("/opt/acme/") and "/__pycache__" in p)
                 and (mtime(p) or 0) > late]
    pg_gap = [p for p in _walk_root((), "/var/lib/postgresql") if late < (mtime(p) or 0) < boot]
    tmp_mt = datetime.fromtimestamp(mtime("/tmp") or 0, UTC)
    return [
        ("no file dated after the install outside logs, reports, live DB files and story-dated files",
         not offenders, f"{len(offenders)}: {offenders[:8]}"),
        ("database files are from the install or from since the last boot", not pg_gap, f"{len(pg_gap)}: {pg_gap[:8]}"),
        ("/tmp is dated at the last boot", abs((tmp_mt - T.prev_boot).total_seconds()) < 2, str(tmp_mt)),
    ]


def _entries_ts_install(path: str) -> list[datetime]:
    if not os.path.exists(path):
        return []
    return [datetime.strptime(f"{m.group(1)} {m.group(3)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            for m in INSTALL_TS.finditer(open(path, encoding="utf-8", errors="replace").read())]


def app_requests() -> list[tuple[datetime, str, int]]:
    """(time, path, status) of every request line in app.log."""
    out = []
    rx = re.compile(r" GET (\S+) (\d{3}) ")
    for t, x in read_entries(APP_LOG, APP_TS):
        m = rx.search(x)
        if t and m:
            out.append((t, m.group(1), int(m.group(2))))
    return out


def phase_check() -> dict[str, Any]:
    T = times()
    results = common_assertions(T)
    if (fn := hook("story_assertions")) is not None:
        results += fn(T)
    return {"ok": all(ok for _, ok, _ in results),
            "assertions": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]}


def phase_final() -> dict[str, Any]:
    """After the consistency check: start what would block the check itself (T06's lock) and
    verify it. Prints {"facts": ..., "assertions": [...]}."""
    T = times()
    facts: dict[str, Any] = {}
    if (fn := hook("at_end")) is not None:
        facts = fn(T) or {}
    results = hook("final_assertions")(T) if hook("final_assertions") else []
    return {"facts": facts, "assertions": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results]}


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase == "pre":
        print("FACTS " + json.dumps(phase_pre()))
    elif phase == "post":
        print("FACTS " + json.dumps(phase_post()))
    elif phase == "check":
        print(json.dumps(phase_check()))
    elif phase == "final":
        print(json.dumps(phase_final()))
    else:
        raise SystemExit(f"unknown phase {phase!r}")


# ---------------------------------------------------------------- task story hooks below
