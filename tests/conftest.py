import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from mcp import Client

from iran_dictionary_mcp.abadis import http as ab_http
from iran_dictionary_mcp.server import mcp
from iran_dictionary_mcp.vajehyab import http as vy_http

HTTPS = (ab_http, vy_http)
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fresh_http_clients(monkeypatch):
    """The shared httpx clients are bound to one event loop; each test gets a new loop."""
    for h in HTTPS:
        monkeypatch.setattr(h, "RETRY_DELAY", 0)
        monkeypatch.setattr(h, "JITTER", (0, 0))
        h.set_transport(None)
    yield
    for h in HTTPS:
        h.set_transport(None)


@pytest.fixture
def web():
    """Route table for both fake sites: web["/path"] = fixture file name, or a callable(request) -> Response.

    Keys match the decoded URL path only (the sites use different paths). Unknown paths return 404. Every request
    is appended to web.calls.
    """

    class Routes(dict):
        calls: list[httpx.Request]

    routes = Routes()
    routes.calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.calls.append(request)
        target = routes.get(unquote(request.url.path))
        if target is None:
            return httpx.Response(404, text="not found")
        if callable(target):
            return target(request)
        return httpx.Response(200, text=fixture(target))

    for h in HTTPS:
        h.set_transport(httpx.MockTransport(handler))
    return routes


@pytest.fixture
async def client():
    async with Client(mcp, raise_exceptions=True) as c:
        call_tool = c.call_tool

        async def parsed(*args, **kwargs):
            result = await call_tool(*args, **kwargs)
            if not result.is_error:
                result.structured_content = json.loads(result.content[0].text)
            return result

        c.call_tool = parsed
        yield c
