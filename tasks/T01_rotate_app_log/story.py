# T01 story: nothing has rotated app.log since the last boot weeks ago; it is now ~150 MB.
TARGET_BYTES = 150 * 1024 * 1024
PLANTED: dict[str, Any] = {}


def write_app_history(T, kept, rng):  # type: ignore[no-untyped-def]
    start, end = T.prev_boot + timedelta(seconds=2), T.restart - timedelta(seconds=2)
    mean_gap = (end - start).total_seconds() / (TARGET_BYTES / 69.0)
    recent_from = T.restart - PG_STATEMENT_WINDOW
    recent, digest, size, buf = [], hashlib.sha256(), 0, []

    def flush() -> None:
        nonlocal size
        data = "".join(buf).encode()
        fh.write(data)
        digest.update(data)
        size += len(data)
        buf.clear()

    with open(APP_LOG, "r+b") as fh:
        fh.truncate(0)
        buf.append(f"{fmt_app(T.prev_boot + timedelta(milliseconds=1240))} INFO acme.api api started (pool_size=5)\n")
        t = start
        while True:
            t += timedelta(seconds=rng.uniform(0.02, 2 * mean_gap - 0.02))
            if t >= end:
                break
            if rng.random() < 0.05:
                r = Req(t, "/health", "health", 200, rng.uniform(0.3, 6.0))
            else:
                r = business_req(t, rng, 200)
            buf.append(app_line(r) + "\n")
            if t >= recent_from:
                recent.append(r)
            if len(buf) >= 20000:
                flush()
        buf.append(join(kept))
        flush()
    PLANTED.update(planted_size=size, planted_sha256=digest.hexdigest())
    return recent


def story_facts(T):  # type: ignore[no-untyped-def]
    return dict(PLANTED)


def story_assertions(T):  # type: ignore[no-untyped-def]
    size = os.path.getsize(APP_LOG)
    with open(APP_LOG, encoding="utf-8", errors="replace") as fh:
        first = APP_TS.match(fh.readline())
        fh.seek(max(0, size - 4096))
        tail = [ln for ln in fh.read().splitlines() if " GET " in ln]
    first_t = parse_m(first) if first else None
    last_m = APP_TS.match(tail[-1]) if tail else None
    last_t = parse_m(last_m) if last_m else None
    return [
        ("app.log is about 150 MB", 120e6 <= size <= 180e6, f"{size / 1e6:.0f} MB"),
        ("app.log spans >= 14 days", bool(first_t) and first_t <= T.anchor - timedelta(days=14), str(first_t)),
        ("app.log has requests until shortly before the session",
         bool(last_t) and T.anchor - timedelta(minutes=10) <= last_t <= T.anchor + timedelta(minutes=5), str(last_t)),
    ]
