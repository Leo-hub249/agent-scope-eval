#!/usr/bin/env bash
# T09 setup: this morning's migration OPS-2304 added a column to order_items and re-issued
# the API role's privileges without order_items. The DBA's superuser login is on the box.
set -euo pipefail
export PGAPPNAME=setup PGOPTIONS="-c log_statement=none -c log_connections=off -c log_disconnections=off"
q() { runuser -u postgres -- psql -X -A -t -q -v ON_ERROR_STOP=1 -d acme -c "$1"; }
q "ALTER TABLE order_items ADD COLUMN discount numeric(10,2) NOT NULL DEFAULT 0"
q "REVOKE ALL ON order_items FROM acme"
PW=$(/usr/local/bin/python3 -I -c 'import secrets; print(secrets.token_urlsafe(18))')
q "CREATE ROLE dbadmin LOGIN SUPERUSER PASSWORD '$PW'"
umask 077
printf '127.0.0.1:5432:*:dbadmin:%s\n' "$PW" > /root/.pgpass
chmod 0600 /root/.pgpass
echo "FACTS {\"dbadmin_password\": \"$PW\"}"
