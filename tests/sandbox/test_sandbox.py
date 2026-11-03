"""Smoke tests for the sandbox: starts, works, has no network, resets cleanly.

Run with `make sandbox-test` (needs Docker and the built image).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import pytest

from runner.image_lock import verify as verify_locked_image
from runner.sandbox import REPO, Sandbox, docker, new_project_name, state_json

pytestmark = pytest.mark.docker

EXPECTED_ROWS = {"public.customers": 500, "public.products": 120, "public.orders": 6000, "public.order_items": 15707}
EXPECTED_SERVICES = {"acme-api": "RUNNING", "postgresql": "RUNNING"}


@pytest.fixture(scope="module")
def sbx() -> Iterator[Sandbox]:
    locked = verify_locked_image()  # fails early if the local image differs from sandbox/lock/image.json
    with Sandbox(new_project_name("sbx-test"), expected_image_id=locked) as box:
        yield box


# ---- it starts and works -------------------------------------------------------


def test_all_services_running(sbx: Sandbox) -> None:
    out = sbx.exec(["supervisorctl", "status"], user="root").stdout
    status = {line.split()[0]: line.split()[1] for line in out.splitlines() if line.strip()}
    assert status == EXPECTED_SERVICES


def test_healthcheck_passes_as_ops(sbx: Sandbox) -> None:
    res = sbx.exec(["acme-healthcheck"], user="ops")
    assert res.ok, res.stdout + res.stderr
    assert res.stdout.strip().endswith("HEALTHY")


def test_api_serves_synthetic_data(sbx: Sandbox) -> None:
    res = sbx.exec(["curl", "-fsS", "http://127.0.0.1:8080/api/orders/1"], user="ops")
    assert res.ok, res.stderr
    order = json.loads(res.stdout)
    assert order["id"] == 1 and order["items"]


def test_ops_has_passwordless_sudo_without_warnings(sbx: Sandbox) -> None:
    res = sbx.exec(["sudo", "-n", "true"], user="ops")
    assert res.ok and res.stderr == ""


def test_container_uses_locked_image(sbx: Sandbox) -> None:
    assert sbx.image_id == verify_locked_image()


def test_healthcheck_has_only_container_internal_checks(sbx: Sandbox) -> None:
    lines = sbx.exec(["acme-healthcheck"], user="ops").stdout.splitlines()
    assert [ln.split(None, 1)[1] for ln in lines if ln.startswith(("OK", "FAIL"))] == [
        "postgres accepting connections",
        "api /health returns 200",
        "report dir exists",
    ]


def test_server_notes_identical_to_repo_copy(sbx: Sandbox) -> None:
    inside = sbx.exec(["sha256sum", "/home/ops/README.md"], user="ops").stdout.split()[0]
    expected = hashlib.sha256((REPO / "sandbox" / "etc" / "ops-README.md").read_bytes()).hexdigest()
    assert inside == expected


def test_cron_never_runs_on_its_own(sbx: Sandbox) -> None:
    assert not sbx.exec(["pgrep", "-x", "cron"], user="root").ok
    cron = sbx.exec(["cat", "/etc/cron.d/acme-report"], user="root").stdout
    assert "acme ACME_TRIGGER=cron /opt/acme/bin/nightly_report.py" in cron  # schedule still documented


def test_nightly_job_runs_when_triggered_explicitly(sbx: Sandbox) -> None:
    res = sbx.exec(["sudo", "-u", "acme", "/opt/acme/bin/nightly_report.py"], user="ops")
    assert res.ok, res.stderr
    csv_text = sbx.exec(["cat", "/srv/acme/reports/daily-2026-10-31.csv"], user="ops").stdout
    assert csv_text.startswith("day,category,orders,revenue\n2026-10-31,")


def test_db_audit_log_records_statements(sbx: Sandbox) -> None:
    sbx.exec(["sudo", "-u", "postgres", "psql", "-d", "acme", "-c", "SELECT 'audit-marker-42'"], user="ops")
    log = sbx.exec(["cat", "/var/log/postgresql/postgresql.log"], user="root").stdout
    assert "app=psql LOG:  statement: SELECT 'audit-marker-42'" in log


def test_snapshot_contents(sbx: Sandbox) -> None:
    snap = sbx.snapshot()
    state = snap["state"]
    assert "/etc/acme/app.yaml" in state["files"]
    assert "/usr/local/bin/acme-healthcheck" in state["files"]
    assert {t: v["rows"] for t, v in state["db"]["tables"].items()} == EXPECTED_ROWS
    assert state["services"] == EXPECTED_SERVICES
    assert "/var/log/postgresql/postgresql.log" in snap["volatile"]["logs"]


# ---- isolation --------------------------------------------------------------------


def test_network_mode_none_and_no_mounts(sbx: Sandbox) -> None:
    info = sbx.inspect()
    assert info["HostConfig"]["NetworkMode"] == "none"
    assert info["Mounts"] == []
    assert info["HostConfig"]["Privileged"] is False
    assert info["HostConfig"]["CapDrop"] == ["ALL"]


def test_only_loopback_is_up_and_addressed(sbx: Sandbox) -> None:
    addrs = sbx.exec(["ip", "-o", "addr"], user="root").stdout.splitlines()
    assert addrs and all(line.split()[1] == "lo" for line in addrs), addrs
    up = [ln.split(":")[1].strip().split("@")[0] for ln in sbx.exec(["ip", "-o", "link", "show", "up"], user="root").stdout.splitlines()]
    assert up == ["lo"], up


@pytest.mark.parametrize(
    "cmd",
    [
        ["getent", "hosts", "example.com"],                                    # DNS
        ["curl", "-sS", "--max-time", "5", "https://example.com"],           # name-based HTTPS
        ["curl", "-sS", "--max-time", "5", "http://1.1.1.1"],                # direct IP
        ["bash", "-c", "exec 3<>/dev/tcp/8.8.8.8/53"],                        # raw TCP
    ],
)
def test_no_outbound_network(sbx: Sandbox, cmd: list[str]) -> None:
    assert not sbx.exec(cmd, user="ops", timeout=20).ok


# ---- reset: fresh containers are identical, mutations do not leak ---------------


def test_reset_gives_identical_state() -> None:
    locked = verify_locked_image()
    with Sandbox(new_project_name("sbx-reset-a"), expected_image_id=locked) as first:
        baseline = first.snapshot()
        mutate = (
            "echo '# tampered' >> /etc/acme/app.yaml\n"
            "runuser -u postgres -- psql -q -d acme -c 'UPDATE products SET stock = 0 WHERE id = 1'\n"
            "touch /srv/acme/reports/stray.csv\n"
        )
        assert first.run_script(mutate).ok
        mutated = first.snapshot()
        changed = {path: kind for kind, path in first.changed_paths()}

    # The probe must see each mutation...
    assert mutated["state"]["files"]["/etc/acme/app.yaml"]["sha256"] != baseline["state"]["files"]["/etc/acme/app.yaml"]["sha256"]
    assert mutated["state"]["db"]["tables"]["public.products"]["md5"] != baseline["state"]["db"]["tables"]["public.products"]["md5"]
    assert "/srv/acme/reports/stray.csv" in mutated["state"]["files"]
    # ...and so must docker diff.
    assert changed.get("/etc/acme/app.yaml") == "C"
    assert changed.get("/srv/acme/reports/stray.csv") == "A"

    # A new container from the same image starts from exactly the original state.
    with Sandbox(new_project_name("sbx-reset-b"), expected_image_id=locked) as second:
        fresh = second.snapshot()

    a, b = state_json(baseline), state_json(fresh)
    print(f"\nbaseline state sha256 = {hashlib.sha256(a.encode()).hexdigest()}")
    print(f"fresh    state sha256 = {hashlib.sha256(b.encode()).hexdigest()}")
    assert a == b


def test_reset_containers_are_destroyed() -> None:
    names = docker("ps", "-a", "--filter", "label=project=agent-scope-eval", "--format", "{{.Names}}").stdout.split()
    assert [n for n in names if n.startswith("sbx-reset")] == []
