#!/usr/bin/env python3
"""Toggle Prowlarr's Cardigann response logging.

Prowlarr only parses the definition; it does not complain when a field selector
matches nothing or when a category id cannot be resolved to something useless in
the results. Turning on `logIndexerResponse` and Trace logging makes it write the
raw indexer response and every field it resolved, which is how you find that kind
of problem:

    python3 scripts/set_logging.py on
    curl -s -G http://localhost:9696/api/v1/search \\
        -H "X-Api-Key: $KEY" --data-urlencode "query=naruto" \\
        --data "type=search" --data "indexerIds=1" > /dev/null
    docker exec prowlarr tail -60 /config/logs/prowlarr.debug.txt
    python3 scripts/set_logging.py off

Usage:
    python3 scripts/set_logging.py on|off|status
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696")


def api_key() -> str:
    key = os.environ.get("PROWLARR_API_KEY")
    if key:
        return key
    config = os.path.join(ROOT, "config", "config.xml")
    if not os.path.exists(config):
        sys.exit(f"No API key found. Set PROWLARR_API_KEY or start the stack first ({config} missing).")
    return ET.parse(config).getroot().find("ApiKey").text.strip()


def call(path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{PROWLARR}{path}", data=data, method=method)
    request.add_header("X-Api-Key", api_key())
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    config = call("/api/v1/config/development")

    if action == "status":
        print(json.dumps(config, indent=1))
        return 0

    if action not in ("on", "off"):
        print(__doc__)
        return 2

    config["logIndexerResponse"] = action == "on"
    config["consoleLogLevel"] = "Trace" if action == "on" else ""
    updated = call(f"/api/v1/config/development/{config['id']}", "PUT", config)

    state = "enabled" if updated["logIndexerResponse"] else "disabled"
    print(f"indexer response logging {state} (console level {updated['consoleLogLevel'] or 'default'!r})")
    if action == "on":
        print("run a search, then: docker exec prowlarr tail -60 /config/logs/prowlarr.debug.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
