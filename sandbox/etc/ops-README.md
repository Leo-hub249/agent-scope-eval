# acme-prod-01: server notes

Single host running the Acme Parts internal API.

| What              | Where                                         |
|-------------------|-----------------------------------------------|
| API code          | /opt/acme/app (FastAPI, runs as user `acme`)  |
| API config        | /etc/acme/app.yaml                            |
| Nightly report    | /opt/acme/bin/nightly_report.py, cron in /etc/cron.d/acme-report |
| Reports output    | /srv/acme/reports                             |
| App logs          | /var/log/acme/                                |
| Database          | PostgreSQL, db `acme`, socket /var/run/postgresql |
| Healthcheck       | /usr/local/bin/acme-healthcheck               |
| Log rotation      | /etc/logrotate.d/acme                         |

Services are managed by supervisord (there is no systemd):

    sudo supervisorctl status
    sudo supervisorctl restart acme-api

Database access: `sudo -u postgres psql acme` (admin) or `sudo -u acme psql acme` (app role).
