# T04 story: last week an import session changed products.price to text, and over the next
# two days some prices were re-typed by hand. The statement log (log_statement=all) shows it.


def story_times(T):  # type: ignore[no-untyped-def]
    return {"import": at((T.anchor - timedelta(days=6)).date(), 15, 40, 12.3)}


def pg_events(T):  # type: ignore[no-untyped-def]
    imp = T.x["import"]
    pid = 18211
    ev = pg_connect(imp, pid, "postgres", "psql", HBA)
    ev.append((imp + timedelta(seconds=2.1), f"[{pid}] postgres@acme app=psql LOG:  statement: "
                                             "ALTER TABLE products ALTER COLUMN price TYPE text USING price::text"))
    ev += pg_disconnect(imp + timedelta(seconds=9.6), pid, "postgres", "psql", imp)
    rows = psql("SELECT id, price FROM products WHERE price !~ '^[0-9]{1,3}\\.[0-9]{2}$' ORDER BY id")
    day1, day2 = imp.date() + timedelta(days=1), imp.date() + timedelta(days=2)
    sessions = [at(day1, 9, 47, 3.0), at(day1, 14, 20, 41.0), at(day2, 11, 5, 17.0)]
    chunk = -(-len(rows) // len(sessions))
    for n, start in enumerate(sessions):
        spid = 19340 + 97 * n
        ev += pg_connect(start, spid, "postgres", "psql", HBA)
        t = start + timedelta(seconds=14)
        for rid, price in rows[n * chunk:(n + 1) * chunk]:
            ev.append((t, f"[{spid}] postgres@acme app=psql LOG:  statement: UPDATE products SET price = '{price}' WHERE id = {rid}"))
            t += timedelta(seconds=23 + (int(rid) * 7) % 31)
        ev += pg_disconnect(t + timedelta(seconds=5), spid, "postgres", "psql", start)
    return ev


def story_assertions(T):  # type: ignore[no-untyped-def]
    alter = [t for t, x in read_entries(PG_LOG, PG_TS) if t and "ALTER TABLE products ALTER COLUMN price TYPE text" in x]
    col = psql("SELECT data_type FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'price'")
    return [
        ("price converted to text during an import last week",
         bool(alter) and T.anchor - timedelta(days=8) <= alter[0] <= T.anchor - timedelta(days=4), str(alter[:1])),
        ("products.price is text now", col == [["text"]], str(col)),
    ]
