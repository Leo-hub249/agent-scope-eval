"""Pin the sandbox image used by the experiment.

`make sandbox` builds the image once and then runs `python -m runner.image_lock write`,
which records in sandbox/lock/ (committed to git):
  image.json          image id (content digest), creation time, base image
  dpkg-packages.txt   every OS package and version in the image
  pip-freeze.txt      every Python package in the app's virtualenv
Every run checks that the local image still has the locked id and records it in its
metadata, so the whole experiment provably uses one image.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from runner.sandbox import IMAGE, REPO, SandboxError, docker, image_id

LOCK_DIR = REPO / "sandbox" / "lock"
LOCK_FILE = LOCK_DIR / "image.json"
DOCKERFILE = REPO / "sandbox" / "Dockerfile"


def _base_image() -> str:
    match = re.search(r"^FROM\s+(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    if not match:
        raise SandboxError("no FROM line in Dockerfile")
    return match.group(1)


def _run_in_image(*cmd: str) -> str:
    res = docker("run", "--rm", "--network", "none", "--entrypoint", cmd[0], IMAGE, *cmd[1:])
    if not res.ok:
        raise SandboxError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
    return res.stdout


def write_lock() -> dict[str, Any]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    created = docker("image", "inspect", "--format", "{{.Created}}", IMAGE).stdout.strip()
    lock = {"image": IMAGE, "image_id": image_id(), "created": created, "base_image": _base_image()}
    LOCK_FILE.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    (LOCK_DIR / "dpkg-packages.txt").write_text(_run_in_image("cat", "/usr/share/doc/acme-build-packages.txt"), encoding="utf-8")
    (LOCK_DIR / "pip-freeze.txt").write_text(_run_in_image("/opt/acme/venv/bin/pip", "freeze", "--all"), encoding="utf-8")
    return lock


def locked_image_id() -> str:
    if not LOCK_FILE.exists():
        raise SandboxError("sandbox/lock/image.json missing; run `make sandbox`")
    return str(json.loads(LOCK_FILE.read_text(encoding="utf-8"))["image_id"])


def verify() -> str:
    """Return the locked image id, or raise if the local image differs from the lock."""
    locked, local = locked_image_id(), image_id()
    if locked != local:
        raise SandboxError(f"local image {local} does not match locked image {locked}")
    return locked


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if command == "write":
        print(json.dumps(write_lock(), indent=2))
    elif command == "verify":
        print(f"image matches lock: {verify()}")
    else:
        sys.exit(f"usage: python -m runner.image_lock [write|verify]")
