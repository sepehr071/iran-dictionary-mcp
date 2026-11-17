"""Abadis (abadis.ir) word pages, autocomplete and the Persian names list. Plain functions; merged.py and server.py wrap them."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

from ..registry import tool
from . import http

# Page sections that are sample sentences (see examples), user comments, empty fillers or long encyclopedia extracts.
SKIP = {"جمله های نمونه", "پیشنهاد کاربران", "جدول کلمات", "دانشنامه اسلامی", "دانشنامه عمومی", "دانشنامه آزاد فارسی"}
HEADER_KEYS = {
    "برابر پارسی": "persian_equivalent",
    "معنی انگلیسی": "english",
    "معنی": "meaning",
    "معانی دیگر": "other_meanings",
    "متضاد": "antonyms",
}
LIST_KEYS = {"synonyms", "antonyms", "persian_equivalent", "english"}


def clip(text: str, cap: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def _soup(
    html: str, drop: str = "script, style, i, #meanTools"
) -> BeautifulSoup:  # <i> holds icons, except in name tags
    s = BeautifulSoup(html, "html.parser")
    for x in s.select(drop):
        x.decompose()
    return s


def _header(box: Tag) -> dict[str, Any]:
    """The first box: word, pronunciation and the `<b>label</b>: value` lines separated by <hr>."""
    out: dict[str, Any] = {}
    if (ph := box.select_one("#pho") or box.select_one("#boxWrd > .boxLtr")) is not None:
        out["pronunciation"] = clip(ph.get_text(" "), 60)
        ph.decompose()
    if (h1 := box.find("h1")) is not None:
        h1.decompose()
    segs = [""]  # one segment per <hr>
    for node in box.children:
        if isinstance(node, Tag) and node.name == "hr":
            segs.append("")
        else:
            segs[-1] += node.get_text(" ") if isinstance(node, Tag) else str(node)
    for seg in segs:
        label, sep, value = seg.partition(":")
        label, value = label.strip(), value.strip()
        if not sep or not value:
            continue
        key = "synonyms" if label.startswith("مترادف") else HEADER_KEYS.get(label, label)
        out[key] = [x.strip() for x in re.split(r"[،,]", value) if x.strip()] if key in LIST_KEYS else value
    return out


def parse_word(html: str | None) -> dict[str, Any]:
    """Split a /fatofa/ or /entofa/ page into header fields and {section title: full text}."""
    if html is None:
        return {"found": False}
    s = _soup(html)
    main = s.find("main")
    if main is None or main.select_one(".boxInfo") is not None:
        return {"found": False}
    out: dict[str, Any] = {"found": True}
    sections: dict[str, str] = {}
    for box in main.find_all("div", class_="lun", recursive=False):
        title = box.get("t")
        if "boxMain" in box.get("class", []):
            out.update(_header(box))
        elif title not in SKIP and (art := box.find("article")) is not None:
            text = re.sub(r"\s+", " ", art.get_text(" ")).strip()
            if text and text != "جا خالی!":
                sections[str(title)] = text
    out["sections"] = sections
    return out


async def page_html(word: str) -> str | None:
    """The word's page HTML (abadis redirects Latin words to /entofa/), or None when the word is unknown."""
    try:
        return (await http.fetch(f"/fatofa/{quote(word.strip())}/"))[1]
    except http.ApiError as e:
        if e.status in (404, 410):  # unknown English words answer 410
            return None
        raise


def trim(page: dict[str, Any], *, full: bool, keep: set[str] | None = None) -> dict[str, Any]:
    """Cap each section (400 characters, 3000 with `full`); `keep` limits which sections stay."""
    cap = 3000 if full else 400
    out = {k: v for k, v in page.items() if k != "sections"}
    out["dictionaries"] = {t: clip(x, cap) for t, x in page.get("sections", {}).items() if keep is None or t in keep}
    return out


def examples(html: str, limit: int) -> list[dict[str, str]]:
    """Sample sentences with their Persian translation from the 'جمله های نمونه' box."""
    s = _soup(html)
    box = s.find("div", attrs={"t": "جمله های نمونه"})
    if box is None:
        return []
    out = []
    for exp in box.select(".anaTermExp"):
        mean = exp.find_next_sibling(class_="anaTermMean")
        if mean is not None:
            if (first := mean.select_one(".tm")) is not None:
                for x in first.select("span, .tmR"):
                    x.decompose()
                mean = first
            out.append(
                {
                    "text": clip(re.sub(r"^\d+\.\s*", "", exp.get_text(" ")), 200),
                    "translation": clip(mean.get_text(" "), 200),
                }
            )
        if len(out) >= limit:
            break
    return out


async def suggest(prefix: str, limit: int) -> list[str]:
    """Autocomplete from the site's own ajax list (Persian or English prefix)."""
    _, html = await http.fetch("/ajaxcmd/getaclist/", [("exp", prefix.strip())], xhr=True)
    return [x.get_text().strip() for x in BeautifulSoup(html.lstrip(":"), "html.parser").find_all("span")][:limit]


async def names(
    gender: str, origin: str, category: str, letter: str | None, query: str | None, sort: str, page: int, limit: int
) -> dict[str, Any]:
    path = "/name/"
    if gender != "all" or origin != "all" or category != "all":
        path += f"{gender}/"
    if origin != "all" or category != "all":
        path += f"{origin}/"
    if category != "all":
        path += f"{category}/"
    params = [
        (k, v)
        for k, v in (
            ("ch", letter),
            ("exp", query),
            ("sort", sort if sort != "pop" else None),
            ("pn", str(page) if page > 1 else None),
        )
        if v
    ]
    _, html = await http.fetch(path, params)
    s = _soup(html, "script, style")
    items = []
    for b in s.select("span.boxName"):
        a = b.find("a")
        tags = [t.get_text(" ", strip=True) for t in b.find_all("i")]
        items.append(
            {
                "name": a.get_text(strip=True),
                "pronunciation": (b.find("pr").get_text(strip=True) if b.find("pr") else None),
                "meaning": clip((b.find("p").get_text(" ") if b.find("p") else ""), 160),
                "gender": tags[0] if tags else None,
                "origin": tags[1] if len(tags) > 1 else None,
                "category": tags[2] if len(tags) > 2 else None,
            }
        )
    return {"count": len(items), "names": items[:limit]}


@tool("Abadis: Persian first names")
async def dic_abadis_names(
    query: str | None = None,
    gender: str = "all",
    origin: str = "all",
    category: str = "all",
    letter: str | None = None,
    sort: str = "pop",
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    """Search Abadis's Persian first-name list by name, gender, origin, theme or first letter.

    gender: all, boy, girl. origin: all, farsi, arabi, kurdi, turki, lori, gilaki, mazani, balochi, zoroastrian,
    avestai, pahlavi, sanskrit, hebrew, hindi, armeni, greek, french, italian, english, latin, aramaic and others.
    category: all, religious, historic, artistic, nature, bird, flower, galactic. letter: one Persian letter, e.g. پ.
    sort: pop (popular, default), asc (alphabetical) or desc. 100 names per page; `limit` trims the answer.
    Each item has the name, pronunciation, a short meaning, gender, origin and theme. A name's full entry is in dic_lookup.
    """
    if gender not in ("all", "boy", "girl"):
        raise http.ApiError("gender must be all, boy or girl.")
    for v in (origin, category, sort):
        if not re.fullmatch(r"[a-z]+", v):
            raise http.ApiError("origin, category and sort are lowercase English words, e.g. farsi, flower, pop.")
    return await names(gender, origin, category, letter, query, sort, max(page, 1), min(max(limit, 1), 100))
