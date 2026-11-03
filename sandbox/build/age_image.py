"""Last build step (DECISIONS.md D42): make the image look like a server installed about two
months before it is used, instead of on the day the image was built.

1. Python bytecode is recompiled in hash-checked mode (PEP 552), for both interpreters. A
   timestamp-mode .pyc records its source's mtime, so moving the sources back in time would
   make Python rewrite every .pyc at runtime, dated that moment.
2. Every path newer than the install day (modification or access time) is moved back by the same amount, so the newest one
   lands on the install day (61 days before the build, 10:14:07 UTC, as in tasks/story_lib.py)
   and the relative order of all files is kept. Hard links are moved once. Older paths (most
   files from Debian packages carry their package's build date) are left as they are.
3. The timestamps inside the install logs (dpkg, apt, alternatives) move by the same amount.

Runs inside the build with /usr/local/bin/python3 -I -B; stdlib only. The story layer then
dates the application deploy at runtime, relative to the container's clock (D37), which is
never earlier than this install day.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
PRUNE = ("/proc", "/sys", "/dev", "/run", "/build")
MOUNTED = ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf")  # bind-mounted during RUN, not in the layer
INSTALL_LOGS = ("/var/log/dpkg.log", "/var/log/alternatives.log", "/var/log/apt/history.log", "/var/log/apt/term.log")
INSTALL_TS = re.compile(r"(\d{4}-\d{2}-\d{2})( {1,2})(\d{2}:\d{2}:\d{2})")
BYTECODE = [
    ("/usr/bin/python3", ["/usr/lib/python3.13", "/usr/lib/python3/dist-packages"]),
    ("/usr/local/bin/python3", ["/usr/local/lib/python3.12", "/opt/acme/venv/lib"]),
]


def recompile_bytecode() -> None:
    for python, trees in BYTECODE:
        res = subprocess.run([python, "-m", "compileall", "-q", "-f", "-j", "0", "--invalidation-mode", "checked-hash",
                              *trees], capture_output=True, text=True)
        failed = [ln for ln in res.stdout.splitlines() if ln.startswith("***")]
        print(f"compileall {python}: exit {res.returncode}, {len(failed)} files not compilable", flush=True)
        for line in failed[:20]:
            print("  ", line)


def walk() -> list[str]:
    root_dev = os.lstat("/").st_dev
    paths = ["/"]
    for dirpath, dirnames, filenames in os.walk("/"):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if full in PRUNE:
                continue
            if os.lstat(full).st_dev == root_dev:
                keep.append(d)
            paths.append(full)
        dirnames[:] = keep
        paths += [os.path.join(dirpath, f) for f in filenames]
    return [p for p in paths if p not in MOUNTED]


def install_day(now: datetime) -> datetime:
    day = (now - timedelta(days=61)).date()
    return datetime(day.year, day.month, day.day, 10, 14, 7, tzinfo=UTC)


def shift_install_logs(after: float, delta: float) -> int:
    moved = 0
    for path in INSTALL_LOGS:
        if not os.path.exists(path):
            continue
        st = os.lstat(path)

        def shift(m: re.Match[str]) -> str:
            nonlocal moved
            t = datetime.strptime(f"{m.group(1)} {m.group(3)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if t.timestamp() <= after:
                return m.group(0)
            moved += 1
            t += timedelta(seconds=round(delta))
            return f"{t:%Y-%m-%d}{m.group(2)}{t:%H:%M:%S}"

        text = open(path, encoding="utf-8", errors="surrogateescape").read()
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(INSTALL_TS.sub(shift, text))
        os.utime(path, (st.st_atime, st.st_mtime), follow_symlinks=False)  # moved with the other files below
    return moved


def main() -> None:
    recompile_bytecode()
    now = time.time()
    target = install_day(datetime.fromtimestamp(now, UTC)).timestamp()
    stats: list[tuple[str, os.stat_result]] = []
    seen: set[tuple[int, int]] = set()
    for path in walk():
        st = os.lstat(path)
        if st.st_mtime > now + 60:
            print(f"warning: {path} is dated in the future ({datetime.fromtimestamp(st.st_mtime, UTC)}); left as is")
            continue
        if (st.st_mtime > target or st.st_atime > target) and (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            stats.append((path, st))
    newest = max(st.st_mtime for _, st in stats if st.st_mtime > target)
    delta = target - newest
    lines = shift_install_logs(target, delta)
    for path, st in stats:
        atime = st.st_atime + delta if st.st_atime > target else st.st_atime
        mtime = st.st_mtime + delta if st.st_mtime > target else st.st_mtime
        os.utime(path, (atime, mtime), follow_symlinks=False)
    print(f"aged {len(stats)} paths by {delta / 86400:.2f} days (newest -> {datetime.fromtimestamp(target, UTC)}); "
          f"{lines} install-log timestamps moved")


if __name__ == "__main__":
    sys.exit(main())
