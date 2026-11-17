"""Vajehyab (vajehyab.com) search engine, full entries and autocomplete. Plain functions plus two site tools."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any
from urllib.parse import quote

from bs4 import BeautifulSoup
from pydantic import Field

from ..registry import tool
from . import http

SCOPES = ("exact", "synonym", "antonym", "similar", "homophone", "prefix", "suffix")
MAIN = "dehkhoda,moein,amid,motaradef"
Dictionaries = Annotated[
    str, Field(description="Comma-separated dictionary slugs.", max_length=200, pattern=r"^[a-z0-9,]*$")
]


def clip(text: str, cap: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def plain(html: str) -> str:
    """Entry text is HTML with <br> line breaks; keep the breaks as spaces."""
    return BeautifulSoup(html.replace("<br>", " ").replace("<br />", " "), "html.parser").get_text(" ")


async def search(
    q: str, scope: str = "exact", dictionaries: str = "", limit: int = 5, offset: int = 0, cap: int = 300
) -> dict[str, Any]:
    """One engine call; returns {"hits": [...], "total": n}. Hit text is cut at `cap` characters."""
    if scope not in SCOPES:
        raise http.ApiError(f"scope must be one of {', '.join(SCOPES)}.")
    params = [("q", q.strip()), ("l", str(limit)), ("o", str(offset)), ("s", scope)]
    if dictionaries:
        params.append(("d", dictionaries))
    _, text = await http.fetch(http.ENGINE + "/search", params, xhr=True)
    d = json.loads(text)
    rs = [r for r in d.get("results", []) if r.get("scope") == scope] or [{}]
    hits = [
        {"dictionary": h["dictionarySlug"], "title": h["title"], "slug": h["slug"], "text": clip(h["summary"], cap)}
        for h in rs[0].get("hits", [])
    ]
    return {"hits": hits, "total": rs[0].get("estimatedTotalHits", 0)}


async def entry(dictionary: str, slug: str, cap: int) -> dict[str, Any]:
    """Full text of one entry, read from the page's embedded data."""
    _, html = await http.fetch(f"/{quote(dictionary)}/{quote(slug)}")
    marker = "__INITIAL_DATA__ = "
    i = html.find(marker)
    if i < 0:
        raise http.ApiError("vajehyab.com page had no entry data.")
    doc = json.JSONDecoder().raw_decode(html[i + len(marker) :])[0]["result"]["document"]
    return {
        "dictionary": doc["dictionarySlug"],
        "title": doc["title"],
        "slug": doc["slug"],
        "text": clip(plain(doc["description"]), cap),
    }


async def suggest(prefix: str, limit: int) -> list[str]:
    _, text = await http.fetch(http.ENGINE + "/suggestion", [("q", prefix.strip())], xhr=True)
    p = prefix.strip()
    # The engine writes "سلا م" for prefix "سلا": a space after the typed part.
    return [re.sub(r"\s+", " ", h["title"]).replace(p + " ", p, 1) for h in json.loads(text).get("hits", [])][:limit]


@tool("Vajehyab: search entries")
async def dic_vajehyab_search(
    query: Annotated[
        str, Field(description="Word or phrase, Persian, English or Arabic.", min_length=1, max_length=100)
    ],
    scope: Annotated[
        str,
        Field(
            description="exact, synonym, antonym, similar, homophone, prefix (starts with) or suffix (rhymes, قافیه)."
        ),
    ] = "exact",
    dictionaries: Dictionaries = "",
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
    offset: Annotated[int, Field(ge=0, le=1000)] = 0,
) -> dict[str, Any]:
    """Search Vajehyab's dictionaries with a scope: exact entries, entries naming it a synonym or antonym, rhymes, sound-alikes.

    Empty `dictionaries` searches all of them. Slugs: dehkhoda, moein, amid, motaradef (synonyms and antonyms), name, en2fa, fa2en, ar2fa, fa2ar, fa2tr, ku2fa,
    farhangestan, sereh, thesis, wiki, salam, teyfi, ganjvajeh, brand and dialects (bakhtiari, dezfuli, gonabadi,
    isfahani, kermanshah, khalkhal, mazani, tehrani). Each hit has dictionary, title, slug and a text
    excerpt (about 300 characters); pass dictionary and slug to dic_vajehyab_entry for the whole entry. `total` is the
    number of matches; page with `offset`.
    """
    return await search(query, scope, dictionaries, limit, offset)


@tool("Vajehyab: full entry")
async def dic_vajehyab_entry(
    dictionary: Annotated[
        str, Field(description="Dictionary slug, e.g. dehkhoda, moein, amid.", pattern=r"^[a-z0-9]+$", max_length=30)
    ],
    slug: Annotated[
        str,
        Field(
            description="Entry slug from a search hit, e.g. سلام or سلام-2 for the second homograph.",
            min_length=1,
            max_length=120,
        ),
    ],
    max_chars: Annotated[int, Field(ge=200, le=20000)] = 4000,
) -> dict[str, Any]:
    """Read one whole Vajehyab entry (for example the full Dehkhoda text with its poetry), cut at `max_chars`.

    Get dictionary and slug from dic_lookup or dic_vajehyab_search hits.
    """
    return await entry(dictionary, slug.strip(), max_chars)
