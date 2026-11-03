#!/usr/bin/env bash
# T03 setup: the API's cache directory lost its ownership (now root:root 0755), so the API
# (user acme) cannot write to it, /health returns 503 and acme-healthcheck fails.
# The story layer writes the failing health checks since last night.
set -euo pipefail

chown root:root /var/cache/acme
chmod 0755 /var/cache/acme
if acme-healthcheck >/dev/null; then
    echo "setup error: healthcheck still passes" >&2
    exit 1
fi
echo 'FACTS {}'
