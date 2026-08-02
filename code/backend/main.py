"""Sidecar entrypoint.

Binds 127.0.0.1 on an OS-assigned port (or GRINDSTONE_PORT for dev), then
announces it on stdout as one JSON line the shell waits for:

    {"event": "listening", "port": 51234, "version": "0.1.0"}

GRINDSTONE_BOOT_TOKEN must arrive via the environment (never argv — argv is
visible in the process list). Standalone dev runs may omit it; a random token
is generated and printed so curl-testing is still possible.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys

import uvicorn

from backend.app import API_VERSION, State, create_app


def main() -> int:
    boot_token = os.environ.get("GRINDSTONE_BOOT_TOKEN")
    if not boot_token:
        boot_token = secrets.token_urlsafe(32)
        print(json.dumps({"event": "dev-token", "token": boot_token}), flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", int(os.environ.get("GRINDSTONE_PORT", "0"))))
    port = sock.getsockname()[1]
    sock.listen(128)

    print(json.dumps({"event": "listening", "port": port, "version": API_VERSION}),
          flush=True)

    app = create_app(State(boot_token))
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
