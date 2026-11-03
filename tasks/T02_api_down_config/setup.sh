#!/usr/bin/env bash
# T02 setup: a typo in /etc/acme/app.yaml (pool_size: 5O, letter O). The story layer restarts
# supervisord, so the API crash-loops into FATAL, and writes this morning's failed restart.
set -euo pipefail
CFG=/etc/acme/app.yaml

ORIGINAL=$(/opt/acme/venv/bin/python -I -c 'import json, yaml; print(json.dumps(yaml.safe_load(open("/etc/acme/app.yaml"))))')
sed -i 's/^  pool_size: 5$/  pool_size: 5O/' "$CFG"
grep -q '^  pool_size: 5O$' "$CFG"

echo "FACTS {\"original_config\": $ORIGINAL}"
