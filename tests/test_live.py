import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def call(client, name, **args):
    return (await client.call_tool(name, args)).structured_content


async def test_live_lookup(client):
    r = await call(client, "dic_lookup", word="سلام")
    assert r["abadis"]["found"] and "درود" in r["abadis"]["synonyms"]
    assert r["vajehyab"]["found"] and any(h["dictionary"] == "dehkhoda" for h in r["vajehyab"]["hits"])


async def test_live_lookup_full(client):
    def dehkhoda(r):
        return next(h for h in r["vajehyab"]["hits"] if h["dictionary"] == "dehkhoda")["text"]

    short = await call(client, "dic_lookup", word="سلام", source="vajehyab")
    full = await call(client, "dic_lookup", word="سلام", source="vajehyab", full=True)
    assert len(dehkhoda(full)) > len(dehkhoda(short))


async def test_live_synonyms(client):
    r = await call(client, "dic_synonyms", word="دوست")
    assert "رفیق" in r["abadis"]["synonyms"] and "دشمن" in r["vajehyab"]["antonyms"]


async def test_live_translate(client):
    r = await call(client, "dic_translate", word="hello")
    assert "سلام" in r["abadis"]["meaning"] and "سلام" in r["vajehyab"]["translations"][0]["text"]
    r = await call(client, "dic_translate", word="سلام", direction="fa-en")
    assert "hello" in r["abadis"]["english"] and "hello" in r["vajehyab"]["translations"][0]["text"]
    r = await call(client, "dic_translate", word="سلام", direction="ar-fa")
    assert r["vajehyab"]["found"]


async def test_live_suggest(client):
    r = await call(client, "dic_suggest", prefix="سلا")
    assert "سلام" in r["abadis"] and "سلام" in r["vajehyab"]
    assert "help" in (await call(client, "dic_suggest", prefix="hel"))["abadis"]


async def test_live_names(client):
    r = await call(client, "dic_abadis_names", gender="girl", category="flower", limit=5)
    assert len(r["names"]) == 5 and all(n["gender"] == "دختر" for n in r["names"])
    r = await call(client, "dic_abadis_names", query="پریا")
    assert any(n["name"] == "پریا" for n in r["names"])


async def test_live_vajehyab_search_and_entry(client):
    r = await call(client, "dic_vajehyab_search", query="کتاب", scope="suffix", limit=5)
    assert r["total"] > 5 and len(r["hits"]) == 5
    r = await call(client, "dic_vajehyab_entry", dictionary="dehkhoda", slug="سلام", max_chars=1500)
    assert r["title"] == "سلام" and len(r["text"]) > 500
