"""Generate deterministic synthetic data for the Acme Parts database.

Stdlib only. Writes psql-compatible COPY blocks to stdout. The same seed always
produces byte-identical output, so every sandbox image starts from the same data.
All names, emails and cities are synthetic; emails use reserved example domains.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TextIO

SEED = 20260131
N_CUSTOMERS = 500
N_PRODUCTS = 120
N_ORDERS = 6000
# Fixed business window: data never depends on the wall clock.
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)

FIRST = ["Ada", "Bruno", "Carla", "Dario", "Elena", "Fabio", "Giulia", "Hugo", "Irene", "Jonas",
         "Katia", "Luca", "Marta", "Nico", "Olga", "Paolo", "Quinn", "Rosa", "Sergio", "Tina",
         "Ugo", "Vera", "Walter", "Xenia", "Yuri", "Zoe"]
LAST = ["Rossi", "Bianchi", "Ferri", "Conti", "Galli", "Moretti", "Lombardi", "Barbieri", "Fontana",
        "Santoro", "Mariani", "Rinaldi", "Caruso", "Ferrara", "Greco", "Serra", "Vitale", "Leone"]
CITIES = ["Milano", "Torino", "Bologna", "Verona", "Padova", "Genova", "Firenze", "Brescia",
          "Bergamo", "Trento", "Parma", "Modena"]
CATEGORIES = {
    "bearings": ["Ball bearing", "Roller bearing", "Thrust bearing", "Needle bearing"],
    "fasteners": ["Hex bolt", "Lock nut", "Washer set", "Socket screw", "Rivet pack"],
    "belts": ["V-belt", "Timing belt", "Flat belt"],
    "seals": ["O-ring kit", "Shaft seal", "Gasket sheet"],
    "motors": ["Stepper motor", "DC gear motor", "Servo motor"],
    "sensors": ["Proximity sensor", "Temperature probe", "Pressure switch", "Encoder"],
}
STATUSES = ["paid"] * 6 + ["shipped"] * 10 + ["pending"] * 2 + ["cancelled"]


# Tracking events, derived deterministically from the orders (no randomness): paid orders
# have a label and a pickup scan; shipped orders also have 60-160 in-transit pings and a
# delivery scan. About 350k rows.
SHIPMENT_EVENTS_SQL = """
INSERT INTO shipment_events (order_id, at, kind, location)
SELECT o.id, o.created_at + make_interval(mins => 20 + 37 * k),
       CASE WHEN k = 0 THEN 'label_created' WHEN k = 1 THEN 'picked_up'
            WHEN k = n - 1 THEN 'delivered' WHEN k = n - 2 THEN 'out_for_delivery' ELSE 'in_transit' END,
       (ARRAY['Milano', 'Torino', 'Bologna', 'Verona', 'Padova', 'Genova', 'Firenze', 'Brescia',
              'Bergamo', 'Trento', 'Parma', 'Modena'])[1 + (o.id * 7 + k * 3) % 12] || ' hub'
FROM orders o
CROSS JOIN LATERAL (SELECT CASE WHEN o.status = 'shipped' THEN 64 + (o.id * 37) % 101 ELSE 2 END AS n) c
CROSS JOIN LATERAL generate_series(0, c.n - 1) AS k
WHERE o.status IN ('paid', 'shipped')
ORDER BY o.id, k;

"""


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


def random_time(rng: random.Random, start: datetime, end: datetime) -> datetime:
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randrange(span))


def copy_block(out: TextIO, table: str, columns: list[str], rows: list[tuple[object, ...]]) -> None:
    out.write(f"COPY {table} ({', '.join(columns)}) FROM stdin;\n")
    for row in rows:
        out.write("\t".join(str(v) for v in row) + "\n")
    out.write("\\.\n\n")


def generate(out: TextIO, seed: int = SEED) -> None:
    rng = random.Random(seed)

    customers = []
    for cid in range(1, N_CUSTOMERS + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        domain = rng.choice(["example.com", "example.org", "example.net"])
        email = f"{first.lower()}.{last.lower()}{cid}@{domain}"
        created = random_time(rng, START - timedelta(days=730), START)
        # Ofcom's range reserved for fiction (+44 7700 900000-900999): never a real subscriber.
        phone = f"+44 7700 900{cid - 1:03d}"
        customers.append((cid, f"{first} {last}", email, phone, rng.choice(CITIES), ts(created)))

    products = []
    prices: dict[int, Decimal] = {}
    for pid in range(1, N_PRODUCTS + 1):
        category = rng.choice(sorted(CATEGORIES))
        base = rng.choice(CATEGORIES[category])
        size = rng.choice(["S", "M", "L", "XL", "6mm", "8mm", "10mm", "12mm"])
        price = Decimal(rng.randrange(150, 45000)) / 100
        prices[pid] = price
        sku = f"{category[:3].upper()}-{pid:04d}"
        products.append((pid, sku, f"{base} {size}", category, f"{price:.2f}", rng.randrange(0, 800)))

    orders = []
    items = []
    item_id = 1
    order_times = sorted(random_time(rng, START, END) for _ in range(N_ORDERS))
    for oid, created in enumerate(order_times, start=1):
        total = Decimal("0.00")
        for _ in range(rng.choice([1, 1, 2, 2, 3, 3, 4, 5])):
            pid = rng.randrange(1, N_PRODUCTS + 1)
            qty = rng.choice([1, 1, 1, 2, 2, 3, 5, 10])
            unit = prices[pid]
            total += unit * qty
            items.append((item_id, oid, pid, qty, f"{unit:.2f}"))
            item_id += 1
        cid = rng.randrange(1, N_CUSTOMERS + 1)
        orders.append((oid, cid, ts(created), rng.choice(STATUSES), f"{total:.2f}"))

    out.write("-- Generated by generate_seed.py (seed=%d). Do not edit by hand.\n" % seed)
    out.write("BEGIN;\n\n")
    copy_block(out, "customers", ["id", "name", "email", "phone", "city", "created_at"], customers)
    copy_block(out, "products", ["id", "sku", "name", "category", "price", "stock"], products)
    copy_block(out, "orders", ["id", "customer_id", "created_at", "status", "total"], orders)
    copy_block(out, "order_items", ["id", "order_id", "product_id", "quantity", "unit_price"], items)
    out.write(SHIPMENT_EVENTS_SQL)
    for table in ["customers", "products", "orders", "order_items", "shipment_events"]:
        out.write(f"SELECT setval('{table}_id_seq', (SELECT max(id) FROM {table}));\n")
    out.write("\nCOMMIT;\n")


if __name__ == "__main__":
    generate(sys.stdout)
