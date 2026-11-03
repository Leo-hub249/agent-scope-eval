#!/bin/sh
# PID 1: recreate runtime dirs, then hand over to supervisord.
set -e
install -d -o postgres -g postgres -m 2775 /var/run/postgresql
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
