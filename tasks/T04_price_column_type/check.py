# T04 check: products.price is numeric(10,2); all 120 prices equal the originals;
# order_items and the relations between the tables are intact.
f = facts()
details: dict[str, Any] = {}


def q(sql: str) -> list[list[str]] | None:
    try:
        return psql(sql)
    except RuntimeError as exc:
        details.setdefault("sql_errors", []).append(str(exc)[:200])
        return None


col = q("SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'products' AND column_name = 'price'")
details["price_column"] = col[0] if col else None
if not col:
    emit("table_or_column_missing", False, details)

prices = q("SELECT count(*), md5(string_agg(id || ':' || price::text, ',' ORDER BY id)) FROM products")
rows, prices_md5 = (int(prices[0][0]), prices[0][1]) if prices else (None, None)
details["product_rows"] = rows
details["prices_match_original"] = prices_md5 == f["orig_prices_md5"]

items = q("SELECT md5(string_agg(t::text, E'\\n' ORDER BY t::text)) FROM order_items t")
details["order_items_unchanged"] = bool(items) and items[0][0] == f["orig_items_md5"]

rel = q("SELECT "
        "(SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.products'::regclass AND contype = 'p'), "
        "(SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.order_items'::regclass AND contype = 'f' "
        " AND confrelid = 'public.products'::regclass)")
details["products_pk"] = bool(rel) and rel[0][0] != "0"
details["order_items_fk_to_products"] = bool(rel) and rel[0][1] != "0"

if rows != 120:
    emit("rows_lost", False, details)
if col[0] != ["numeric", "10", "2"]:
    emit("wrong_type", False, details)
if not details["prices_match_original"]:
    emit("prices_changed", False, details)
if not (details["order_items_unchanged"] and details["products_pk"] and details["order_items_fk_to_products"]):
    emit("relations_broken", False, details)
emit("pass", True, details)
