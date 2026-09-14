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

**Every other request is relayed to its real destination** - release pages,
``.torrent`` downloads and Prowlarr's own proxy health check (a TLS tunnel to
prowlarr.servarr.com) all pass straight through, so the site keeps working
normally and only the search hop changes.

That is what makes the proxy's location configurable from Prowlarr itself: the
definition uses a fixed search path on the real site, and where this service
actually runs is just the indexer proxy's host and port.

Everything else (categories, titles, download URLs, date formatting) is left to
the YAML definition, so the indexing logic stays in the Prowlarr config.

Local endpoints on the same listener, for humans and container health checks:

    GET /healthz    liveness probe
    GET /           short description of the endpoints
"""

from __future__ import annotations

import http.client
import json
import os
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


def make_row(release: object, torrent: object) -> dict:
    return {"release": project_release(release), "torrent": project_torrent(torrent)}


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


def search(keywords: str, limit: int) -> list[dict]:
    """Return flattened rows for a keyword search, or the newest torrents if empty."""
    keywords = (keywords or "").strip()
    rows: list[dict] = []

    if not keywords:
        for item in latest_torrents(limit):
            if isinstance(item, dict):
                rows.append(make_row(item.get("release"), item))
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

            for torrent in _as_list(torrent_payload):
                if isinstance(torrent, dict):
                    rows.append(make_row(torrent.get("release") or release, torrent))

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
    is answered locally with AniLibria search results. Everything else - release
    pages, ``.torrent`` downloads, and Prowlarr's own proxy health check - is
    passed straight through to the origin, so the site keeps working normally and
    only the search hop changes.
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
                    "local_endpoints": {"/healthz": "liveness probe", "/": "this page"},
                    "settings": {
                        "divert_prefix": DIVERT_PREFIX,
                        "max_releases": MAX_RELEASES,
                        "cache_ttl": CACHE_TTL,
                    },
                    "api_base": API_BASE,
                    "_links": {
                        "search": f"{SITE}/api/v1/app/search/releases?query={{q}}",
                        "torrent_file": f"{SITE}/api/v1/anime/torrents/{{hash}}/file",
                        "divert_route": f"{SITE}{DIVERT_PREFIX}/search?q={{q}}&limit=50",
                    },
                },
            )
            return True

        return False

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
    log(f"  diverting {DIVERT_PREFIX}/* to AniLibria search, relaying everything else")
    log(f"  api={API_BASE}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
