#!/opt/acme/venv/bin/python
"""Nightly sales report for Acme Parts.

Writes <report.output_dir>/daily-<day>.csv with revenue per product category.
By default <day> is the latest business day present in the orders table
(not the wall clock), so the output is reproducible.
Usage: nightly_report.py [--day YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

sys.path.insert(0, "/opt/acme/app")

import psycopg  # noqa: E402

from acme_config import QuotaExceeded, check_storage_quota, conninfo, load_config  # noqa: E402

SQL_BY_CATEGORY = """
SELECT p.category, count(DISTINCT o.id) AS orders, sum(i.quantity * i.unit_price) AS revenue
FROM orders o
JOIN order_items i ON i.order_id = o.id
JOIN products p ON p.id = i.product_id
WHERE o.created_at >= %s::date AND o.created_at < %s::date + 1 AND o.status <> 'cancelled'
GROUP BY p.category
ORDER BY p.category
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", help="YYYY-MM-DD (default: latest day with orders)")
    args = parser.parse_args()

    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s report %(message)s")
    trigger = os.environ.get("ACME_TRIGGER", "manual")
    logging.info("run started (trigger=%s)", trigger)

    with psycopg.connect(conninfo(cfg, "acme-report")) as conn:
        day = args.day or conn.execute("SELECT max(created_at)::date FROM orders").fetchone()[0].isoformat()
        rows = conn.execute(SQL_BY_CATEGORY, (day, day)).fetchall()

    out_dir = cfg["report"]["output_dir"]
    path = os.path.join(out_dir, f"daily-{day}.csv")
    try:
        check_storage_quota(cfg)
    except QuotaExceeded as exc:
        logging.error("not written: %s", exc)
        return 1
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["day", "category", "orders", "revenue"])
        for category, orders, revenue in rows:
            writer.writerow([day, category, orders, f"{revenue:.2f}"])
    logging.info("written: %s (%d categories)", path, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
