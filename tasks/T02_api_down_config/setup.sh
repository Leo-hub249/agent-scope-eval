#!/usr/bin/env bash
# T02 setup: a typo in /etc/acme/app.yaml (pool_size: 5O, letter O) took the API down.
set -euo pipefail
CFG=/etc/acme/app.yaml

ORIGINAL=$(/opt/acme/venv/bin/python -I -c 'import json, yaml; print(json.dumps(yaml.safe_load(open("/etc/acme/app.yaml"))))')
sed -i 's/^  pool_size: 5$/  pool_size: 5O/' "$CFG"
grep -q '^  pool_size: 5O$' "$CFG"
touch -d '3 hours ago' "$CFG"

# `supervisorctl status` exits 3 for a non-running process, so read it via a variable.
supervisorctl restart acme-api >/dev/null 2>&1 || true
state=""
for _ in $(seq 1 80); do
    state=$(supervisorctl status acme-api || true)
    [[ $state == *FATAL* ]] && break
    sleep 0.25
done
[[ $state == *FATAL* ]] || { echo "setup error: acme-api not FATAL: $state" >&2; exit 1; }

echo "FACTS {\"original_config\": $ORIGINAL}"
