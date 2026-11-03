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
    return (
        f"host={db['host']} port={db['port']} dbname={db['name']} "
        f"user={db['user']} application_name={application_name} connect_timeout=3"
    )
