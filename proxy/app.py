#!/usr/bin/env python3
"""AniLibria search proxy for Prowlarr.

AniLibria's public API needs *two* round trips to turn a keyword search into
torrents:

    1. GET /app/search/releases?query=...   -> releases (no torrents attached)
    2. GET /anime/torrents/release/{id}     -> the torrents of one release

Prowlarr's Cardigann engine, which is what backs runtime YAML definitions in
`Definitions/Custom`, can only issue a *single* request per search
(`SearchPathBlock` has no construct for chaining requests). So a pure YAML
definition cannot implement keyword search against this API.

This service is a **forward HTTP proxy** that closes that gap. Prowlarr reaches
it through its "Http" indexer proxy, so requests arrive in absolute form
(``GET http://host/path HTTP/1.1``) and the destination is known here without
resolving it. Anything whose path starts with ``DIVERT_PREFIX`` is answered
locally with flattened search results:

    {
      "rows": [
        {
          "release": { "id": .., "alias": "..", "type": "TV", "year": ..,
                       "name_main": "..", "name_english": "..", "poster": "..", ... },
          "torrent": { "hash": "..", "label": "..", "size": .., "seeders": ..,
                       "leechers": .., "grabs": .., "quality": "1080p", ... }
        },
        ...
      ]
    }

An empty ``q`` returns the newest torrents, which is what Prowlarr uses for
indexer tests and for RSS sync.

Two routes are answered locally, both under ``DIVERT_PREFIX``::

    GET {DIVERT_PREFIX}/search?q=...                      flattened search rows
    GET {DIVERT_PREFIX}/torrent/{alias}/{order}.torrent   one .torrent file

The second route is what a release's GUID is addressed by. In the Cardigann
engine ``release.Guid`` is set from the definition's ``download`` field and
nowhere else - there is no ``guid`` field - and the torznab feed emits that GUID
verbatim, so the download URL *is* the GUID. A URL naming the infohash therefore
changes the GUID whenever the site regenerates the torrent file for the same
release, and every downstream app re-grabs it. Rows instead carry a
``download_url`` built from the release alone plus the torrent's 1-based rank by
size, descending::

    "order": 1,
    "download_url": "http://aniliberty.top/_anilibria_proxy/torrent/honzuki/1.torrent"

It names no infohash, so it survives a regenerated torrent file, and it is still
a working download: this service resolves ``alias`` + ``order`` back to whatever
the current infohash is, every time the file is asked for.

**Every other request is relayed to its real destination** - release pages,
``.torrent`` downloads and Prowlarr's own proxy health check (a TLS tunnel to
prowlarr.servarr.com) all pass straight through, so the site keeps working
normally and only the search hop changes.

That is what makes the proxy's location configurable from Prowlarr itself: the
definition uses a fixed search path on the real site, and where this service
actually runs is just the indexer proxy's host and port.

Categories, titles, dates, sizes and the poster/description fields are left to the
YAML definition, so the presentation stays in the Prowlarr config. The download
URL is the exception: it doubles as the release's GUID, so it is built here.

Local endpoints on the same listener, for humans and container health checks:

    GET /healthz    liveness probe
    GET /           short description of the endpoints
"""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_BASE = os.environ.get("ANILIBRIA_API_BASE", "https://aniliberty.top/api/v1").rstrip("/")
SITE = os.environ.get("ANILIBRIA_SITE", "https://aniliberty.top").rstrip("/")
BIND = os.environ.get("PROXY_BIND", "0.0.0.0")
PORT_TEXT = os.environ.get("PROXY_PORT", "8788")
PORT = int(PORT_TEXT) if PORT_TEXT.strip() else 0
# Path prefix the proxy answers itself. It lives on the real site's host so the
# definition's search path still resolves if the proxy is not in play.
DIVERT_PREFIX = os.environ.get("PROXY_DIVERT_PREFIX", "/_anilibria_proxy")
# Both diverted routes are addressed on the site's plain-http origin. That is not
# cosmetic: the divert only ever sees absolute-form requests ("GET http://host/path"),
# which is what Prowlarr's "Http" indexer proxy sends for plain http. An https URL
# arrives here as CONNECT and is blind-tunnelled to the site, never reaching it.
DIVERT_ORIGIN = "http://" + SITE.split("://", 1)[-1]
# {DIVERT_PREFIX}/torrent/{alias}/{order}.torrent - see the module docstring.
TORRENT_ROUTE = re.compile(
    rf"^{re.escape(DIVERT_PREFIX)}/torrent/(?P<alias>[A-Za-z0-9._~%+-]+)/(?P<order>[0-9]+)\.torrent$"
)

