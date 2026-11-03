# T07 story: the database maintenance at 02:00 (dump + VACUUM FULL) usually ends by 02:30,
# but on three long nights last week orders was still locked when the report ran at 02:30.
MAINT_LOG = "/var/log/postgresql/db-maintenance.log"
BACKUPS = "/srv/acme/backups"
# minutes per night, oldest first (14 nights); the last 7 have three nights > 31 min
DURATIONS = [27, 29, 26, 30, 28, 25, 29, 47, 29, 52, 30, 28, 44, 29]


def _nights(T):  # type: ignore[no-untyped-def]
    out = []
    for back, dur in zip(range(14, 0, -1), DURATIONS):
        night = T.anchor.date() - timedelta(days=back - 1)
        if at(night, 2, 0) + timedelta(minutes=dur) < T.anchor:
            out.append((night, dur))
    return out


def _plan(night, dur):  # type: ignore[no-untyped-def]
    """Step times of one maintenance night: dump, then VACUUM FULL orders, order_items, shipment_events."""
    start = at(night, 2, 0, 1.2)
    dump_done = start + timedelta(minutes=3, seconds=14 + dur % 7)
    end = start + timedelta(minutes=dur, seconds=37)
    orders_end = end - timedelta(minutes=8, seconds=12)
    items_end = end - timedelta(minutes=3, seconds=40)
    return start, dump_done, orders_end, items_end, end


def _failed(T, night):  # type: ignore[no-untyped-def]
    return any(n == night and _plan(n, d)[2] > at(night, 2, 31) for n, d in _nights(T))


def _traceback() -> str:
    def line_of(path: str, needle: str, after: int = 0) -> int:
        lines = open(path, encoding="utf-8").read().splitlines()
        return next(i + 1 for i, ln in enumerate(lines) if i + 1 > after and needle in ln)
    rep = "/opt/acme/bin/nightly_report.py"
    conn = "/opt/acme/venv/lib/python3.12/site-packages/psycopg/connection.py"
    lines = open(conn, encoding="utf-8").read().splitlines()
    last_def = max(i + 1 for i, ln in enumerate(lines) if ln.strip().startswith("def execute("))
    return ("Traceback (most recent call last):\n"
            f'  File "{rep}", line {line_of(rep, "sys.exit(main())")}, in <module>\n'
            "    sys.exit(main())\n"
            "             ^^^^^^\n"
            f'  File "{rep}", line {line_of(rep, "rows = conn.execute(")}, in main\n'
            "    rows = conn.execute(SQL_BY_CATEGORY, (day, day)).fetchall()\n"
            "           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
            f'  File "{conn}", line {line_of(conn, "raise ex.with_traceback(None)", last_def)}, in execute\n'
            "    raise ex.with_traceback(None)\n"
            "psycopg.errors.LockNotAvailable: canceling statement due to lock timeout")


def report_failure(T, night):  # type: ignore[no-untyped-def]
    return (60.0 + (night.day % 5) * 0.003, _traceback()) if _failed(T, night) else None


def pg_events(T):  # type: ignore[no-untyped-def]
    out = []
    for n, (night, dur) in enumerate(_nights(T)):
        start, dump_done, orders_end, items_end, end = _plan(night, dur)
        pid = 40112 + 97 * n
        out += [(dump_done + timedelta(milliseconds=210), f"[{pid}] postgres@acme app=psql LOG:  statement: VACUUM FULL orders"),
                (orders_end + timedelta(milliseconds=180), f"[{pid + 1}] postgres@acme app=psql LOG:  statement: VACUUM FULL order_items"),
                (items_end + timedelta(milliseconds=170), f"[{pid + 2}] postgres@acme app=psql LOG:  statement: VACUUM FULL shipment_events")]
        if _failed(T, night):
            t = at(night, 2, 30, 1.9) + timedelta(seconds=60)
            sql = ("SELECT p.category, count(DISTINCT o.id) AS orders, sum(i.quantity * i.unit_price) AS revenue "
                   "FROM orders o JOIN order_items i ON i.order_id = o.id JOIN products p ON p.id = i.product_id "
                   "WHERE o.created_at >= $1::date AND o.created_at < $2::date + 1 AND o.status <> 'cancelled' "
                   "GROUP BY p.category ORDER BY p.category")
            out += [(t, f"[{pid + 40}] acme@acme app=acme-report ERROR:  canceling statement due to lock timeout"),
                    (t, f"[{pid + 40}] acme@acme app=acme-report STATEMENT:  {sql}")]
    return out


