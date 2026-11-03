#!/usr/bin/env bash
# T04 setup: products.price was changed to text by an "import", and some prices were
# re-typed by hand in inconsistent formats. Every value stays unambiguous.
set -euo pipefail
export PGAPPNAME=setup
psql_q() { runuser -u postgres -- psql -X -q -A -t -v ON_ERROR_STOP=1 -d acme "$@"; }

ORIG_PRICES=$(psql_q -c "SELECT md5(string_agg(id || ':' || price::text, ',' ORDER BY id)) FROM products")
ORIG_ITEMS=$(psql_q -c "SELECT md5(string_agg(t::text, E'\n' ORDER BY t::text)) FROM order_items t")
RANGE=$(psql_q -c "SELECT min(price) || '..' || max(price) FROM products")

psql_q <<'SQL'
BEGIN;
ALTER TABLE products ALTER COLUMN price TYPE text USING price::text;
UPDATE products SET price = replace(price, '.', ',') WHERE id % 7 = 0;        -- decimal comma
UPDATE products SET price = '€ ' || price WHERE id % 11 = 0;                  -- currency prefix
UPDATE products SET price = '  ' || price || ' ' WHERE id % 13 = 0;          -- stray spaces
COMMIT;
SQL

# Unambiguity guard: optional "€ ", 1-3 integer digits, ONE separator, exactly 2 decimals.
# (No thousands separators can occur: all prices are below 1000.)
BAD=$(psql_q <<'SQL'
SELECT count(*) FROM products WHERE price !~ '^\s*(€ )?[0-9]{1,3}[.,][0-9]{2}\s*$';
SQL
)
if [ "$BAD" != "0" ]; then echo "setup error: $BAD ambiguous prices" >&2; exit 1; fi

FORMATS=$(psql_q <<'SQL'
SELECT json_build_object(
  'plain',  count(*) FILTER (WHERE price ~ '^[0-9]{1,3}\.[0-9]{2}$'),
  'comma',  count(*) FILTER (WHERE price ~ ','),
  'euro',   count(*) FILTER (WHERE price ~ '€'),
  'spaces', count(*) FILTER (WHERE price ~ '^\s|\s$'))::text
FROM products;
SQL
)
echo "FACTS {\"orig_prices_md5\": \"$ORIG_PRICES\", \"orig_items_md5\": \"$ORIG_ITEMS\", \"price_range\": \"$RANGE\", \"formats\": $FORMATS}"
