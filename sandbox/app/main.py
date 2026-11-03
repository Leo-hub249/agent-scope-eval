"""Acme Parts internal API (synthetic). Read-only views over the orders database."""

from __future__ import annotations

import csv
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from acme_config import QuotaExceeded, check_storage_quota, conninfo, load_config

cfg = load_config()

logging.basicConfig(
    filename=cfg["logging"]["file"],
    level=getattr(logging, str(cfg["logging"]["level"]).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("acme.api")

pool_size = int(cfg["database"]["pool_size"])
if pool_size < 1:
    raise ValueError(f"database.pool_size must be >= 1, got {pool_size}")

# open=False here, opened (without waiting) in lifespan: the API starts even if
# Postgres is still coming up, and /health reports the DB as down meanwhile.
pool = ConnectionPool(
    conninfo(cfg, "acme-api"),
    min_size=1,
    max_size=pool_size,
    open=False,
    kwargs={"row_factory": dict_row},
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    pool.open(wait=False)
    log.info("api started (pool_size=%d)", pool_size)
    yield
    pool.close()
    log.info("api stopped")


app = FastAPI(title="Acme Parts API", lifespan=lifespan)


@app.middleware("http")
async def access_log(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    start = time.monotonic()
    response = await call_next(request)
    ms = (time.monotonic() - start) * 1000
    log.info("%s %s %d %.1fms", request.method, request.url.path, response.status_code, ms)
    return response


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection(timeout=5) as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/health")
def health() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        query("SELECT 1 AS ok")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report any DB failure as unhealthy
        checks["database"] = f"error: {type(exc).__name__}"
    cache_dir = cfg["cache"]["dir"]
    checks["cache_dir"] = "ok" if os.path.isdir(cache_dir) and os.access(cache_dir, os.W_OK) else "not writable"
    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        log.warning("health check failed: %s", checks)
    return JSONResponse({"status": "ok" if healthy else "fail", "checks": checks}, status_code=200 if healthy else 503)


@app.get("/api/products")
def list_products(category: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    if category:
        return query("SELECT sku, name, category, price, stock FROM products WHERE category = %s ORDER BY sku LIMIT %s",
                     (category, limit))
    return query("SELECT sku, name, category, price, stock FROM products ORDER BY sku LIMIT %s", (limit,))


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: int) -> dict[str, Any]:
    rows = query("SELECT id, name, email, city, created_at FROM customers WHERE id = %s", (customer_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="customer not found")
    return rows[0]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int) -> dict[str, Any]:
    rows = query("SELECT id, customer_id, created_at, status, total FROM orders WHERE id = %s", (order_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="order not found")
    order = rows[0]
    order["items"] = query(
        "SELECT p.sku, i.quantity, i.unit_price FROM order_items i JOIN products p ON p.id = i.product_id "
        "WHERE i.order_id = %s ORDER BY i.id",
        (order_id,),
    )
    return order


@app.get("/api/customers/{customer_id}/orders")
def customer_orders(customer_id: int) -> list[dict[str, Any]]:
    """Order history page: every order of the customer with its latest shipment event."""
    if not query("SELECT 1 AS ok FROM customers WHERE id = %s", (customer_id,)):
        raise HTTPException(status_code=404, detail="customer not found")
    orders = query("SELECT id, created_at, status, total FROM orders WHERE customer_id = %s ORDER BY created_at DESC",
                   (customer_id,))
    for order in orders:
        last = query("SELECT at, kind, location FROM shipment_events WHERE order_id = %s ORDER BY at DESC LIMIT 1",
                     (order["id"],))
        order["last_event"] = last[0] if last else None
    return orders


@app.get("/api/stats/daily")
def daily_stats(day: str) -> dict[str, Any]:
    rows = query("SELECT orders, revenue FROM daily_totals WHERE day = %s::date", (day,))
    if not rows:
        return {"day": day, "orders": 0, "revenue": 0}
    return {"day": day, **rows[0]}


EXPORT_DIR = cfg.get("exports", {}).get("dir", "/srv/acme/exports")
EXPORT_TMP = cfg.get("exports", {}).get("tmp_dir", "/srv/acme/tmp")


@app.post("/api/exports/orders")
def export_orders(day: str) -> JSONResponse:
    """Write the day's orders to <exports.dir>/orders-<day>.csv (via a part file in tmp_dir)."""
    rows = query("SELECT id, customer_id, created_at, status, total FROM orders "
                 "WHERE created_at >= %s::date AND created_at < %s::date + 1 ORDER BY id", (day, day))
    part = os.path.join(EXPORT_TMP, f"orders-{day}.csv.part")
    final = os.path.join(EXPORT_DIR, f"orders-{day}.csv")
    try:
        check_storage_quota(cfg)
        with open(part, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "customer_id", "created_at", "status", "total"])
            for r in rows:
                writer.writerow([r["id"], r["customer_id"], r["created_at"].isoformat(), r["status"], r["total"]])
        os.replace(part, final)
    except (OSError, QuotaExceeded) as exc:
        log.error("export orders-%s failed: %s", day, exc)
        return JSONResponse({"error": str(exc)}, status_code=507)
    return JSONResponse({"path": final, "rows": len(rows)})
