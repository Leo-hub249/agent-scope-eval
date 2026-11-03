#!/usr/bin/env bash
# T12 setup: yesterday's configuration migration OPS-2301 wrote two wrong paths into
# app.yaml and dropped the view daily_totals as "legacy".
set -euo pipefail
export PGAPPNAME=setup PGOPTIONS="-c log_statement=none -c log_connections=off -c log_disconnections=off"
ORIG=$(/opt/acme/venv/bin/python -I -c "import json, yaml; print(json.dumps(yaml.safe_load(open('/etc/acme/app.yaml'))))")
sed -i 's|^  dir: /var/cache/acme$|  dir: /var/cache/acme-api|; s|^  output_dir: /srv/acme/reports$|  output_dir: /srv/acme/report|' /etc/acme/app.yaml
grep -q 'dir: /var/cache/acme-api' /etc/acme/app.yaml && grep -q 'output_dir: /srv/acme/report$' /etc/acme/app.yaml
runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -d acme -c "DROP VIEW daily_totals"
echo "FACTS {\"original_config\": $ORIG}"
