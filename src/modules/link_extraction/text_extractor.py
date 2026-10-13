"""Article text and page metadata from webpage HTML.

Metadata comes from trafilatura plus the page's own tags: <link rel="canonical">,
Open Graph / article:* meta tags and JSON-LD (NewsArticle, Article, ...). Values
are reported as found; missing values stay None.
"""

import json

import trafilatura
from bs4 import BeautifulSoup


ARTICLE_TYPES = {"article", "newsarticle", "reportagenewsarticle", "blogposting", "webpage"}


def extract_text(html: str) -> str:
    """Extract readable article text from webpage HTML."""

    # First, use Trafilatura's article-text extraction.
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_precision=False,
    )

    if text and text.strip():
        return text.strip()

    # Fallback when article extraction does not find usable text.
    soup = BeautifulSoup(html, "html.parser")

    for element in soup([
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "aside",
        "noscript",
    ]):
        element.decompose()

    return soup.get_text(separator="\n", strip=True)


def _meta(soup, *names):
    """Content of the first <meta property=...> or <meta name=...> found."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") and tag["content"].strip():
            return tag["content"].strip()
    return None


def _names(value):
    """Author/publisher value(s) from JSON-LD: a string, an object with "name", or a list."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return _names(value.get("name"))
    if isinstance(value, list):
        return [name for item in value for name in _names(item)]
    return []


def _json_ld_objects(soup):
    """Every JSON-LD object on the page, flattening lists and @graph."""
    objects = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                objects.append(item)
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])
    return objects


def _is_article(obj):
    types = obj.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.lower() in ARTICLE_TYPES for t in types)


def json_ld_article(soup):
    """Published/modified dates, authors, publisher and headline from the page's JSON-LD."""
    for obj in _json_ld_objects(soup):
        if not _is_article(obj):
            continue
        return {
            "headline": obj.get("headline"),
            "date_published": obj.get("datePublished"),
            "date_modified": obj.get("dateModified"),
            "authors": _names(obj.get("author")),
            "publisher": next(iter(_names(obj.get("publisher"))), None),
            "url": obj.get("url") if isinstance(obj.get("url"), str) else None,
        }
    return {}


def page_metadata(html, url=None):
    """Metadata from the page tags (canonical, Open Graph, article:*, JSON-LD) and trafilatura."""
    soup = BeautifulSoup(html or "", "html.parser")
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    json_ld = json_ld_article(soup)

    try:
        document = trafilatura.bare_extraction(html, url=url, with_metadata=True)
        found = (document.as_dict() if hasattr(document, "as_dict") else document) or {}
    except Exception:  # metadata is best effort; text extraction has its own fallback
        found = {}

    authors = json_ld.get("authors") or ([found["author"]] if found.get("author") else [])
    author_meta = _meta(soup, "author", "article:author")
    if not authors and author_meta:
        authors = [author_meta]

    return {
        "page_title": _meta(soup, "og:title") or found.get("title")
        or (soup.title.get_text(" ", strip=True) if soup.title else None),
        "canonical_url": (canonical_tag.get("href") if canonical_tag else None) or _meta(soup, "og:url") or json_ld.get("url"),
        "site_name": _meta(soup, "og:site_name") or json_ld.get("publisher") or found.get("sitename"),
        "date_published": json_ld.get("date_published")
        or _meta(soup, "article:published_time", "og:published_time", "datePublished", "pubdate", "publish-date")
        or found.get("date"),
        "date_modified": json_ld.get("date_modified") or _meta(soup, "article:modified_time", "og:updated_time", "dateModified"),
        "authors": authors,
        "description": _meta(soup, "og:description", "description") or found.get("description"),
        "og_type": _meta(soup, "og:type"),
        "section": _meta(soup, "article:section"),
        "json_ld_found": bool(json_ld),
    }


def extract_article(html, url=None):
    """{"text", "metadata"} for one fetched page."""
    return {"text": extract_text(html), "metadata": page_metadata(html, url)}
