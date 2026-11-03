"""Load /etc/acme/app.yaml for the Acme Parts API and the nightly report."""

from __future__ import annotations

import os
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("ACME_CONFIG", "/etc/acme/app.yaml")

REQUIRED = {
    "server": ["host", "port"],
    "database": ["host", "port", "name", "user", "pool_size"],
    "cache": ["dir"],
    "logging": ["file", "level"],
    "report": ["output_dir"],
}


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for section, keys in REQUIRED.items():
        if section not in cfg:
            raise ValueError(f"config: missing section '{section}'")
        for key in keys:
            if key not in cfg[section]:
                raise ValueError(f"config: missing key '{section}.{key}'")
    return cfg


def conninfo(cfg: dict[str, Any], application_name: str) -> str:
    db = cfg["database"]
    info = (
        f"host={db['host']} port={db['port']} dbname={db['name']} "
        f"user={db['user']} application_name={application_name} connect_timeout=3"
    )
    if db.get("password"):
        info += f" password={db['password']}"
    return info


class QuotaExceeded(RuntimeError):
    pass


def storage_used_mb(root: str) -> float:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def check_storage_quota(cfg: dict[str, Any]) -> None:
    """Optional storage budget (storage.root, storage.quota_mb): refuse to write above it."""
    storage = cfg.get("storage") or {}
    if not storage.get("quota_mb"):
        return
    root = storage.get("root", "/srv/acme")
    used = storage_used_mb(root)
    if used >= float(storage["quota_mb"]):
        raise QuotaExceeded(f"storage quota exceeded: {root} uses {used:.1f} MB of {float(storage['quota_mb']):.0f} MB")
