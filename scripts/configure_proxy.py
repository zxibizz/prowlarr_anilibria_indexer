#!/usr/bin/env python3
"""Register the AniLibria proxy with Prowlarr as an "Http" indexer proxy.

Creates a tag, an Http indexer proxy pointed at the proxy service, and assigns
that tag to the AniLibria indexer. Prowlarr then routes that indexer's traffic
through the proxy, which answers the definition's search route and relays
everything else (downloads, release pages, its own proxy health check) to the
real site.

This is what to do by hand in *Settings -> Indexer Proxies*, plus a tag, plus the
tag on the indexer.

Usage:
    python3 scripts/configure_proxy.py                     # anilibria-proxy:8788
    python3 scripts/configure_proxy.py myhost.local:8788   # proxy elsewhere
    python3 scripts/configure_proxy.py --remove            # undo

Prowlarr validates the address when it saves, so an unreachable host is rejected
with a clear "Connection refused (host:port)" rather than silently breaking.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696")
PROXY_NAME = "anilibria-proxy"
TAG_LABEL = "anilibria-proxy"
DEFAULT_ADDRESS = "anilibria-proxy:8788"
DEFAULT_PORT = "8788"
DEFINITION = "anilibria"


def api_key() -> str:
    key = os.environ.get("PROWLARR_API_KEY")
    if key:
        return key
    config = os.path.join(ROOT, "config", "config.xml")
    if not os.path.exists(config):
        sys.exit(f"No API key: set PROWLARR_API_KEY or start the stack ({config} missing).")
    return ET.parse(config).getroot().find("ApiKey").text.strip()


KEY = api_key()


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{PROWLARR}{path}", data=data, method=method)
    request.add_header("X-Api-Key", KEY)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode()
        return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:400]


def find_tag():
    _, tags = api("/api/v1/tag")
    return next((t for t in (tags or []) if t.get("label") == TAG_LABEL), None)


def find_proxy():
    _, proxies = api("/api/v1/indexerproxy")
    return next((p for p in (proxies or []) if p.get("name") == PROXY_NAME), None)


def find_indexer():
    _, indexers = api("/api/v1/indexer")
    return next((i for i in (indexers or []) if i.get("definitionName") == DEFINITION), None)


def remove() -> int:
    indexer = find_indexer()
    if indexer and indexer.get("tags"):
        indexer["tags"] = []
        api(f"/api/v1/indexer/{indexer['id']}?forceSave=true", "PUT", indexer)
        print("cleared tags on the AniLibria indexer")

    proxy = find_proxy()
    if proxy:
        api(f"/api/v1/indexerproxy/{proxy['id']}", "DELETE")
        print(f"deleted indexer proxy {proxy['id']}")

    tag = find_tag()
    if tag:
        api(f"/api/v1/tag/{tag['id']}", "DELETE")
        print(f"deleted tag {tag['id']}")

    print("\nNote: the definition's search route is answered by the proxy, so the")
    print("indexer will not return search results until a proxy is assigned again.")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--remove" in sys.argv:
        return remove()

    address = args[0] if args else DEFAULT_ADDRESS
    host, _, port = address.rpartition(":")
    if not host:
        host, port = address, DEFAULT_PORT

    status, tag = api("/api/v1/tag", "POST", {"label": TAG_LABEL})
    if not isinstance(tag, dict) or "id" not in tag:
        tag = find_tag()
    if not tag:
        print(f"error: could not create the tag (HTTP {status})", file=sys.stderr)
        return 1
    print(f"tag          : id={tag['id']} label={tag['label']}")

    _, schema = api("/api/v1/indexerproxy/schema")
    http_proxy = next((s for s in (schema or []) if s.get("implementation") == "Http"), None)
    if not http_proxy:
        print("error: this Prowlarr has no Http indexer proxy implementation", file=sys.stderr)
        return 2

    fields = json.loads(json.dumps(http_proxy["fields"]))
    for field in fields:
        if field["name"] == "host":
            field["value"] = host
        if field["name"] == "port":
            field["value"] = port

    existing = find_proxy()
    payload = {
        "name": PROXY_NAME,
        "implementation": http_proxy["implementation"],
        "configContract": http_proxy["configContract"],
        "fields": fields,
        "tags": [tag["id"]],
    }
    if existing:
        payload["id"] = existing["id"]
        status, proxy = api(f"/api/v1/indexerproxy/{existing['id']}", "PUT", payload)
    else:
        status, proxy = api("/api/v1/indexerproxy", "POST", payload)

    if not isinstance(proxy, dict):
        print(f"error: proxy not saved (HTTP {status}): {json.dumps(proxy)[:400]}", file=sys.stderr)
        return 1
    print(f"indexer proxy: id={proxy['id']} -> {host}:{port} (tag {tag['id']})")

    indexer = find_indexer()
    if indexer:
        indexer["tags"] = sorted(set((indexer.get("tags") or []) + [tag["id"]]))
        status, saved = api(f"/api/v1/indexer/{indexer['id']}?forceSave=true", "PUT", indexer)
        assigned = saved.get("tags") if isinstance(saved, dict) else status
        print(f"indexer      : id={indexer['id']} tags={assigned}")
    else:
        # No indexer yet. It has to be created with the tag already on it:
        # saving an indexer runs a test search, and that search only succeeds
        # once the proxy is in play.
        _, schema = api("/api/v1/indexer/schema")
        match = next((s for s in (schema or []) if s.get("definitionName") == DEFINITION), None)
        if not match:
            print(f"\nerror: definition {DEFINITION!r} is not loaded; is definitions/ mounted?")
            return 1

        _, profiles = api("/api/v1/appprofile")
        status, created = api(
            "/api/v1/indexer",
            "POST",
            {
                "name": "AniLibria",
                "implementation": match["implementation"],
                "configContract": match["configContract"],
                "definitionName": DEFINITION,
                "enable": True,
                "protocol": "torrent",
                "priority": 25,
                "appProfileId": profiles[0]["id"] if profiles else 1,
                "tags": [tag["id"]],
                "fields": match.get("fields", []),
            },
        )
        if not isinstance(created, dict):
            print(f"\nerror: could not add the indexer (HTTP {status})", file=sys.stderr)
            for entry in created if isinstance(created, list) else [created]:
                if isinstance(entry, dict):
                    print(f"  {entry.get('errorMessage')}", file=sys.stderr)
            return 1
        print(f"indexer      : id={created['id']} (created, tags={created.get('tags')})")

    print("\nCheck that traffic really goes through the proxy:")
    print("  docker compose logs --tail=30 anilibria-proxy")
    print("    DIVERT GET aniliberty.top/_anilibria_proxy/search q='naruto' limit=50")
    print("    tunnel aniliberty.top:443          <- the .torrent download")
    print("    tunnel prowlarr.servarr.com:443    <- Prowlarr's proxy health check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
