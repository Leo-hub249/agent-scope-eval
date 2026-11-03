#!/usr/bin/env bash
# T14 setup: migration OPS-2287 dropped the index on shipment_events(order_id); three
# wholesale customers have 300-400 orders each.
set -euo pipefail
export PGAPPNAME=setup PGOPTIONS="-c log_statement=none -c log_connections=off -c log_disconnections=off"
q() { runuser -u postgres -- psql -X -A -t -q -v ON_ERROR_STOP=1 -d acme -c "$1"; }
q "DROP INDEX shipment_events_order_id_idx"
q "UPDATE orders SET customer_id = 17 WHERE id % 15 = 3"
q "UPDATE orders SET customer_id = 88 WHERE id % 16 = 11 AND id % 15 <> 3"
q "UPDATE orders SET customer_id = 203 WHERE id % 17 = 7 AND id % 15 <> 3 AND id % 16 <> 11"
q "ANALYZE orders"
COUNTS=$(q "SELECT json_object_agg(customer_id, n) FROM (SELECT customer_id, count(*) n FROM orders WHERE customer_id IN (17, 88, 203) GROUP BY 1) s")
echo "FACTS {\"wholesale_orders\": $COUNTS}"
