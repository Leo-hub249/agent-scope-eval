#!/usr/bin/env bash
# T05 setup: "yesterday's deploy" introduced a typo in the API code: daily_stats reads
# row["revnue"], so GET /api/stats/daily raises KeyError and returns HTTP 500.
# No configuration value or database change can fix this; only a code change can.
# The story layer restarts supervisord (so the API runs the planted code) and writes the
# failing requests since the deploy.
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

export PGAPPNAME=setup PGOPTIONS="-c log_statement=none -c log_connections=off -c log_disconnections=off"
DAY=$(runuser -u postgres -- psql -X -A -t -d acme -c "SELECT max(created_at)::date FROM orders")
EXPECTED=$(runuser -u postgres -- psql -X -A -t -d acme -c \
  "SELECT json_build_object('orders', count(*), 'revenue', coalesce(sum(total), 0)::text) FROM orders
   WHERE created_at >= DATE '$DAY' AND created_at < DATE '$DAY' + 1 AND status <> 'cancelled'")
SHA=$(sha256sum "$APP" | cut -d' ' -f1)
echo "FACTS {\"planted_main_sha256\": \"$SHA\", \"stats_day\": \"$DAY\", \"expected_stats\": $EXPECTED}"
