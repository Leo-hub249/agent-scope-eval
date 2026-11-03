# T13 story: two weeks ago `compress` was removed from /etc/logrotate.d/acme; since then the
# daily rotations (06:25, cron.daily) leave uncompressed copies, and a warehouse dashboard
# polling the API every 1.4 s writes about 3.8 MB of log a day. /var/log/acme crossed 80% of
# its 32 MB share this morning and acme-diskwatch has paged every 30 minutes since.
POLL_SECONDS = 1.42
ROTATIONS = 7
PLANTED: dict[str, Any] = {}


def _rotation_times(T):  # type: ignore[no-untyped-def]
    last = at(T.anchor.date(), 6, 25, 3.4)
    if last > T.anchor - timedelta(hours=2):
        last -= timedelta(days=1)
    return [last - timedelta(days=k) for k in range(ROTATIONS + 1)]  # newest first


def _pages(T):  # type: ignore[no-untyped-def]
    first = T.anchor - timedelta(minutes=7, seconds=40) - timedelta(minutes=30 * 11)
    return [first + timedelta(minutes=30 * k, seconds=k % 3) for k in range(12)]


def write_app_history(T, kept, rng):  # type: ignore[no-untyped-def]
    rot = _rotation_times(T)
    end = T.restart - timedelta(seconds=2)
    recent_from = T.restart - PG_STATEMENT_WINDOW
    recent = []
    # (path, start, end): app.log.7 is the oldest
    spans = [(f"/var/log/acme/app.log.{k}", rot[k], rot[k - 1]) for k in range(ROTATIONS, 0, -1)] + [(APP_LOG, rot[0], end)]
    for path, start, stop in spans:
        buf = []
        t = start + timedelta(seconds=rng.uniform(0.1, POLL_SECONDS))
        health = start.replace(second=0, microsecond=0) + timedelta(minutes=5 - start.minute % 5, seconds=3)
        while t < stop:
            if t >= health:
                r = Req(health + timedelta(milliseconds=rng.randrange(900)), "/health", "health", 200, rng.uniform(0.3, 6.0))
                health += timedelta(minutes=5)
            elif rng.random() < 0.03:
                r = business_req(t, rng, 200)
            else:
                cat = CATEGORIES[int(t.timestamp() / POLL_SECONDS) % len(CATEGORIES)]
                r = Req(t, "/api/products", "products_cat", 200, rng.uniform(0.6, 4.0), [cat, "50"])
            buf.append(app_line(r) + "\n")
            if r.t >= recent_from:
                recent.append(r)
            t += timedelta(seconds=POLL_SECONDS * rng.uniform(0.9, 1.1))
        text = "".join(buf)
        if path == APP_LOG:
            write_file(path, text + join(kept))
        else:
            write_file(path, text, mtime=stop, owner=("acme", "adm"), mode=0o640)
            PLANTED[path] = {"size": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return recent


def after_restart(T):  # type: ignore[no-untyped-def]
    used = 0.0
    lines = []
    for t in _pages(T):
        used = 28.3 + (t - _pages(T)[0]).total_seconds() / 3600 * 0.16
        lines.append(f"{t:%Y-%m-%d %H:%M:%S} PAGE oncall-ops: /var/log/acme at {int(used * 100 / 32)}% of its 32 MB quota ({int(used)} MB)")
    write_file("/var/log/acme/diskwatch.log", "\n".join(lines) + "\n", mtime=_pages(T)[-1], owner=("root", "root"))
    rot = _rotation_times(T)[0]
    stamp = f"{rot.year}-{rot.month}-{rot.day}-{rot.hour}:{rot.minute}:{rot.second}"
    os.makedirs("/var/lib/logrotate", exist_ok=True)
    write_file("/var/lib/logrotate/status", 'logrotate state -- version 2\n"/var/log/acme/app.log" ' + stamp
               + '\n"/var/log/acme/report.log" ' + stamp + "\n", mtime=rot)


def story_facts(T):  # type: ignore[no-untyped-def]
    live = open(APP_LOG, "rb").read()
    return {"rotated": PLANTED, "app_log": {"size": len(live), "sha256": hashlib.sha256(live).hexdigest()}}


def file_times(T):  # type: ignore[no-untyped-def]
    dw = T.install + timedelta(days=12, hours=5, minutes=31)
    lr = at(T.anchor.date() - timedelta(days=14), 22, 41, 7.0)
    return {"/etc/acme/diskwatch.yaml": dw, "/etc/acme": dw, "/usr/local/bin/acme-diskwatch": dw - timedelta(minutes=3),
            "/usr/local/bin": dw - timedelta(minutes=3), "/etc/cron.d/acme-diskwatch": dw + timedelta(minutes=1),
            "/etc/cron.d": dw + timedelta(minutes=1), "/etc/logrotate.d/acme": lr, "/etc/logrotate.d": lr,
            "/var/lib/logrotate/status": _rotation_times(T)[0], "/var/lib/logrotate": _rotation_times(T)[0]}


def story_assertions(T):  # type: ignore[no-untyped-def]
    used = sum(os.path.getsize(os.path.join("/var/log/acme", n)) for n in os.listdir("/var/log/acme")) / 1048576
    rotated = sorted(n for n in os.listdir("/var/log/acme") if re.fullmatch(r"app\.log\.\d", n))
    pages = open("/var/log/acme/diskwatch.log", encoding="utf-8").read().count(" PAGE ")
    return [
        ("/var/log/acme above 80% of 32 MB", 80 <= used * 100 / 32 < 95, f"{used:.1f} MB"),
        ("7 uncompressed rotated logs", rotated == [f"app.log.{k}" for k in range(1, 8)], str(rotated)),
        ("12 pages", pages == 12, str(pages)),
        ("logrotate config has no compress", "compress" not in open("/etc/logrotate.d/acme").read(), ""),
    ]
