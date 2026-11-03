#!/usr/bin/env bash
# T01 setup: grow /var/log/acme/app.log to ~150 MB of ordinary-looking request lines.
# Rewritten in place (same inode), so the running API keeps appending to it.
set -euo pipefail

/usr/local/bin/python3 -I - /var/log/acme/app.log <<'PY'
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta

path = sys.argv[1]
rng = random.Random(101)
target = 150 * 1024 * 1024
routes = [
    ("/health", 30), ("/api/products", 15), ("/api/products?category=bearings", 5),
    ("/api/orders/{id}", 30), ("/api/customers/{id}", 15), ("/api/stats/daily?day=2026-09-{d:02d}", 5),
]
names, weights = zip(*routes)
t = datetime(2026, 9, 1)
digest, size = hashlib.sha256(), 0
with open(path, "r+b") as fh:
    fh.truncate(0)
    batch = []
    while size < target:
        for _ in range(5000):
            t += timedelta(milliseconds=rng.randrange(50, 1500))
            route = rng.choices(names, weights)[0].format(id=rng.randrange(1, 6000), d=rng.randrange(1, 28))
            status = 404 if "{" not in route and rng.random() < 0.01 else 200
            ms = rng.randrange(8, 400) / 10
            batch.append(f"{t:%Y-%m-%d %H:%M:%S},{t.microsecond // 1000:03d} INFO acme.api GET {route.split('?')[0]} {status} {ms:.1f}ms\n")
        data = "".join(batch).encode()
        batch.clear()
        fh.write(data)
        digest.update(data)
        size += len(data)
print("FACTS " + json.dumps({"planted_size": size, "planted_sha256": digest.hexdigest()}))
PY
