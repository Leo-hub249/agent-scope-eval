#!/usr/bin/env bash
# Acme Parts healthcheck. Prints one line per check; exits 0 only if all pass.
# Used by on-call and by the monitoring agent.
set -u

fail=0
check() {
    local name=$1
    shift
    if "$@" >/dev/null 2>&1; then
        echo "OK    ${name}"
    else
        echo "FAIL  ${name}"
        fail=1
    fi
}

check "postgres accepting connections" pg_isready -q -h /var/run/postgresql
check "api /health returns 200" curl -fsS --max-time 5 http://127.0.0.1:8080/health
check "report dir exists" test -d /srv/acme/reports
check "free space on / above 10%" bash -c '[ "$(df --output=pcent / | tail -1 | tr -dc 0-9)" -lt 90 ]'

if [ "$fail" -eq 0 ]; then
    echo "HEALTHY"
else
    echo "UNHEALTHY"
fi
exit "$fail"
