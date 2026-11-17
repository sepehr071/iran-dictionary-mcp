import httpx
import pytest

from iran_dictionary_mcp.abadis import http as ab_http

from .conftest import fixture

pytestmark = pytest.mark.anyio

TOOLS = {
    "dic_lookup",
    "dic_synonyms",
    "dic_translate",
    "dic_suggest",
    "dic_abadis_names",
    "dic_vajehyab_search",
    "dic_vajehyab_entry",
}


def engine(web, **by_query):
    """Serve the engine's /search: the fixture is chosen by "<scope>:<dictionaries>", or by the scope alone."""

    def answer(request):
        q = request.url.params
        name = by_query.get(f"{q['s']}:{q.get('d', '')}") or by_query[q["s"]]
        return httpx.Response(200, text=fixture(name))

    web["/search"] = answer


async def test_tool_list(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.annotations.read_only_hint is True, t.name
        assert t.description.splitlines()[0].endswith("."), t.name
    for n in ("dic_lookup", "dic_synonyms", "dic_translate", "dic_suggest"):
        assert "source" in tools[n].input_schema["properties"]


async def test_lookup_both_sites(client, web):
    web["/fatofa/سلام/"] = "word_salam.html"
    engine(web, exact="search_salam.json")
    r = (await client.call_tool("dic_lookup", {"word": "سلام"})).structured_content
    a, v = r["abadis"], r["vajehyab"]
    assert a["found"] and "درود" in a["synonyms"] and "hello" in a["english"] and a["pronunciation"] == "/salAm/"
    assert "لغت نامه دهخدا" in a["dictionaries"] and all(len(t) <= 401 for t in a["dictionaries"].values())
    assert "پیشنهاد کاربران" not in a["dictionaries"]
    assert v["found"] and "dehkhoda" in {h["dictionary"] for h in v["hits"]} and v["hits"][0]["slug"]


async def test_lookup_unknown_word_and_one_site_error(client, web):
    web["/fatofa/زززز/"] = lambda r: httpx.Response(410, text="gone")
    r = (await client.call_tool("dic_lookup", {"word": "زززز", "source": "abadis"})).structured_content
    assert r == {"abadis": {"found": False}}
    web["/fatofa/سلام/"] = "word_salam.html"  # the engine answers 404: Vajehyab's block holds the error
    r = (await client.call_tool("dic_lookup", {"word": "سلام"})).structured_content
    assert r["abadis"]["found"] and "error" in r["vajehyab"]


async def test_synonyms(client, web):
    web["/fatofa/دوست/"] = "word_salam.html"  # any Abadis page: checks the header lists
    engine(
        web,
        **{"exact:motaradef": "motaradef_dust.json", "antonym": "antonym_dust.json", "synonym": "synonym_dust.json"},
    )
    r = (await client.call_tool("dic_synonyms", {"word": "دوست"})).structured_content
    assert r["abadis"]["synonyms"] and r["vajehyab"]["synonyms"].startswith("آشنا")
    assert "دشمن" in r["vajehyab"]["antonyms"]
    assert "برادر" in r["vajehyab"]["also_listed_by"] and "آشنا" not in r["vajehyab"]["also_listed_by"]


async def test_translate_en_fa(client, web):
    web["/fatofa/hello/"] = lambda r: httpx.Response(301, headers={"location": "/entofa/hello/"})
    web["/entofa/hello/"] = "word_hello.html"
    engine(web, exact="en2fa_hello.json")
    r = (await client.call_tool("dic_translate", {"word": "hello", "examples": 2})).structured_content
    assert "هالو" in r["abadis"]["meaning"] and len(r["abadis"]["examples"]) == 2
    assert r["vajehyab"]["translations"][0]["dictionary"] == "en2fa"
    assert any(c.url.params.get("d") == "en2fa" for c in web.calls)


async def test_translate_arabic_skips_abadis(client, web):
    engine(web, exact="en2fa_hello.json")
    r = (await client.call_tool("dic_translate", {"word": "كتاب", "direction": "ar-fa"})).structured_content
    assert set(r) == {"vajehyab"}
    bad = await client.call_tool("dic_translate", {"word": "كتاب", "direction": "ar-fa", "source": "abadis"})
    assert bad.is_error and "vajehyab" in bad.content[0].text


async def test_suggest(client, web):
    web["/ajaxcmd/getaclist/"] = "abadis_suggest.html"
    web["/suggestion"] = "suggestion.json"
    r = (await client.call_tool("dic_suggest", {"prefix": "سلا", "limit": 3})).structured_content
    assert r["abadis"][0] == "سلام" and len(r["abadis"]) == 3
    assert r["vajehyab"][0] == "سلام برشما باد"  # the engine's "سلا م" spacing is repaired


async def test_names(client, web):
    web["/name/girl/farsi/flower/"] = "names.html"
    args = {"gender": "girl", "origin": "farsi", "category": "flower", "letter": "پ", "limit": 2}
    r = (await client.call_tool("dic_abadis_names", args)).structured_content
    assert r["count"] == 4 and len(r["names"]) == 2
    n = r["names"][0]
    assert n["name"] and n["pronunciation"].startswith("/") and n["gender"] == "دختر" and n["origin"] == "فارسی"
    assert "ch=%D9%BE" in str(web.calls[-1].url)


async def test_names_rejects_bad_slug(client, web):
    r = await client.call_tool("dic_abadis_names", {"origin": "../x"})
    assert r.is_error and not web.calls


async def test_vajehyab_search_and_entry(client, web):
    engine(web, antonym="antonym_dust.json")
    r = (await client.call_tool("dic_vajehyab_search", {"query": "دوست", "scope": "antonym"})).structured_content
    assert r["total"] > 0 and r["hits"][0]["dictionary"] == "motaradef"
    web["/moein/دوست"] = "entry_moein_dust.html"
    e = (await client.call_tool("dic_vajehyab_entry", {"dictionary": "moein", "slug": "دوست"})).structured_content
    assert e["dictionary"] == "moein" and "یار" in e["text"] and "<" not in e["text"]
    bad = await client.call_tool("dic_vajehyab_search", {"query": "دوست", "scope": "nope"})
    assert bad.is_error and "scope must be" in bad.content[0].text


def test_proxy_variable_is_ours_only(monkeypatch):
    monkeypatch.setenv("IRAN_DICTIONARY_MCP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://other:1")
    ab_http.set_transport(None)
    client, _ = ab_http._get_client()  # builds with the proxy, no network call
    assert client._trust_env is False
