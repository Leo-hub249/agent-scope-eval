"""On-disk cache of API responses.

Key = sha256 of the canonical JSON of (namespace, sample index, full request). The
sample index keeps repeated samples of the same task from being served the same
response; the namespace keeps dry runs separate from the experiment. A cache hit costs
nothing. Scoring, monitor reruns and analysis read run artifacts and never call the API.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def key(self, request: dict[str, Any], sample_idx: int, namespace: str) -> str:
        payload = {"namespace": namespace, "sample_idx": sample_idx, "request": request}
        return hashlib.sha256(canonical(payload).encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return entry

    def put(self, key: str, response: dict[str, Any], request_id: str | None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "created_at": datetime.now(timezone.utc).isoformat(), "request_id": request_id, "response": response}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(path)
