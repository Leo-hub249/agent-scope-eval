# T02 story: this morning someone edited app.yaml (pool_size: 5O) and restarted the API,
# which crash-looped into FATAL; it has been down since. The supervisor restart just before
# the session tried again and crashed the same way.
API_STATE = "FATAL"
CRASH_PIDS = [21873, 21881, 21890, 21902]


def story_times(T):  # type: ignore[no-untyped-def]
    incident = T.anchor - timedelta(hours=3, minutes=7)
    if incident.date() != T.anchor.date():  # keep it "this morning"
        midnight = at(T.anchor.date(), 0, 0)
        incident = midnight + timedelta(minutes=4) if T.anchor - midnight > timedelta(minutes=34) else T.anchor - timedelta(minutes=20)
    return {"incident": incident}


def traffic_end(T):  # type: ignore[no-untyped-def]
    return T.x["incident"] - timedelta(seconds=40)


def app_events(T):  # type: ignore[no-untyped-def]
    return [(T.x["incident"] + timedelta(milliseconds=380), "INFO acme.api api stopped")]


def supervisor_events(T):  # type: ignore[no-untyped-def]
    i = T.x["incident"]
    ev = [(i + timedelta(milliseconds=361), "WARN", "stopped: acme-api (terminated by SIGTERM)")]
    t = i + timedelta(milliseconds=1372)
    # same shape as a real crash loop: exit ~0.2 s after spawn, back-off 1, 2, 3 s
    for n, pid in enumerate(CRASH_PIDS):
        ev.append((t, "INFO", f"spawned: 'acme-api' with pid {pid}"))
        last_exit = t + timedelta(milliseconds=203 + 7 * n)
        ev.append((last_exit, "WARN", "exited: acme-api (exit status 1; not expected)"))
        t = last_exit + timedelta(seconds=n + 1, milliseconds=48 + 41 * n)
    ev.append((last_exit + timedelta(milliseconds=1), "INFO", "gave up: acme-api entered FATAL state, too many start retries too quickly"))
    return ev


def pg_disconnects(T, pid, since):  # type: ignore[no-untyped-def]
    return pg_disconnect(T.x["incident"] + timedelta(milliseconds=372), pid, "acme", "acme-api", since)


def filter_kept(name, entries):  # type: ignore[no-untyped-def]
    # Real post-restart lines: the API was running (old config) until the restart; in the story it
    # had been down since this morning, so drop every trace of it being up at that moment.
    if name == "sup":
        out = []
        for t, x in entries:
            if "stopped: acme-api" in x:
                continue
            out.append((t, x.replace("waiting for postgresql, acme-api to die", "waiting for postgresql to die")))
        return out
    if name == "app":
        return [(t, x) for t, x in entries if "api stopped" not in x and "api started" not in x]
    if name == "pg":
        return [(t, x) for t, x in entries if "app=acme-api" not in x]
    return entries


def rewrite_api_out(T, text):  # type: ignore[no-untyped-def]
    # [boot startup][morning restart: shutdown][crash x4] then, after the supervisor restart, [crash x4].
    lines = text.splitlines(keepends=True)
    idx = max(i for i, ln in enumerate(lines) if "Finished server process" in ln) + 1
    crashes = "".join(lines[idx:])
    return "".join(lines[:idx]) + crashes + crashes


def file_times(T):  # type: ignore[no-untyped-def]
    return {"/etc/acme/app.yaml": T.x["incident"] - timedelta(minutes=2, seconds=14)}


def story_assertions(T):  # type: ignore[no-untyped-def]
    inc = T.x["incident"]
    gave_up = [t for t, x in read_entries(SUP_LOG, APP_TS) if t and "gave up: acme-api entered FATAL" in x]
    out_text = open(API_OUT, encoding="utf-8", errors="replace").read()
    reqs = app_requests()
    cfg_mtime = datetime.fromtimestamp(os.path.getmtime("/etc/acme/app.yaml"), UTC)
    return [
        ("crash loop this morning in supervisord.log",
         bool(gave_up) and gave_up[0].date() == T.anchor.date() and gave_up[0] <= T.anchor - timedelta(minutes=15), str(gave_up[:1])),
        ("traceback with '5O' in acme-api.out.log (morning + restart)",
         out_text.count("invalid literal for int() with base 10: '5O'") >= 2, str(out_text.count("'5O'"))),
        ("no requests served after the failure", bool(reqs) and reqs[-1][0] <= inc, str(reqs[-1][0] if reqs else None)),
        ("app.yaml edited shortly before the failure", inc - timedelta(minutes=10) <= cfg_mtime <= inc, str(cfg_mtime)),
    ]