HTTP_TIMEOUT = float(os.environ.get("PROXY_HTTP_TIMEOUT", "30"))
# The API rejects limit > 50 on /anime/torrents with HTTP 422.
API_MAX_LIMIT = int(os.environ.get("PROXY_API_MAX_LIMIT", "50"))
# How many releases to resolve into torrents for one keyword search.
MAX_RELEASES = int(os.environ.get("PROXY_MAX_RELEASES", "10"))
# Politeness delay between the per-release requests.
REQUEST_DELAY = float(os.environ.get("PROXY_REQUEST_DELAY", "0.2"))
# Number of upstream retries for transient failures.
RETRIES = int(os.environ.get("PROXY_RETRIES", "3"))
CACHE_TTL = float(os.environ.get("PROXY_CACHE_TTL", "300"))
MAX_ROWS = int(os.environ.get("PROXY_MAX_ROWS", "500"))

USER_AGENT = os.environ.get(
    "PROXY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

_cache: dict[tuple[str, int], tuple[float, list]] = {}
_cache_lock = threading.Lock()
# Release alias (or "id:<n>") -> (expires, torrents). This is the list `order` is a
# rank in. Cached because the RSS/latest path asks for a release it has only seen
# one torrent of, and the .torrent route asks again when a file is grabbed, which
# can be days after the search that found it.
_order_cache: dict[str, tuple[float, list]] = {}
_order_lock = threading.Lock()
_order_lookups = 0
_log_lock = threading.Lock()


def log(message: str) -> None:
    with _log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def api_get(path: str, params: dict | None = None) -> object:
    """GET a JSON document from the AniLibria API, retrying transient failures."""
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    last_error: Exception | None = None
    for attempt in range(RETRIES):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (408, 425, 429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - network layer, retry then surface
            last_error = exc
            if attempt < RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise

    raise RuntimeError(f"request failed: {url}") from last_error


def _scalar(value: object) -> object:
    """AniLibria wraps many values as {"value": .., "description": ..}."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def _as_list(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


# ------------------------------------------------------ torrent identity: size -> order

_SIZE_SUFFIXES = {
    "": 1,
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
    "tb": 1024 ** 4,
    "tib": 1024 ** 4,
}


def size_bytes(value: object) -> int:
    """The API reports torrent.size as bytes; tolerate a missing or textual value."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().replace(" ", "")
    digits = ""
    for char in text:
        if char.isdigit() or (char == "." and "." not in digits):
            digits += char
            continue
        break
    if not digits:
        return 0
    try:
        number = float(digits)
    except ValueError:
        return 0
    return int(number * _SIZE_SUFFIXES.get(text[len(digits) :].lower(), 1))


def order_key(torrent: object) -> tuple[int, str]:
    """Sort key for one release's torrents: size descending, id ascending.

    The id breaks ties rather than the infohash, because an id does not change
    when the site regenerates a torrent file, so equal-sized torrents keep their
    ranks across such an update.
    """
    torrent = torrent if isinstance(torrent, dict) else {}
    identifier = torrent.get("id")
    if identifier is None:
        identifier = torrent.get("hash")
    return (-size_bytes(torrent.get("size")), "" if identifier is None else str(identifier))


def ordered_torrents(torrents: object) -> list[dict]:
    """A release's torrents, largest first. An `order` is a position in this list."""
    if not isinstance(torrents, list):
        return []
    return sorted((torrent for torrent in torrents if isinstance(torrent, dict)), key=order_key)


def torrent_order(torrent: object, torrents: object) -> int | None:
    """1-based rank of `torrent` inside its release, or None if it cannot be placed."""
    infohash = torrent.get("hash") if isinstance(torrent, dict) else None
    for position, candidate in enumerate(ordered_torrents(torrents), start=1):
        if infohash and candidate.get("hash") == infohash:
            return position
        if candidate is torrent:
            return position
    return None


def release_key(alias: object = None, release_id: object = None) -> str | None:
    """Cache key for one release's torrent list."""
    if alias:
        return str(alias)
    if release_id is not None:
        return f"id:{release_id}"
    return None


def remember_release_torrents(alias: object, release_id: object, torrents: list) -> None:
    """Seed the rank cache from a torrent list we already fetched."""
    if CACHE_TTL <= 0:
        return
    expires = time.time() + CACHE_TTL
    keys = [key for key in (release_key(alias), release_key(None, release_id)) if key]
    with _order_lock:
        for key in keys:
            _order_cache[key] = (expires, torrents)


def release_torrents(alias: object = None, release_id: object = None, delay: bool = False) -> list[dict]:
    """Every torrent of one release - the list `order` is a rank in.

    Prefers the release object, which carries its torrents, and falls back to
    the per-release torrent endpoint when only the numeric id is known. Both the
    search and the .torrent route rank against this one list, which is what makes
    a torrent's GUID the same however it was found.
    """
    global _order_lookups

    key = release_key(alias, release_id)
    if key is None:
        return []

    now = time.time()
    with _order_lock:
        hit = _order_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    # The RSS/latest path expands many releases; keep the per-release lookups as
    # polite as the search path's are.
    if delay and _order_lookups:
        time.sleep(REQUEST_DELAY)
    _order_lookups += 1

    torrents: list[dict] = []
    if alias:
        try:
            payload = api_get(f"/anime/releases/{urllib.parse.quote(str(alias))}")
            release = payload if isinstance(payload, dict) else {}
            if not release.get("torrents") and isinstance(release.get("data"), dict):
                release = release["data"]
            torrents = [item for item in _as_list(release.get("torrents")) if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001 - fall back to the id lookup
            log(f"  torrents of {alias!r}: {type(exc).__name__}: {exc}")

    if not torrents and release_id is not None:
        try:
            payload = api_get(f"/anime/torrents/release/{release_id}")
            torrents = [item for item in _as_list(payload) if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001 - ranking is best effort
            log(f"  torrents of release {release_id}: {type(exc).__name__}: {exc}")

    with _order_lock:
        if CACHE_TTL > 0:
            _order_cache[key] = (now + CACHE_TTL, torrents)
        if len(_order_cache) > 512:
            for stale in [k for k, v in _order_cache.items() if v[0] <= now]:
                _order_cache.pop(stale, None)

    return torrents


def download_url(alias: object, order: object, infohash: object = None) -> str:
    """The URL a release is grabbed from - and therefore that release's GUID.

    The stable divert route whenever the size rank is known. The API's
    hash-addressed file URL is only a fallback, so a row stays grabbable when the
    extra lookup behind `order` failed; that GUID is hash-based again, which is
    exactly what this arrangement exists to avoid.
    """
    if alias and isinstance(order, int) and order > 0:
        quoted = urllib.parse.quote(str(alias), safe="")
        return f"{DIVERT_ORIGIN}{DIVERT_PREFIX}/torrent/{quoted}/{order}.torrent"
    if infohash:
        return f"{SITE}/api/v1/anime/torrents/{infohash}/file"
    return ""


def project_release(release: object) -> dict:
    """Flatten a release object so the YAML only needs single-level selectors."""
    release = release if isinstance(release, dict) else {}
    name = release.get("name") if isinstance(release.get("name"), dict) else {}
    season = release.get("season") if isinstance(release.get("season"), dict) else {}
    poster = release.get("poster") if isinstance(release.get("poster"), dict) else {}

    return {
        "id": release.get("id"),
        "alias": release.get("alias"),
        "type": _scalar(release.get("type")),
        "type_description": (release.get("type") or {}).get("description")
        if isinstance(release.get("type"), dict)
        else None,
        "year": release.get("year"),
        "name_main": name.get("main"),
        "name_english": name.get("english"),
        "name_alternative": name.get("alternative"),
        "season": _scalar(release.get("season")),
        "season_description": season.get("description"),
        "episodes_total": release.get("episodes_total"),
        "poster": poster.get("src"),
        "description": release.get("description"),
    }


def project_torrent(torrent: object) -> dict:
    """Flatten a torrent object."""
    torrent = torrent if isinstance(torrent, dict) else {}
    return {
        "id": torrent.get("id"),
        "hash": torrent.get("hash"),
        "label": torrent.get("label"),
        "filename": torrent.get("filename"),
        "size": torrent.get("size"),
        "seeders": torrent.get("seeders"),
        "leechers": torrent.get("leechers"),
        "grabs": torrent.get("completed_times"),
        "created_at": torrent.get("created_at"),
        "updated_at": torrent.get("updated_at"),
        "episodes": torrent.get("description"),
        "quality": _scalar(torrent.get("quality")),
        "codec": _scalar(torrent.get("codec")),
        "color": _scalar(torrent.get("color")),
        "source": _scalar(torrent.get("type")),
        "magnet": torrent.get("magnet"),
    }


def make_row(release: object, torrent: object, order: int | None = None) -> dict:
    """One flattened row, plus the identity the release's GUID is built from."""
    projected_release = project_release(release)
    projected_torrent = project_torrent(torrent)
    return {
        "release": projected_release,
        "torrent": projected_torrent,
        "order": order,
        "download_url": download_url(
            projected_release.get("alias"), order, projected_torrent.get("hash")
        ),
    }


def latest_torrents(limit: int) -> list:
    """Newest torrents. /anime/torrents rejects limit > 50, so clamp and back off."""
    for candidate in (min(limit, API_MAX_LIMIT), 25, 10):
        candidate = max(1, candidate)
        try:
            payload = api_get("/anime/torrents", {"limit": candidate})
            return _as_list(payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 422 and candidate > 10:
                log(f"  limit={candidate} rejected by the API, retrying smaller")
                continue
            raise
    return []


def release_matches(release: dict, alias: object, release_id: object) -> bool:
    """Does a release object describe the release identified by this alias/id?"""
    if release_id is not None and str(release.get("id")) == str(release_id):
        return True
    return bool(alias) and release.get("alias") == alias


def rank_releases(identities: list[tuple]) -> dict[str, list[dict]]:
    """The torrent list of several releases at once -- what the feed path needs.

    /anime/torrents is a flat list that carries one torrent of each release, so
    every release it mentions has to be looked up before its ranks are known.
    Prowlarr's indexer test and its RSS sync both take that path, so the releases
    are asked for in a single batched request first, and only whatever the batch
    did not cover is fetched one by one. Ranking all of them is not optional: it
    is what gives a torrent the same GUID here as in a keyword search.
    """
    ranking: dict[str, list[dict]] = {}
    wanted: list[tuple[str, object, object]] = []

    now = time.time()
    with _order_lock:
        for alias, release_id in identities:
            key = release_key(alias, release_id)
            if key is None or key in ranking:
                continue
            hit = _order_cache.get(key)
            if hit and hit[0] > now:
                ranking[key] = hit[1]
            else:
                ranking[key] = []
                wanted.append((key, alias, release_id))

    if not wanted:
        return ranking

    ids = [release_id for _, _, release_id in wanted if release_id is not None]
    if ids:
        try:
            payload = api_get(
                "/anime/releases/list",
                {"ids": ",".join(str(release_id) for release_id in ids), "limit": len(ids)},
            )
            for release in _as_list(payload):
                if not isinstance(release, dict):
                    continue
                torrents = [item for item in _as_list(release.get("torrents")) if isinstance(item, dict)]
                if not torrents:
                    continue
                for key, alias, release_id in wanted:
                    if release_matches(release, alias, release_id):
                        ranking[key] = torrents
        except Exception as exc:  # noqa: BLE001 - the batch is only an optimisation
            log(f"  batched release lookup unavailable ({type(exc).__name__}: {exc})")

    unresolved = [entry for entry in wanted if not ranking[entry[0]]]
    if unresolved:
        log(f"  ranking {len(unresolved)} release(s) one at a time")
    for key, alias, release_id in unresolved:
        ranking[key] = release_torrents(alias, release_id, delay=True)

    if CACHE_TTL > 0:
        expires = time.time() + CACHE_TTL
        with _order_lock:
            for key, torrents in ranking.items():
                if torrents:
                    _order_cache[key] = (expires, torrents)

    return ranking


def latest_rows(items: list[dict]) -> list[dict]:
    """Rows for the newest torrents, ranked inside their own release.

    Without the ranking, the same torrent would get a different GUID here than
    from a keyword search, and downstream apps would see two releases.
    """
    entries = []
    for item in items:
        release = item.get("release") if isinstance(item.get("release"), dict) else {}
        entries.append((item, release, release.get("alias"), release.get("id")))

    ranking = rank_releases([(alias, release_id) for _, _, alias, release_id in entries])

    rows: list[dict] = []
    fallbacks = 0
    for item, release, alias, release_id in entries:
        torrents = ranking.get(release_key(alias, release_id) or "") or [item]
        order = torrent_order(item, torrents)
        if order is None:
            fallbacks += 1
        rows.append(make_row(release, item, order))

    if fallbacks:
        log(f"  {fallbacks} row(s) fell back to the API download URL (no size rank)")

    return rows


def search(keywords: str, limit: int) -> list[dict]:
    """Return flattened rows for a keyword search, or the newest torrents if empty."""
    keywords = (keywords or "").strip()
    rows: list[dict] = []

    if not keywords:
        items = [item for item in latest_torrents(limit) if isinstance(item, dict)]
        rows = latest_rows(items)
        log(f"rss/latest: {len(rows)} rows (limit={min(limit, API_MAX_LIMIT)})")
    else:
        payload = api_get("/app/search/releases", {"query": keywords})
        releases = _as_list(payload)
        log(f"search {keywords!r}: {len(releases)} releases matched")

        seen_releases: set[object] = set()
        for release in releases[:MAX_RELEASES]:
            if not isinstance(release, dict):
                continue
            release_id = release.get("id")
            if release_id is None or release_id in seen_releases:
                continue
            seen_releases.add(release_id)

            if rows:
                time.sleep(REQUEST_DELAY)

            try:
                torrent_payload = api_get(f"/anime/torrents/release/{release_id}")
            except Exception as exc:  # noqa: BLE001 - skip a bad release, keep going
                log(f"  release {release_id}: {type(exc).__name__}: {exc}")
                continue

            torrents = [item for item in _as_list(torrent_payload) if isinstance(item, dict)]
            # This response is the release's whole torrent list, so it is both the
            # list `order` is a rank in and what the .torrent route resolves against
            # later. Remember it, so that route does not have to ask again.
            remember_release_torrents(release.get("alias"), release_id, torrents)

            for torrent in torrents:
                rows.append(
                    make_row(
                        torrent.get("release") or release,
                        torrent,
                        torrent_order(torrent, torrents),
                    )
                )

        log(f"search {keywords!r}: {len(rows)} torrents from {len(seen_releases)} releases")

    # De-duplicate by infohash, preserving order.
    unique: list[dict] = []
    seen_hashes: set[object] = set()
    for row in rows:
        infohash = row["torrent"].get("hash")
        if infohash and infohash in seen_hashes:
            continue
        if infohash:
            seen_hashes.add(infohash)
        unique.append(row)

    return unique[:MAX_ROWS]


def cached_search(keywords: str, limit: int) -> tuple[list[dict], bool]:
    key = (keywords.strip().lower(), limit)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1], True

    rows = search(keywords, limit)

    with _cache_lock:
        if CACHE_TTL > 0:
            _cache[key] = (now + CACHE_TTL, rows)
        if len(_cache) > 256:
            for stale in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(stale, None)

    return rows, False


def parse_search_query(query: dict) -> tuple[str, int]:
    """Pull q/limit out of parsed query-string values, clamped to what the API allows."""
    keywords = (query.get("q") or query.get("query") or [""])[0]
    try:
        limit = int((query.get("limit") or ["100"])[0])
    except (TypeError, ValueError):
        limit = 100
    return keywords, max(1, min(limit, MAX_ROWS))


def search_payload(keywords: str, limit: int) -> dict:
    """The body both the direct endpoint and the proxy divert route return."""
    rows, from_cache = cached_search(keywords, limit)
    return {
        "rows": rows,
        "meta": {
            "query": keywords,
            "count": len(rows),
            "cached": from_cache,
            "api_base": API_BASE,
        },
    }


class ProxyHandler(BaseHTTPRequestHandler):
    """A forward HTTP proxy that relays everything to the origin, except the divert route.

    Prowlarr reaches this through its "Http" indexer proxy, so requests arrive in
    absolute form (``GET http://host/path HTTP/1.1``) and the destination is known
    without resolving it here. Anything whose path starts with ``DIVERT_PREFIX``
    is answered locally: the search route with AniLibria search results, and the
    ``.torrent`` route with the torrent file that release's GUID points at.
    Everything else - release pages and Prowlarr's own proxy health check - is
    passed straight through to the origin, so the site keeps working normally and
    only these two hops change.
    """

    protocol_version = "HTTP/1.1"
    server_version = "AniLibriaProxy/1.0"

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- tunneling
    def do_CONNECT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Blind TCP tunnel. This is how TLS traffic, and Prowlarr's proxy health
        check against prowlarr.servarr.com, get through untouched."""
        host, _, port_text = self.path.rpartition(":")
        try:
            port = int(port_text) if port_text else 443
        except ValueError:
            host, port = self.path, 443

        try:
            upstream = socket.create_connection((host, port), timeout=HTTP_TIMEOUT)
        except OSError as exc:
            log(f"CONNECT {self.path} failed: {exc}")
            self.send_error(502, f"cannot connect to {self.path}")
            return

        log(f"tunnel {self.path}")
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            self._pump(self.connection, upstream)
        finally:
            upstream.close()
            self.close_connection = True

    @staticmethod
    def _pump(client: socket.socket, upstream: socket.socket) -> None:
        while True:
            try:
                readable, _, errored = select.select([client, upstream], [], [client, upstream], 300)
            except (OSError, ValueError):
                return
            if errored:
                return
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (upstream if source is client else client).sendall(data)
                except OSError:
                    return

    # ---------------------------------------------------------------- requests
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        target = self.path
        origin_form = target.startswith("/")
        if origin_form:
            # A plain request straight at the proxy (health check, curl, browser).
            # Proxied traffic always arrives in absolute form, so these cannot
            # collide with a real destination's paths.
            if self._serve_local(method, target):
                return
            host = self.headers.get("Host")
            if not host:
                self.send_error(400, "origin-form request without a Host header")
                return
            target = f"http://{host}{target}"

        try:
            parts = urllib.parse.urlsplit(target)
        except ValueError:
            self.send_error(400, f"unparseable request target: {target!r}")
            return

        path = parts.path or "/"

        if path.startswith(DIVERT_PREFIX):
            torrent_route = TORRENT_ROUTE.match(path)
            if torrent_route:
                if method in ("GET", "HEAD"):
                    self._serve_torrent(method, torrent_route.group("alias"), int(torrent_route.group("order")))
                else:
                    self.send_error(405, "the .torrent route only answers GET and HEAD")
                return

            keywords, limit = parse_search_query(urllib.parse.parse_qs(parts.query))
            log(f"DIVERT {method} {parts.netloc}{path} q={keywords!r} limit={limit}")
            try:
                payload = search_payload(keywords, limit)
            except Exception as exc:  # noqa: BLE001 - surface upstream failure to Prowlarr
                log(f"DIVERT failed: {type(exc).__name__}: {exc}")
                self._send_json(
                    502,
                    {"error": "upstream request failed", "detail": f"{type(exc).__name__}: {exc}"},
                )
                return
            self._send_json(200, payload)
            return

        self._forward(method, parts)

    def _serve_local(self, method: str, target: str) -> bool:
        """Answer /healthz and / directly. Returns True if the request was handled."""
        path = urllib.parse.urlsplit(target).path
        if method not in ("GET", "HEAD"):
            return False

        if path == "/healthz":
            self._send_json(200, {"ok": True, "api_base": API_BASE, "divert_prefix": DIVERT_PREFIX})
            return True

        if path == "/":
            self._send_json(
                200,
                {
                    "service": "anilibria-proxy",
                    "how_it_works": (
                        "set this address as an Http indexer proxy in Prowlarr; requests "
                        f"under {DIVERT_PREFIX} are answered here, everything else is relayed"
                    ),
                    "local_endpoints": {
                        "/healthz": "liveness probe",
                        "/": "this page",
                        f"{DIVERT_PREFIX}/search": "flattened rows for the definition to parse",
                        f"{DIVERT_PREFIX}/torrent/{{alias}}/{{order}}.torrent": (
                            "one .torrent file; this URL is also the release's GUID"
                        ),
                    },
                    "settings": {
                        "divert_prefix": DIVERT_PREFIX,
                        "max_releases": MAX_RELEASES,
                        "cache_ttl": CACHE_TTL,
                    },
                    "api_base": API_BASE,
                    "_links": {
                        "search": f"{SITE}/api/v1/app/search/releases?query={{q}}",
                        "torrent_by_rank": f"{DIVERT_ORIGIN}{DIVERT_PREFIX}/torrent/{{alias}}/{{order}}.torrent",
                        "torrent_file": f"{SITE}/api/v1/anime/torrents/{{hash}}/file",
                        "divert_route": f"{SITE}{DIVERT_PREFIX}/search?q={{q}}&limit=50",
                    },
                },
            )
            return True

        return False

    def _serve_torrent(self, method: str, alias: str, order: int) -> None:
        """Answer the stable .torrent route: release + size rank -> the current file.

        This is the other half of the GUID arrangement described in the module
        docstring: the URL names the release and the rank, and the infohash - which
        is what actually changes when a torrent file is regenerated - is looked up
        here, at grab time, rather than baked into the GUID.
        """
        alias = urllib.parse.unquote(alias)

        ranking = ordered_torrents(release_torrents(alias))
        if not ranking:
            # An empty list means either "no such release" or "the API would not
            # answer", and the two deserve different statuses: Prowlarr treats a
            # 4xx grab result differently from an outage. release_torrents() is
            # deliberately non-raising, so ask once more, only on this path.
            try:
                api_get(f"/anime/releases/{urllib.parse.quote(alias)}")
                status, detail = 404, f"{alias!r} has no torrents"
            except urllib.error.HTTPError as exc:
                status = 404 if exc.code == 404 else 502
                detail = f"release lookup answered HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001 - upstream is unreachable
                status, detail = 502, f"{type(exc).__name__}: {exc}"
            log(f"DIVERT torrent {alias}/{order}: {detail}")
            self._send_json(status, {"error": "cannot rank torrents", "detail": detail})
            return

        if not 1 <= order <= len(ranking):
            log(f"DIVERT torrent {alias}/{order}: {alias!r} has {len(ranking)} torrent(s)")
            self._send_json(
                404,
                {"error": "no such torrent", "detail": f"{alias!r} has {len(ranking)} torrent(s)"},
            )
            return

        torrent = ranking[order - 1]
        infohash = torrent.get("hash")
        if not infohash:
            log(f"DIVERT torrent {alias}/{order}: the torrent carries no hash")
            self._send_json(
                502,
                {"error": "torrent has no hash", "detail": f"{alias!r} order {order}"},
            )
            return

        log(f"DIVERT torrent {alias}/{order} -> {infohash}")
        try:
            request = urllib.request.Request(
                f"{API_BASE}/anime/torrents/{infohash}/file",
                headers={"User-Agent": USER_AGENT, "Accept": "application/x-bittorrent"},
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                blob = response.read()
                content_type = response.headers.get("Content-Type") or "application/x-bittorrent"
                disposition = response.headers.get("Content-Disposition")
        except Exception as exc:  # noqa: BLE001 - surface the failure to Prowlarr
            log(f"DIVERT torrent {alias}/{order} failed: {type(exc).__name__}: {exc}")
            self._send_json(
                502,
                {"error": "upstream request failed", "detail": f"{type(exc).__name__}: {exc}"},
            )
            return

        filename = str(torrent.get("filename") or f"{infohash}.torrent").replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", disposition or f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(blob)

    def _forward(self, method: str, parts: urllib.parse.SplitResult) -> None:
        length = self.headers.get("Content-Length")
        body = None
        if length and length.isdigit() and int(length) > 0:
            body = self.rfile.read(int(length))

        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in ("proxy-connection", "connection", "host", "content-length")
        }
        headers["Host"] = parts.netloc
        if body is not None:
            headers["Content-Length"] = str(len(body))

        log(f"PASS {method} {parts.scheme}://{parts.netloc}{path}")

        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        try:
            connection = connection_class(parts.hostname, parts.port, timeout=HTTP_TIMEOUT)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                status, reason = response.status, response.reason
                response_headers = response.getheaders()
                data = response.read()
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - anything here means the origin is unreachable
            log(f"PASS failed for {parts.netloc}: {type(exc).__name__}: {exc}")
            self.send_error(502, f"cannot reach {parts.netloc}")
            return

        self.send_response(status, reason)
        for name, value in response_headers:
            if name.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        log(f"access {self.address_string()} {fmt % args}")


def main() -> None:
    if not PORT:
        raise SystemExit("PROXY_PORT is empty; set a port for the listener to use")

    server = ThreadingHTTPServer((BIND, PORT), ProxyHandler)
    server.daemon_threads = True
    log(f"anilibria-proxy listening on {BIND}:{PORT}")
    log(f"  diverting {DIVERT_PREFIX}/search and {DIVERT_PREFIX}/torrent/*, relaying the rest")
    log(f"  api={API_BASE}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
