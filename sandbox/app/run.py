"""Start the Acme Parts API with host/port taken from /etc/acme/app.yaml."""

from __future__ import annotations

import uvicorn

from acme_config import load_config

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(
        "main:app",
        host=str(cfg["server"]["host"]),
        port=int(cfg["server"]["port"]),
        access_log=False,
        log_level="info",
    )
