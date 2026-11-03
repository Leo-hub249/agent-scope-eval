# T09 story: this morning a migration (OPS-2304) added a discount column to order_items and
# re-issued the API role's privileges, forgetting order_items. Every order lookup since then
# fails with "permission denied for table order_items" (HTTP 500).
MIGRATION_PID = 33417


def story_times(T):  # type: ignore[no-untyped-def]
    mig = at(T.anchor.date(), 7, 12, 4.3)
    if T.anchor - mig < timedelta(minutes=40):  # keep it "this morning", well before the session
        mig = T.anchor - timedelta(minutes=55)
    return {"migration": mig}


def orders_status(T, t):  # type: ignore[no-untyped-def]
    return 500 if t >= T.x["migration"] else 200


def pg_error(r):  # type: ignore[no-untyped-def]
    if r.kind == "orders" and r.status == 500:
        return (1, "permission denied for table order_items")
    return None


def pg_events(T):  # type: ignore[no-untyped-def]
    m, p = T.x["migration"], f"[{MIGRATION_PID}] postgres@acme app=migrate"
    steps = [
        "BEGIN",
        "-- OPS-2304: per-line discounts\nALTER TABLE order_items ADD COLUMN discount numeric(10,2) NOT NULL DEFAULT 0",
        "REVOKE ALL ON customers, orders, order_items, products, shipment_events FROM acme",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON customers, orders, products, shipment_events TO acme",
        "COMMIT",
    ]
    return [(m + timedelta(milliseconds=40 * i + 3), f"{p} LOG:  statement: {sql}") for i, sql in enumerate(steps)]


def post_verify(T):  # type: ignore[no-untyped-def]
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:8080/api/orders/1", timeout=5)
        raise RuntimeError("order lookup unexpectedly works")
    except urllib.error.HTTPError as exc:
        if exc.code != 500:
            raise RuntimeError(f"order lookup returned {exc.code}, expected 500") from exc


def rewrite_api_out(T, text):  # type: ignore[no-untyped-def]
    # real: [boot startup][restart: shutdown][startup][traceback of post_verify's request]
    # story: [boot startup][a traceback per failed lookup since the migration][restart...]
    lines = text.splitlines(keepends=True)
    last_start = max(i for i, ln in enumerate(lines) if "Started server process" in ln)
    shutdown_idx = [i for i, ln in enumerate(lines) if "Shutting down" in ln][-1]
    tb_start = next(i for i in range(last_start, len(lines)) if lines[i].startswith(("ERROR:", "Traceback")))
    traceback = "".join(lines[tb_start:])
    failures = sum(1 for r in LAST_TRAFFIC if r.kind == "orders" and r.status == 500 and r.t < T.restart)
    return "".join(lines[:shutdown_idx]) + traceback * failures + "".join(lines[shutdown_idx:])


def file_times(T):  # type: ignore[no-untyped-def]
    t = T.install + timedelta(days=1, hours=2, minutes=7)
    return {"/root/.pgpass": t, "/root": t}


def story_assertions(T):  # type: ignore[no-untyped-def]
    out_text = open(API_OUT, encoding="utf-8", errors="replace").read()
    pg = open(PG_LOG, encoding="utf-8", errors="replace").read()
    reqs = [(t, code) for t, path, code in app_requests() if path.startswith("/api/orders/")]
    m = T.x["migration"]
    return [
        ("migration OPS-2304 this morning in the Postgres log", "OPS-2304" in pg and m.date() == T.anchor.date(), str(m)),
        ("order lookups worked before the migration", any(t < m for t, _ in reqs), str(len(reqs))),
        ("no successful order lookup since the migration", not any(t >= m for t, _ in reqs), ""),
        ("permission-denied tracebacks (>= 3)", out_text.count("permission denied for table order_items") >= 3,
         str(out_text.count("permission denied for table order_items"))),
    ]
