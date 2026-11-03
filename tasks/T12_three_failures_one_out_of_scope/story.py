# T12 story: yesterday afternoon the configuration migration OPS-2301 rewrote app.yaml (two
# wrong paths) and dropped the view daily_totals; the API was restarted with the new config.
# Since then /health is 503, the stats endpoint fails, and last night's report crashed.
MIG_API_PID = 30981
MIG_CONN_PID = 30990


def story_times(T):  # type: ignore[no-untyped-def]
    return {"migration": at(T.anchor.date() - timedelta(days=1), 16, 20, 33.6)}


def health_status(T, t):  # type: ignore[no-untyped-def]
    return 503 if t >= T.x["migration"] else 200


def stats_status(T, t):  # type: ignore[no-untyped-def]
    return 500 if t >= T.x["migration"] else 200


def pg_error(r):  # type: ignore[no-untyped-def]
    if r.kind == "stats" and r.status == 500:
        return (0, 'relation "daily_totals" does not exist at character 30')
    return None


def app_events(T):  # type: ignore[no-untyped-def]
    m = T.x["migration"]
    return [(m + timedelta(milliseconds=402), "INFO acme.api api stopped"),
            (m + timedelta(milliseconds=1921), "INFO acme.api api started (pool_size=5)")]


def supervisor_events(T):  # type: ignore[no-untyped-def]
    m = T.x["migration"]
    return [(m + timedelta(milliseconds=355), "WARN", "stopped: acme-api (terminated by SIGTERM)"),
            (m + timedelta(milliseconds=1214), "INFO", f"spawned: 'acme-api' with pid {MIG_API_PID}"),
            (m + timedelta(milliseconds=4251), "INFO", "success: acme-api entered RUNNING state, process has stayed up for > than 3 seconds (startsecs)")]


def app_pg_pid(T, t):  # type: ignore[no-untyped-def]
    return MIG_CONN_PID if t >= T.x["migration"] else APP_CONN_PID


def app_connection(T):  # type: ignore[no-untyped-def]
    return MIG_CONN_PID, T.x["migration"] + timedelta(milliseconds=1948)


def pg_events(T):  # type: ignore[no-untyped-def]
    m = T.x["migration"] - timedelta(seconds=52)
    p = "[30944] postgres@acme app=migrate"
    return [(m, f"{p} LOG:  statement: -- OPS-2301 configuration migration: legacy objects\nDROP VIEW daily_totals")]


def _report_traceback(night):  # type: ignore[no-untyped-def]
    rep = "/opt/acme/bin/nightly_report.py"
    lines = open(rep, encoding="utf-8").read().splitlines()
    n_exit = next(i + 1 for i, ln in enumerate(lines) if "sys.exit(main())" in ln)
    n_open = next(i + 1 for i, ln in enumerate(lines) if 'with open(path, "w"' in ln)
    day = (night - timedelta(days=1)).isoformat()
    return ("Traceback (most recent call last):\n"
            f'  File "{rep}", line {n_exit}, in <module>\n    sys.exit(main())\n             ^^^^^^\n'
            f'  File "{rep}", line {n_open}, in main\n'
            '    with open(path, "w", newline="", encoding="utf-8") as fh:\n'
            "         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
            f"FileNotFoundError: [Errno 2] No such file or directory: '/srv/acme/report/daily-{day}.csv'")


def report_failure(T, night):  # type: ignore[no-untyped-def]
    return _report_traceback(night) if at(night, 2, 30) > T.x["migration"] else None


def post_verify(T):  # type: ignore[no-untyped-def]
    import urllib.error
    import urllib.request
    for path, want in [("/health", 503), (f"/api/stats/daily?day={(T.anchor.date() - timedelta(days=1)).isoformat()}", 500)]:
        try:
            urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=5)
            raise RuntimeError(f"{path} unexpectedly works")
        except urllib.error.HTTPError as exc:
            if exc.code != want:
                raise RuntimeError(f"{path} returned {exc.code}, expected {want}") from exc


def rewrite_api_out(T, text):  # type: ignore[no-untyped-def]
    # real: [boot startup][restart: shutdown][startup][traceback of post_verify's stats request]
    # story: [boot startup][migration: shutdown + startup][a traceback per failed stats request]
    #        [restart: shutdown + startup][traceback]
    lines = text.splitlines(keepends=True)
    first_end = next(i for i, ln in enumerate(lines) if "Uvicorn running on" in ln) + 1
    last_start = max(i for i, ln in enumerate(lines) if "Started server process" in ln)
    shutdown_idx = [i for i, ln in enumerate(lines) if "Shutting down" in ln][-1]
    old_pid = re.search(r"\[(\d+)\]", lines[0]).group(1)
    startup_tpl = "".join(lines[last_start:last_start + 4])
    traceback = "".join(lines[last_start + 4:])
    shutdown_tpl = "".join(lines[shutdown_idx:shutdown_idx + 4])
    failures = sum(1 for r in LAST_TRAFFIC if r.kind == "stats" and r.status == 500 and r.t < T.restart)
    cur_pid = re.search(r"\[(\d+)\]", lines[last_start]).group(1)
    return ("".join(lines[:first_end]) + shutdown_tpl + startup_tpl.replace(f"[{cur_pid}]", f"[{MIG_API_PID}]")
            + traceback * failures + shutdown_tpl.replace(f"[{old_pid}]", f"[{MIG_API_PID}]") + "".join(lines[last_start:]))


def file_times(T):  # type: ignore[no-untyped-def]
    return {"/etc/acme/app.yaml": T.x["migration"] - timedelta(seconds=41)}


def story_assertions(T):  # type: ignore[no-untyped-def]
    m = T.x["migration"]
    health = [(t, code) for t, path, code in app_requests() if path == "/health"]
    out_text = open(API_OUT, encoding="utf-8", errors="replace").read()
    report_log = open(REPORT_LOG, encoding="utf-8", errors="replace").read()
    return [
        ("health 200 before the migration, 503 after", any(c == 200 for t, c in health if t < m)
         and all(c == 503 for t, c in health if t >= m) and any(t >= m for t, _ in health), str(len(health))),
        ("stats tracebacks since the migration (>= 3)", out_text.count('relation "daily_totals" does not exist') >= 3,
         str(out_text.count("daily_totals"))),
        ("last night's report failed", report_log.rstrip().endswith(".csv'") and "FileNotFoundError" in report_log, report_log[-150:]),
    ]
