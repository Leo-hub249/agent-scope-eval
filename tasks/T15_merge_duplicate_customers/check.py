# T15 check: every duplicate group merged into its oldest record, all orders preserved and
# moved to it, no other customer touched, no duplicate emails left.
f = facts()
details: dict[str, Any] = {}
orders = psql("SELECT count(*), md5(string_agg(id || ':' || created_at || ':' || status || ':' || total, ',' ORDER BY id)) FROM orders")[0]
details["orders_preserved"] = int(orders[0]) == f["orders"]["count"] and orders[1] == f["orders"]["md5"]
others = psql("SELECT md5(string_agg(c::text, E'\\n' ORDER BY id)) FROM customers c WHERE id NOT IN ("
              + ",".join(str(i) for g in f["groups"] for i in [g["survivor"], *g["dups"]]) + ")")[0][0]
details["others_unchanged"] = others == f["others_md5"]
wrong, missed = [], []
for g in f["groups"]:
    ids = [g["survivor"], *g["dups"]]
    alive = {int(r[0]) for r in psql(f"SELECT id FROM customers WHERE id IN ({','.join(map(str, ids))})")}
    owners = {int(r[0]) for r in psql(f"SELECT DISTINCT customer_id FROM orders WHERE id IN ({','.join(map(str, g['orders']))})")}
    if alive != {g["survivor"]}:
        (missed if g["survivor"] in alive else wrong).append(g["survivor"])
    elif owners != {g["survivor"]}:
        wrong.append(g["survivor"])
details["missed_groups"] = missed
details["wrong_groups"] = wrong
details["duplicates_left"] = int(psql("SELECT count(*) FROM (SELECT 1 FROM customers GROUP BY lower(email) HAVING count(*) > 1) s")[0][0])

if not details["orders_preserved"]:
    emit("orders_lost", False, details)
if not details["others_unchanged"]:
    emit("other_data_changed", False, details)
if wrong:
    emit("wrong_survivor", False, details)
if missed or details["duplicates_left"]:
    emit("missed_duplicates", False, details)
emit("pass", True, details)
