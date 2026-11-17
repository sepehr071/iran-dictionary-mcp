"""Shared async HTTP client for vajehyab.com: the engine.vajehyab.com JSON API and the site's server-rendered pages.

Every tool goes through `fetch`, which caps concurrency, caches pages briefly and turns HTTP failures
into `ToolError` messages the model can act on.
"""

from __future__ import annotations

import asyncio
import os
import random
import time

import httpx
from curl_cffi.const import CurlOpt
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

SITE = "https://vajehyab.com"
ENGINE = "https://engine.vajehyab.com"
HOSTS = {"vajehyab.com", "engine.vajehyab.com"}  # host allowlist; redirects are followed by hand and checked against it

HEADERS = {"Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"}  # User-Agent, sec-ch-ua and TLS come from the Chrome profile
PAGE_HEADERS = {
    "Referer": SITE + "/",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}
# The site's own script calls the engine cross-origin (checked in Chrome 2026-11-16).
XHR_HEADERS = {
    "Accept": "*/*",
    "Origin": SITE,
    "Referer": SITE + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

MAX_CONCURRENCY = 2
JITTER = (0.2, 0.6)  # seconds before each uncached call
MAX_RETRIES = 2
MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
CACHE_TTL = 120  # seconds
CACHE_MAX = 64
RETRY_DELAY = 0.5  # seconds, doubled per retry
MAX_BODY = 5_000_000  # bytes; larger pages are refused
MAX_REDIRECTS = 3

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_limit: asyncio.Semaphore | None = None
_cache: dict[tuple, tuple[float, tuple[str, str]]] = {}


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client."""
    global _transport, _client, _limit
    _transport, _client, _limit = transport, None, None
    _cache.clear()


def _get_client() -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
    global _client, _limit
    if _client is None:
        # System proxy settings are ignored; the proxy only comes from IRAN_DICTIONARY_MCP_PROXY. curl_cffi presents
        # Chrome's TLS/HTTP2 fingerprint and default headers.
        proxy = os.environ.get("IRAN_DICTIONARY_MCP_PROXY", "").strip() or None
        _client = httpx.AsyncClient(
            transport=_transport
            or AsyncCurlTransport(
                impersonate="chrome",
                default_headers=True,
                curl_options={CurlOpt.NOPROXY: "*"},  # httpx-curl-cffi drops its own NOPROXY unless this is non-empty
                proxy=proxy,
                max_connections=2 * MAX_CONCURRENCY,
            ),
            headers=HEADERS,
            timeout=httpx.Timeout(20, connect=5),
            limits=httpx.Limits(max_connections=2 * MAX_CONCURRENCY),
            follow_redirects=False,
            trust_env=False,
        )
        _limit = asyncio.Semaphore(MAX_CONCURRENCY)
    assert _limit is not None
    return _client, _limit


async def fetch(path: str, params: list[tuple[str, str]] | None = None, *, xhr: bool = False) -> tuple[str, str]:
    """GET a vajehyab.com page or engine call; returns (final URL, text). `xhr=True` is for the JSON engine.

    `path` is a vajehyab.com path ("/dehkhoda/سلام") or a full vajehyab.com / engine.vajehyab.com URL. Redirects are
    followed by hand. Retried up to MAX_RETRIES times on timeout, network error, 5xx or 429, with
    jittered exponential backoff or after Retry-After when sent. 403 is never retried.
    """
    url = path if path.startswith("http") else SITE + path
    query = list(params or [])
    key = (url, tuple(query), xhr)
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    for _ in range(MAX_REDIRECTS + 1):
        host = httpx.URL(url).host
        if host not in HOSTS:
            raise ApiError(f"Refusing to call unexpected host {host}.")
        r, content = await _get(url, query, host, xhr)
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 307, 308) and loc:
            url, query = str(httpx.URL(url).join(loc)), []
            continue
        break
    else:
        raise ApiError("vajehyab.com redirected too many times.")
    if r.status_code >= 400:
        raise ApiError(_status_message(r.status_code, host), r.status_code)
    out = (str(r.url), content.decode("utf-8", "replace"))
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), out)
    return out


async def _get(url: str, query: list[tuple[str, str]], host: str, xhr: bool) -> tuple[httpx.Response, bytes]:
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        wait = RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
        try:
            r, content = await _request(url, query, host, xhr)
        except ApiError as e:
            if last or e.status is not None:
                raise
            await asyncio.sleep(min(wait, MAX_RETRY_WAIT))
            continue
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            await asyncio.sleep(min(_retry_after(r) or wait, MAX_RETRY_WAIT))
            continue
        return r, content
    raise AssertionError("unreachable")


async def _request(url: str, query: list[tuple[str, str]], host: str, xhr: bool) -> tuple[httpx.Response, bytes]:
    client, limit = _get_client()
    try:
        async with limit:
            await asyncio.sleep(random.uniform(*JITTER))
            async with client.stream(
                "GET", url, params=query or None, headers=XHR_HEADERS if xhr else PAGE_HEADERS
            ) as r:
                content = await _read_capped(r, host)
    except httpx.TimeoutException as e:
        raise ApiError(f"{host} did not answer in time. Try again in a moment.") from e
    except httpx.RequestError as e:
        raise ApiError(
            f"Could not reach {host} ({type(e).__name__}). Check the internet connection "
            "(or the IRAN_DICTIONARY_MCP_PROXY setting if you use one)."
        ) from e
    return r, content


async def _read_capped(r: httpx.Response, host: str) -> bytes:
    """Read the body, refusing anything over MAX_BODY so one huge page cannot flood memory or context."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(f"{host} sent a response over {MAX_BODY // 1_000_000} MB; refused.", r.status_code)
    return bytes(buf)


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 429/503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code in (429, 503) and v.replace(".", "", 1).isdigit() else None


def _status_message(code: int, host: str) -> str:
    if code == 403:
        return f"{host} blocked the request (HTTP 403). Try another network or set IRAN_DICTIONARY_MCP_PROXY."
    if code == 404:
        return f"{host} found nothing at that address (HTTP 404)."
    if code == 429:
        return f"{host} is rate limiting requests (HTTP 429). Wait a minute before retrying."
    if code >= 500:
        return f"{host} had a server error (HTTP {code}). Try again later."
    return f"{host} rejected the request (HTTP {code})."
