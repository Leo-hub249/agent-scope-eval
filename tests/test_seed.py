"""The synthetic data must be deterministic and internally consistent."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
from decimal import Decimal
from pathlib import Path
from types import ModuleType

SEED_SCRIPT = Path(__file__).resolve().parent.parent / "sandbox" / "db" / "generate_seed.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_seed", SEED_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(seed: int | None = None) -> str:
    gen = _load()
    buf = io.StringIO()
    gen.generate(buf, seed if seed is not None else gen.SEED)
    return buf.getvalue()


def _copy_rows(sql: str, table: str) -> list[list[str]]:
    block = re.search(rf"COPY {table} \(.*?\) FROM stdin;\n(.*?)\\\.\n", sql, re.S)
    assert block, f"no COPY block for {table}"
    return [line.split("\t") for line in block.group(1).splitlines()]


def test_same_seed_gives_identical_output() -> None:
    a, b = _render(), _render()
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()


def test_different_seed_changes_output() -> None:
    assert _render(1) != _render(2)


def test_row_counts() -> None:
    gen = _load()
    sql = _render()
    assert len(_copy_rows(sql, "customers")) == gen.N_CUSTOMERS
    assert len(_copy_rows(sql, "products")) == gen.N_PRODUCTS
    assert len(_copy_rows(sql, "orders")) == gen.N_ORDERS


def test_emails_use_reserved_example_domains() -> None:
    for row in _copy_rows(_render(), "customers"):
        assert row[2].rsplit("@", 1)[1] in {"example.com", "example.org", "example.net"}


def test_order_totals_match_items() -> None:
    sql = _render()
    totals = {row[0]: Decimal(row[4]) for row in _copy_rows(sql, "orders")}
    sums: dict[str, Decimal] = {}
    for _, order_id, _, qty, unit in _copy_rows(sql, "order_items"):
        sums[order_id] = sums.get(order_id, Decimal("0")) + Decimal(unit) * int(qty)
    assert sums == totals
