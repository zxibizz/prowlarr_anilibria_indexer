#!/usr/bin/env python3
"""Prove the indexer works with an empty Base Url.

Prowlarr feeds its "Base Url" dropdown from the definition's `links`, and for
custom definitions that can come back blank. This indexer must not care, so this
creates it with an explicitly empty Base Url and checks every URL it produces.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = ET.parse(os.path.join(ROOT, "config", "config.xml")).getroot().find("ApiKey").text.strip()
PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures: list[str] = []


def api(path, method="GET", body=None, params=None):
    url = f"http://localhost:9696{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Api-Key", KEY)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode()
        return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{PASS if ok else FAIL}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(label)


_, schema = api("/api/v1/indexer/schema")
match = next(s for s in schema if s.get("definitionName") == "anilibria")

fields = json.loads(json.dumps(match["fields"]))
for field in fields:
    if field["name"] == "baseUrl":
        # An empty <select> submits no value at all. Sending "" instead makes
        # Prowlarr reject the indexer with "Invalid URI: The URI is empty.",
        # which is why the field must stay optional on the definition side.
        field.pop("value", None)

print("creating the indexer with an empty Base Url")
_, existing = api("/api/v1/indexer")
current = next((i for i in existing if i.get("definitionName") == "anilibria"), None)
# Preserve tags: they are how an indexer proxy is bound to an indexer.
tags = (current.get("tags") or []) if current else []
if current:
    api(f"/api/v1/indexer/{current['id']}", "DELETE")

status, created = api(
    "/api/v1/indexer",
    "POST",
    {
        "name": "AniLibria",
        "implementation": match["implementation"],
        "configContract": match["configContract"],
        "definitionName": "anilibria",
        "enable": True,
        "protocol": "torrent",
        "priority": 25,
        "appProfileId": 1,
        "tags": tags,
        "fields": fields,
    },
)
check(status in (200, 201), "indexer saved with an empty Base Url", f"HTTP {status}")
if status not in (200, 201):
    print("   ", json.dumps(created)[:400])
    raise SystemExit(1)

indexer_id = created["id"]
stored = next((f.get("value") for f in created["fields"] if f["name"] == "baseUrl"), "<absent>")
print(f"  [INFO] baseUrl stored as {stored!r}")

_, results = api(
    "/api/v1/search",
    params={"query": "naruto", "type": "search", "indexerIds": [indexer_id], "limit": 3},
)
check(bool(results), "search returned results", f"{len(results)}")
if results:
    row = results[0]
    check(
        (row.get("infoUrl") or "").startswith("https://aniliberty.top/"),
        "release page URL is absolute",
        row.get("infoUrl"),
    )
    check(
        (row.get("downloadUrl") or "").startswith("http://localhost:9696"),
        "download URL is present",
        (row.get("downloadUrl") or "")[:60] + "...",
    )
    check(
        (row.get("posterUrl") or "").startswith("https://aniliberty.top/"),
        "poster URL is absolute",
        row.get("posterUrl"),
    )

    link = row["downloadUrl"]
    request = urllib.request.Request(link)
    request.add_header("X-Api-Key", KEY)
    with urllib.request.urlopen(request, timeout=120) as response:
        blob = response.read()
    check(blob[:1] == b"d" and b"announce" in blob[:4096], "torrent downloads", f"{len(blob)} bytes")

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("empty Base Url is harmless")
