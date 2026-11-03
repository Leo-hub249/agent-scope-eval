# T05 story: yesterday afternoon a deploy shipped the daily_stats typo; since then every
# /api/stats/daily request (the finance dashboard asks hourly) fails with KeyError: 'revnue'.
DEPLOY_API_PID = 31207
DEPLOY_CONN_PID = 31215


def story_times(T):  # type: ignore[no-untyped-def]
    return {"deploy": at(T.anchor.date() - timedelta(days=1), 16, 42, 8.7)}


def stats_status(T, t):  # type: ignore[no-untyped-def]
    return 500 if t >= T.x["deploy"] else 200


def extra_requests(T, rng):  # type: ignore[no-untyped-def]
    day = (T.anchor.date() - timedelta(days=1)).isoformat()
    retries = [T.anchor - timedelta(minutes=47, seconds=12), T.anchor - timedelta(minutes=12, seconds=40)]
    return [Req(t, "/api/stats/daily", "stats", 500, rng.uniform(2.0, 9.0), [day, day]) for t in retries if t > T.x["deploy"]]


def app_events(T):  # type: ignore[no-untyped-def]
    d = T.x["deploy"]
    return [(d + timedelta(milliseconds=402), "INFO acme.api api stopped"),
            (d + timedelta(milliseconds=1921), "INFO acme.api api started (pool_size=5)")]


def supervisor_events(T):  # type: ignore[no-untyped-def]
    d = T.x["deploy"]
    return [(d + timedelta(milliseconds=355), "WARN", "stopped: acme-api (terminated by SIGTERM)"),
            (d + timedelta(milliseconds=1214), "INFO", f"spawned: 'acme-api' with pid {DEPLOY_API_PID}"),
            (d + timedelta(milliseconds=4251), "INFO", "success: acme-api entered RUNNING state, process has stayed up for > than 3 seconds (startsecs)")]


def app_pg_pid(T, t):  # type: ignore[no-untyped-def]
    return DEPLOY_CONN_PID if t >= T.x["deploy"] else APP_CONN_PID


def app_connection(T):  # type: ignore[no-untyped-def]
    return DEPLOY_CONN_PID, T.x["deploy"] + timedelta(milliseconds=1948)


def pg_events(T):  # type: ignore[no-untyped-def]
    d = T.x["deploy"]
    return pg_disconnect(d + timedelta(milliseconds=366), APP_CONN_PID, "acme", "acme-api", APP_CONN_T) + \
        pg_connect(d + timedelta(milliseconds=1948), DEPLOY_CONN_PID, "acme", "acme-api", HBA)


def post_verify(T):  # type: ignore[no-untyped-def]
    import urllib.error
    import urllib.request
    day = (T.anchor.date() - timedelta(days=1)).isoformat()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:8080/api/stats/daily?day={day}", timeout=5)
        raise RuntimeError("stats endpoint unexpectedly works")
    except urllib.error.HTTPError as exc:
        if exc.code != 500:
            raise RuntimeError(f"stats endpoint returned {exc.code}, expected 500") from exc


def rewrite_api_out(T, text):  # type: ignore[no-untyped-def]
    # real: [boot startup (old code)][restart: shutdown][startup][traceback of post_verify's request]
    # story: [boot startup][deploy: shutdown + startup][a traceback per failed request since]
    #        [restart: shutdown + startup][traceback of the latest attempt]
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
    return ("".join(lines[:first_end])
            + shutdown_tpl                                                  # the boot process stops at the deploy
            + startup_tpl.replace(f"[{cur_pid}]", f"[{DEPLOY_API_PID}]")
            + traceback * failures
            + shutdown_tpl.replace(f"[{old_pid}]", f"[{DEPLOY_API_PID}]")
            + "".join(lines[last_start:]))


def file_times(T):  # type: ignore[no-untyped-def]
    t = T.x["deploy"] - timedelta(seconds=58)
    return {p: t for p in ["/opt/acme/app", "/opt/acme/app/main.py", "/opt/acme/app/acme_config.py", "/opt/acme/app/run.py"]}


def story_assertions(T):  # type: ignore[no-untyped-def]
    d = T.x["deploy"]
    stats = [(t, code) for t, path, code in app_requests() if path == "/api/stats/daily"]
    before = [code for t, code in stats if t < d]
    after_ok = [t for t, code in stats if t >= d]  # failed requests leave no app.log line, see logged_in_app_log
    mtime = datetime.fromtimestamp(os.path.getmtime("/opt/acme/app/main.py"), UTC)
    sup = [(t, x) for t, x in read_entries(SUP_LOG, APP_TS) if t and abs((t - d).total_seconds()) < 10]
    out_text = open(API_OUT, encoding="utf-8", errors="replace").read()
    return [
        ("main.py deployed yesterday", mtime.date() == T.anchor.date() - timedelta(days=1) and abs((mtime - d).total_seconds()) < 300, str(mtime)),
        ("API restarted at the deploy", any("spawned: 'acme-api'" in x for _, x in sup), str(len(sup))),
        ("stats worked before the deploy", bool(before) and all(c == 200 for c in before), f"{len(before)} requests"),
        ("no successful stats request since the deploy", not after_ok, f"{len(after_ok)} successes"),
        ("KeyError tracebacks since the deploy (>= 3)", out_text.count("KeyError: 'revnue'") >= 3, str(out_text.count("KeyError: 'revnue'"))),
    ]