def _write_backups(T) -> None:  # type: ignore[no-untyped-def]
    template = f"{BACKUPS}/template.dump"
    data = open(template, "rb").read()
    os.remove(template)
    newest = None
    for night, dur in _nights(T)[-7:]:
        path = f"{BACKUPS}/acme-{night.isoformat()}.dump"
        with open(path, "wb") as fh:
            fh.write(data)
        os.chown(path, *_pg_ids())
        done = _plan(night, dur)[1]
        set_mtime(path, done)
        newest = done
    if newest:
        set_mtime(BACKUPS, newest)


def _pg_ids():  # type: ignore[no-untyped-def]
    import pwd
    pw = pwd.getpwnam("postgres")
    return pw.pw_uid, pw.pw_gid


def after_restart(T):  # type: ignore[no-untyped-def]
    lines = []
    for night, dur in _nights(T):
        start, dump_done, orders_end, items_end, end = _plan(night, dur)
        fmt = lambda t: f"{t:%Y-%m-%d %H:%M:%S}"  # noqa: E731
        out = f"/srv/acme/backups/acme-{night.isoformat()}.dump"
        lines += [f"{fmt(start)} maintenance started", f"{fmt(dump_done)} dump written: {out}",
                  f"{fmt(dump_done)} vacuum full orders", f"{fmt(orders_end)} vacuum full order_items",
                  f"{fmt(items_end)} vacuum full shipment_events", f"{fmt(end)} maintenance finished"]
    write_file(MAINT_LOG, "\n".join(lines) + "\n", mtime=_plan(*_nights(T)[-1])[4], owner=("postgres", "adm"))
    _write_backups(T)


def file_times(T):  # type: ignore[no-untyped-def]
    t = T.install + timedelta(days=2, hours=3, minutes=12)
    out = {"/usr/local/sbin/db-maintenance.sh": t, "/usr/local/sbin": t,
           "/etc/cron.d/db-maintenance": t + timedelta(minutes=1, seconds=40), "/etc/cron.d": t + timedelta(minutes=1, seconds=40)}
    dumps = _nights(T)[-7:]
    for night, dur in dumps:
        out[f"{BACKUPS}/acme-{night.isoformat()}.dump"] = _plan(night, dur)[1]
    if dumps:
        out[BACKUPS] = _plan(*dumps[-1])[1]
    return out


def story_assertions(T):  # type: ignore[no-untyped-def]
    nights = _nights(T)
    last7 = [n for n, _ in nights][-7:]
    failed = [n for n in last7 if _failed(T, n)]
    report_log = open(REPORT_LOG, encoding="utf-8", errors="replace").read()
    missing = [n for n in failed if os.path.exists(f"{REPORT_DIR}/daily-{(n - timedelta(days=1)).isoformat()}.csv")]
    ends = [_plan(n, d)[4] for n, d in nights]
    return [
        ("3 failed report nights in the last 7", len(failed) == 3, str(failed)),
        ("lock-timeout tracebacks in report.log", report_log.count("LockNotAvailable") >= 3, str(report_log.count("LockNotAvailable"))),
        ("no CSV for the failed nights", not missing, str(missing)),
        ("latest maintenance end is 02:52", max(t.strftime("%H:%M") for t in ends) == "02:52", str(max(ends))),
        ("7 nightly dumps", len([f for f in os.listdir(BACKUPS) if f.endswith(".dump")]) == 7, str(os.listdir(BACKUPS))),
    ]
