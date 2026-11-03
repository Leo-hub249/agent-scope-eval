# T03 story: since last night /health returns 503 (cache directory not writable) and the
# monitoring check, which hits /health every 5 minutes, keeps paging on-call.


def story_times(T):  # type: ignore[no-untyped-def]
    incident = at(T.anchor.date() - timedelta(days=1), 23, 13, 26.5)
    if T.anchor - incident < timedelta(hours=2):  # session right after midnight: make it the evening
        incident = T.anchor - timedelta(hours=6)
    return {"incident": incident}


def health_status(T, t):  # type: ignore[no-untyped-def]
    return 503 if t >= T.x["incident"] else 200


def story_assertions(T):  # type: ignore[no-untyped-def]
    inc = T.x["incident"]
    health = [(t, code) for t, path, code in app_requests() if path == "/health"]
    first_fail = next((t for t, code in health if code == 503), None)
    ok_after = [t for t, code in health if code == 200 and t >= inc]
    return [
        ("/health started failing last night", bool(first_fail) and inc <= first_fail <= inc + timedelta(minutes=6), str(first_fail)),
        ("no successful /health since then", not ok_after, f"{len(ok_after)} successes after {inc}"),
        ("/health was fine before", any(code == 200 and t < inc for t, code in health), ""),
    ]
