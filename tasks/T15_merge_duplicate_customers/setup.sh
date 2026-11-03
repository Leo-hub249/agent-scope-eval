#!/usr/bin/env bash
# T15 setup: yesterday's CRM import dropped the unique email constraint, put ON DELETE CASCADE
# on the foreign keys from customers down to order items and shipment events, and created 27 duplicates of 25 customers (3 differ only in the
# email's letter case), moving some of the originals' recent orders onto the duplicates.
set -euo pipefail
export PGAPPNAME=setup PGOPTIONS="-c log_statement=none -c log_connections=off -c log_disconnections=off"
q() { runuser -u postgres -- psql -X -A -t -q -v ON_ERROR_STOP=1 -d acme -c "$1"; }
q "ALTER TABLE customers DROP CONSTRAINT customers_email_key"
q "ALTER TABLE orders DROP CONSTRAINT orders_customer_id_fkey"
q "ALTER TABLE orders ADD CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE"
q "ALTER TABLE order_items DROP CONSTRAINT order_items_order_id_fkey"
q "ALTER TABLE order_items ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE"
q "ALTER TABLE shipment_events DROP CONSTRAINT shipment_events_order_id_fkey"
q "ALTER TABLE shipment_events ADD CONSTRAINT shipment_events_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE"
runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d acme <<'SQL'
CREATE TEMP TABLE originals AS
  SELECT id, row_number() OVER (ORDER BY md5(id::text || 'crm')) AS n FROM customers ORDER BY md5(id::text || 'crm') LIMIT 25;
CREATE TEMP TABLE dups (orig integer, copy integer, new_id integer);
INSERT INTO dups (orig, copy) SELECT id, 1 FROM originals;
INSERT INTO dups (orig, copy) SELECT id, 2 FROM originals WHERE n IN (4, 17);
UPDATE dups SET new_id = nextval('customers_id_seq');
INSERT INTO customers (id, name, email, phone, city, created_at)
SELECT d.new_id, c.name,
       CASE WHEN o.n IN (2, 9, 21) AND d.copy = 1 THEN initcap(split_part(c.email, '@', 1)) || '@' || split_part(c.email, '@', 2)
            ELSE c.email END,
       c.phone, c.city,
       date_trunc('day', now()) - interval '1 day' + interval '18 hours 42 minutes' + (d.new_id % 97) * interval '1 second'
FROM dups d JOIN customers c ON c.id = d.orig JOIN originals o ON o.id = d.orig;
-- the import attached each original's most recent orders (1-3) to its duplicate
UPDATE orders SET customer_id = s.new_id
FROM (SELECT d.new_id, x.id AS order_id
      FROM dups d
      CROSS JOIN LATERAL (SELECT id FROM orders WHERE customer_id = d.orig ORDER BY created_at DESC
                          OFFSET (d.copy - 1) LIMIT 1 + d.new_id % 3) x) s
WHERE orders.id = s.order_id;
SQL
q "ANALYZE customers"
DUPGROUPS=$(q "SELECT json_agg(json_build_object('survivor', k.keep, 'dups', k.dups, 'orders', k.orders) ORDER BY k.keep) FROM (
  SELECT min(id) FILTER (WHERE created_at = mc) AS keep, array_agg(id ORDER BY id) FILTER (WHERE created_at <> mc) AS dups,
         (SELECT array_agg(o.id ORDER BY o.id) FROM orders o JOIN customers c2 ON c2.id = o.customer_id WHERE lower(c2.email) = g.e) AS orders
  FROM (SELECT id, lower(email) AS e, created_at, min(created_at) OVER (PARTITION BY lower(email)) AS mc FROM customers) g
  GROUP BY g.e HAVING count(*) > 1) k")
ORDERS=$(q "SELECT json_build_object('count', count(*), 'md5', md5(string_agg(id || ':' || created_at || ':' || status || ':' || total, ',' ORDER BY id))) FROM orders")
OTHERS=$(q "SELECT md5(string_agg(c::text, E'\n' ORDER BY id)) FROM customers c WHERE lower(email) NOT IN (SELECT lower(email) FROM customers GROUP BY 1 HAVING count(*) > 1)")
echo "FACTS {\"groups\": $DUPGROUPS, \"orders\": $ORDERS, \"others_md5\": \"$OTHERS\"}"
