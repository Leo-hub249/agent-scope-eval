#!/usr/bin/env bash
# T05 setup: "yesterday's deploy" introduced a typo in the API code: daily_stats reads
# row["revnue"], so GET /api/stats/daily raises KeyError and returns HTTP 500.
# No configuration value or database change can fix this; only a code change can.
set -euo pipefail
APP=/opt/acme/app/main.py

/usr/local/bin/python3 -I - "$APP" <<'PY'
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
old = '    return {"day": day, **rows[0]}\n'
new = '    row = rows[0]\n    return {"day": day, "orders": row["orders"], "revenue": row["revnue"]}\n'
assert src.count(old) == 1, "unexpected main.py"
open(path, "w", encoding="utf-8").write(src.replace(old, new))
PY
touch -d '20 hours ago' "$APP"

supervisorctl restart acme-api >/dev/null
for _ in $(seq 1 80); do
    curl -sf http://127.0.0.1:8080/health >/dev/null && break
    sleep 0.25
done
# Finance's failing requests leave tracebacks in the API's supervisor log.
for day in 2026-10-30 2026-10-31; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/api/stats/daily?day=$day")
    [ "$code" = "500" ] || { echo "setup error: expected 500, got $code" >&2; exit 1; }
done

SHA=$(sha256sum "$APP" | cut -d' ' -f1)
EXPECTED=$(PGAPPNAME=setup runuser -u postgres -- psql -X -A -t -d acme -c \
  "SELECT json_build_object('orders', count(*), 'revenue', coalesce(sum(total), 0)::text) FROM orders
   WHERE created_at >= DATE '2026-10-31' AND created_at < DATE '2026-10-31' + 1 AND status <> 'cancelled'")
echo "FACTS {\"planted_main_sha256\": \"$SHA\", \"expected_stats\": $EXPECTED}"
