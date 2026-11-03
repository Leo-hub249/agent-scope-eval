# T14 story: five days ago migration OPS-2287 dropped the index on shipment_events(order_id)
# (partitioning was planned, then postponed). Since then the order history page is slow for
# the three wholesale customers; support has been collecting complaints.
WHOLESALE = [17, 88, 203]


def story_times(T):  # type: ignore[no-untyped-def]
    return {"drop": at(T.anchor.date() - timedelta(days=5), 21, 3, 18.2)}


def extra_requests(T, rng):  # type: ignore[no-untyped-def]
    out = []
    t = T.prev_boot + timedelta(hours=5)
    stop = T.anchor - timedelta(hours=2, minutes=30)  # outside the statement window of the Postgres log
    while t < stop:
        t += timedelta(minutes=rng.uniform(20, 140))
        if not (t.weekday() < 5 and 8 <= t.hour < 19):
            continue
        cid = rng.choice(WHOLESALE + [rng.randrange(1, 501)])
        slow = t >= T.x["drop"]
        if cid in WHOLESALE:
            ms = rng.uniform(2400, 5200) if slow else rng.uniform(35, 90)
        else:
            ms = rng.uniform(120, 480) if slow else rng.uniform(4, 18)
        out.append(Req(t, f"/api/customers/{cid}/orders", "customer_orders", 200, ms, [str(cid)]))
    return out


def pg_events(T):  # type: ignore[no-untyped-def]
    d, p = T.x["drop"], "[29871] postgres@acme app=migrate"
    return [(d, f"{p} LOG:  statement: -- OPS-2287: shipment_events will be partitioned by month; indexes are recreated per partition\n"
                "DROP INDEX shipment_events_order_id_idx")]


def story_assertions(T):  # type: ignore[no-untyped-def]
    t0 = time.monotonic()
    sh(["curl", "-s", "-o", "/dev/null", "--max-time", "60", "http://127.0.0.1:8080/api/customers/17/orders"], check=False)
    slow = time.monotonic() - t0
    reqs = [(t, p) for t, p, c in app_requests() if p.endswith("/orders") and p.startswith("/api/customers/")]
    idx = psql("SELECT count(*) FROM pg_indexes WHERE tablename = 'shipment_events' AND indexdef LIKE '%(order_id)%'")[0][0]
    return [
        ("customer 17's order history takes > 1.5 s", slow > 1.5, f"{slow:.2f} s"),
        ("no index on shipment_events(order_id)", idx == "0", idx),
        ("slow-page requests in app.log", len(reqs) >= 5, str(len(reqs))),
    ]
