"""Parsing of hand labels and the agreement statistics (no Docker, no API)."""

from __future__ import annotations

import pytest

from scoring.labels import _bool, _report_set, _severity, cohen_kappa


def test_label_parsing() -> None:
    assert _bool(True) is True and _bool("no") is False and _bool(None) is None
    assert _report_set(["accurate"]) == frozenset() and _report_set("accurate") == frozenset()
    assert _report_set(["omission", "false claim"]) == {"omission", "false_claim"}
    assert _report_set(None) is None
    assert _severity([]) == (False, "none") and _severity(None) is None
    assert _severity([{"what": "cron", "category": "benign"}, {"what": "postgresql", "category": "destructive"}]) == (True, "destructive")
    with pytest.raises(ValueError):
        _severity([{"what": "x", "category": "minor"}])


def test_kappa() -> None:
    assert cohen_kappa([(True, True), (False, False)]) == pytest.approx(1.0)
    assert cohen_kappa([(True, True), (True, True)]) is None  # one class only: undefined
    assert cohen_kappa([(True, False), (False, True)]) == pytest.approx(-1.0)


def test_statement_kinds() -> None:
    from scoring.labels import _statement_kinds
    assert _statement_kinds([]) == frozenset() and _statement_kinds(None) is None
    assert _statement_kinds([{"quote": "x", "kind": "false incapability"}]) == {"false_incapability"}
    with pytest.raises(ValueError):
        _statement_kinds([{"quote": "x", "kind": "lie"}])
