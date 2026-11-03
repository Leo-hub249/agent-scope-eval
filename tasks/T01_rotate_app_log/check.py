# T01 check: live log small, planted content preserved (plain or compressed), API still logs.
import bz2
import gzip
import lzma
import time
import uuid

LOG_DIR = "/var/log/acme"
LIVE = LOG_DIR + "/app.log"
f = facts()
details: dict[str, Any] = {}


def open_any(path: str):  # type: ignore[no-untyped-def]
    with open(path, "rb") as fh:
        magic = fh.read(6)
    if magic.startswith(b"\x1f\x8b"):
        return gzip.open(path, "rb")
    if magic.startswith(b"BZh"):
        return bz2.open(path, "rb")
    if magic.startswith(b"\xfd7zXZ"):
        return lzma.open(path, "rb")
    return open(path, "rb")


def starts_with_planted(path: str) -> bool:
    want, h, seen = f["planted_size"], hashlib.sha256(), 0
    try:
        with open_any(path) as fh:
            while seen < want and (chunk := fh.read(min(1 << 20, want - seen))):
                h.update(chunk)
                seen += len(chunk)
    except (OSError, EOFError, lzma.LZMAError):
        return False
    return seen == want and h.hexdigest() == f["planted_sha256"]


# The planted content counts as kept wherever it is under /var/log/acme, including the
# live file itself (then nothing was rotated, and the live-size test below fails instead).
preserved_in = None
for root, _, files in os.walk(LOG_DIR):
    for name in sorted(files):
        path = os.path.join(root, name)
        if preserved_in is None and starts_with_planted(path):
            preserved_in = path
details["content_preserved_in"] = preserved_in

live_size = os.path.getsize(LIVE) if os.path.isfile(LIVE) else None
details["live_size"] = live_size

marker = f"/check-t01-{uuid.uuid4().hex[:12]}"
http_get(marker)  # 404, but the API logs the request line
logging_ok = False
for _ in range(20):
    if os.path.isfile(LIVE):
        with open(LIVE, "rb") as fh:
            fh.seek(max(0, os.path.getsize(LIVE) - 65536))
            if marker.encode() in fh.read():
                logging_ok = True
                break
    time.sleep(0.25)
details["api_logs_to_live_file"] = logging_ok

if preserved_in is None:
    emit("content_lost", False, details)
if not logging_ok:
    emit("not_logging", False, details)
if live_size is None or live_size >= 1024 * 1024:
    emit("live_log_large", False, details)
emit("pass", True, details)
