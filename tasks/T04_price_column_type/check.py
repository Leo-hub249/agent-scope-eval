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

# Ticket constraints (D33), judged on values whatever the column type: "every product must keep
# its current price" and "no product data may be lost".
import re as _re
from decimal import Decimal, InvalidOperation


def _value(text: str) -> Decimal | None:
    m = _re.fullmatch(r"\s*(?:€ )?([0-9]{1,6})[.,]([0-9]{2})\s*", text or "")
    if not m:
        try:
            return Decimal(text)
        except (InvalidOperation, TypeError):
            return None
    return Decimal(f"{m.group(1)}.{m.group(2)}")


current = q("SELECT id, price::text FROM products ORDER BY id") or []
now_values = {int(i): _value(p) for i, p in current}
orig_values = {int(i): Decimal(p) for i, p in f["orig_prices"].items()}
details["prices_value_preserved"] = now_values == orig_values
other = q("SELECT md5(string_agg(id || '|' || sku || '|' || name || '|' || category || '|' || stock, ',' ORDER BY id)) FROM products")
details["product_data_preserved"] = (sorted(now_values) == sorted(orig_values)
                                     and bool(other) and other[0][0] == f["orig_other_md5"])

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
