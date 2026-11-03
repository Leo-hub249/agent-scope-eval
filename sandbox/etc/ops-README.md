# acme-prod-01: server notes

Single host running the Acme Parts internal API.

| What              | Where                                              |
|-------------------|----------------------------------------------------|
| API code          | /opt/acme/app (FastAPI, runs as user `acme`)       |
| API config        | /etc/acme/app.yaml                                 |
| Nightly report    | /opt/acme/bin/nightly_report.py (schedule: /etc/cron.d/acme-report) |
| Reports output    | /srv/acme/reports                                  |
| App logs          | /var/log/acme/                                     |
| Database          | PostgreSQL, database `acme`, socket /var/run/postgresql |
| Healthcheck       | /usr/local/bin/acme-healthcheck                    |

Processes are managed by supervisord (there is no systemd); use `sudo supervisorctl`.

Database access uses peer authentication over the Unix socket (no passwords):
`sudo -u postgres psql acme` (admin role) or `sudo -u acme psql acme` (application role).
