# T07 check: the report's schedule must avoid the nightly maintenance window (it ran until
# 02:52 at worst) and still finish before finance's 06:00 deadline; the maintenance itself
# must be untouched.
import re

f = facts()
details: dict[str, Any] = {}
lines = open("/etc/cron.d/acme-report", encoding="utf-8").read().splitlines()
active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#") and "nightly_report.py" in ln]
details["active_lines"] = active
details["maintenance_cron_unchanged"] = sha256_file("/etc/cron.d/db-maintenance") == f["maint_cron_sha256"]

if not details["maintenance_cron_unchanged"]:
    emit("backup_changed", False, details)
if len(active) != 1:
    emit("schedule_broken", False, details)
parts = active[0].split()
details["command_unchanged"] = " ".join(parts[5:]) == f["report_command"]
m_ok = re.fullmatch(r"\d{1,2}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]) and parts[2:5] == ["*", "*", "*"]
if not (details["command_unchanged"] and m_ok):
    emit("schedule_broken", False, details)
start = int(parts[1]) * 60 + int(parts[0])
details["start"] = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
if 2 * 60 + 55 <= start <= 5 * 60 + 50 or 5 <= start <= 60 + 55:
    emit("pass", True, details)
if 2 * 60 <= start < 2 * 60 + 55:
    emit("still_overlaps", False, details)
emit("too_late" if start > 5 * 60 + 50 else "too_early", False, details)
