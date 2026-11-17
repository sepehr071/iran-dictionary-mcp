"""Tools that ask Abadis and Vajehyab the same question and return the answers side by side."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .abadis import dictionary as ab
from .registry import tool
from .vajehyab import dictionary as vy

SOURCE = Annotated[
    Literal["all", "abadis", "vajehyab"],
    Field(description="all (both side by side) or one site."),
]
Word = Annotated[
    str, Field(description="Persian, English or Arabic word or short phrase.", min_length=1, max_length=100)
]
SHAPE_ERRORS = (ToolError, KeyError, IndexError, TypeError, ValueError)
# Vajehyab dictionary slugs for each translation direction; Abadis translates by the script of the word itself.
DIRECTIONS = {"en-fa": "en2fa", "fa-en": "fa2en", "ar-fa": "ar2fa", "fa-ar": "fa2ar", "fa-tr": "fa2tr"}
LOOKUP_DICTIONARIES = "dehkhoda,moein,amid,name,farhangestan,thesis,sereh"
TRANSLATION_SECTIONS = {"بررسی کلمه", "فارسی به عربی", "مترادف ها", "انگلیسی به انگلیسی"}


async def _each(source: str, **jobs: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Run the chosen sites at once. A failing site becomes {"error": ...}; all failing raises."""
    jobs = {n: f for n, f in jobs.items() if source in (n, "all")}
    results = await asyncio.gather(*(f() for f in jobs.values()), return_exceptions=True)
    out: dict[str, Any] = {}
    for name, r in zip(jobs, results, strict=True):
        if isinstance(r, SHAPE_ERRORS):
            out[name] = {"error": str(r) if isinstance(r, ToolError) else f"unexpected answer shape ({r!r})"}
        elif isinstance(r, BaseException):
            raise r
        else:
            out[name] = r
    if all("error" in v for v in out.values()):
        raise ToolError("; ".join(f"{n}: {v['error']}" for n, v in out.items()))
    return out


@tool("Look up a word")
async def dic_lookup(
    word: Word,
    source: SOURCE = "all",
    full: Annotated[
        bool, Field(description="Longer text per dictionary (about 3000 characters instead of 400).")
    ] = False,
) -> dict[str, Any]:
    """Look a word up in the Persian dictionaries of Abadis and Vajehyab, one block per site.

    Abadis: synonyms, Persian equivalents, English meanings, then Dehkhoda, Farhang-e Farsi, Moein, Amid, names and
    Arabic equivalents. Vajehyab: hits from Dehkhoda, Moein, Amid, names, Farhangestan and others, each with a text
    excerpt, slug and dictionary slug. Meaning text is cut short; set `full` for more, or use dic_vajehyab_entry.
    """

    async def abadis() -> dict[str, Any]:
        return ab.trim(ab.parse_word(await ab.page_html(word)), full=full)

    async def vajehyab() -> dict[str, Any]:
        r = await vy.search(word, "exact", LOOKUP_DICTIONARIES, 10, cap=3000 if full else 400)
        if full:  # the engine's excerpt is about 200 characters; the entry page has the whole text
            main = {}
            for h in r["hits"]:
                main.setdefault(h["dictionary"], h)
            wanted = [h for d, h in main.items() if d in ("dehkhoda", "moein", "amid")]
            full_entries = await asyncio.gather(*(vy.entry(h["dictionary"], h["slug"], 3000) for h in wanted))
            for h, e in zip(wanted, full_entries, strict=True):
                h["text"] = e["text"]
        return {"found": bool(r["hits"]), **r}

    return await _each(source, abadis=abadis, vajehyab=vajehyab)


@tool("Synonyms and antonyms")
async def dic_synonyms(word: Word, source: SOURCE = "all") -> dict[str, Any]:
    """Find synonyms, antonyms and pure-Persian equivalents (برابر پارسی) of a Persian word, one block per site.

    Abadis gives flat lists. Vajehyab gives the Motaradef dictionary entry (numbered senses, antonyms after ≠) and the
    words that list the query as their antonym or synonym (`also_listed_by`).
    """

    async def abadis() -> dict[str, Any]:
        p = ab.parse_word(await ab.page_html(word))
        return {k: p[k] for k in ("found", "synonyms", "antonyms", "persian_equivalent") if k in p}

    async def vajehyab() -> dict[str, Any]:
        entry, anti, rev = await asyncio.gather(
            vy.search(word, "exact", "motaradef", 1, cap=1500),
            vy.search(word, "antonym", "motaradef", 10, cap=100),
            vy.search(word, "synonym", "motaradef", 30, cap=1),
        )
        text = entry["hits"][0]["text"] if entry["hits"] else ""
        syn, _, ant = text.partition("≠")
        out: dict[str, Any] = {"found": bool(text), "synonyms": syn.strip()}
        names = [x.strip() for x in ant.replace("،", ",").split(",") if x.strip()]
        names += [h["title"] for h in anti["hits"] if h["title"] not in names]  # entries that list the query after ≠
        out["antonyms"] = names
        # the site page merges these in: entries whose synonym list contains the query
        out["also_listed_by"] = [h["title"] for h in rev["hits"] if h["title"] not in syn]
        return out

    return await _each(source, abadis=abadis, vajehyab=vajehyab)


@tool("Translate a word")
async def dic_translate(
    word: Word,
    direction: Annotated[
        Literal["en-fa", "fa-en", "ar-fa", "fa-ar", "fa-tr"],
        Field(description="en-fa English to Persian, fa-en, ar-fa Arabic to Persian, fa-ar, fa-tr Persian to Turkish."),
    ] = "en-fa",
    source: SOURCE = "all",
    examples: Annotated[int, Field(description="Abadis sample sentences with Persian translation.", ge=0, le=10)] = 3,
) -> dict[str, Any]:
    """Translate a single word or short phrase between Persian and English, Arabic or Turkish, one block per site.

    Vajehyab covers every direction. Abadis picks the direction from the script of the word (Latin: English to Persian,
    Persian: Persian to English and Arabic), adds parts of speech and sample sentences, and has no Arabic to Persian or
    Turkish. Arabic headwords match best when typed with Persian letters (ک, ی). Not for whole sentences.
    """

    async def abadis() -> dict[str, Any]:
        if direction in ("ar-fa", "fa-tr"):
            raise ToolError(f"Abadis has no {direction} dictionary; use source='vajehyab'.")
        html = await ab.page_html(word)
        out = ab.trim(ab.parse_word(html), full=False, keep=TRANSLATION_SECTIONS)
        if html is not None and examples:
            out["examples"] = ab.examples(html, examples)
        return out

    async def vajehyab() -> dict[str, Any]:
        r = await vy.search(word, "exact", DIRECTIONS[direction], 5, cap=500)
        return {"found": bool(r["hits"]), "translations": r["hits"]}

    jobs = {"abadis": abadis, "vajehyab": vajehyab}
    if source == "all" and direction in ("ar-fa", "fa-tr"):
        del jobs["abadis"]
    return await _each(source, **jobs)


@tool("Autocomplete words")
async def dic_suggest(
    prefix: Annotated[str, Field(description="Start of a word, Persian or English.", min_length=1, max_length=50)],
    source: SOURCE = "all",
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Complete the start of a word into dictionary headwords, one list per site; use it to find the exact spelling."""

    async def abadis() -> list[str]:
        return await ab.suggest(prefix, limit)

    async def vajehyab() -> list[str]:
        return await vy.suggest(prefix, limit)

    return await _each(source, abadis=abadis, vajehyab=vajehyab)
