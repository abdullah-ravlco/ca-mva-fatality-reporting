"""Step 2: open every article in finalized_raw.json, read its text and page metadata, and
re-check crash date / location / relevance on the full text.

Input:  data/source_collection/finalized_raw.json (one article per item)
Output: data/link_extraction/articles.json          - one record per article, every outcome
        data/link_extraction/extraction_errors.json - fetch/decode failures with diagnostics

A page that is blocked (401/403, CAPTCHA) or otherwise fails is retried through
the site's WordPress REST API (blocked pages only) and then the Wayback Machine.
If every route fails the article is recorded with the page's own status (e.g.
"blocked") and keeps its collected data.
"""

import hashlib
import logging
from collections import Counter
from urllib.parse import urlsplit

from config.settings import module_data_dir
from src.core.checkpoint import Checkpoint
from src.core.date_utils import iso, reference_time, utc_now
from src.core.logger import configure_logging
from src.core.parallel import parallel_map
from src.core.outputs import read_json, write_stage_json
from src.modules.link_extraction.archive_fallback import fetch_archived_article
from src.modules.link_extraction.full_text_filter import KEEP, apply_full_text_filter
from src.modules.link_extraction.google_news_decoder import GoogleNewsDecodeError, decode_google_news_url, is_google_news_url
from src.modules.link_extraction.page_fetcher import PageFetchError, fetch_page_with_url
from src.modules.link_extraction.text_extractor import extract_article
from src.modules.link_extraction.wordpress_fallback import fetch_wordpress_article
from src.modules.source_collection.deduplication import canonical_url


MODULE = "link_extraction"
INPUT_FILE = module_data_dir("source_collection") / "finalized_raw.json"
ARTICLES_FILE = "articles.json"
ERRORS_FILE = "extraction_errors.json"

# Shorter text is treated as an error page or redirect stub.
MIN_TEXT_CHARS = 100
# Upper bound on articles opened for one case (cases are usually 1-3 articles; a
# widely covered crash can have more, and every link of a case should be opened).
MAX_ARTICLES_PER_CASE = 20
# Articles opened at the same time. page_fetcher spaces requests to one host, so articles
# from different outlets overlap while each outlet is still approached politely.
FETCH_WORKERS = 8

# Redirect pages that don't contain the article (Google News needs JavaScript).
AGGREGATOR_HOSTS = ("news.google.com",)

# fetch_status values
OK = "ok"
BLOCKED = "blocked"                 # HTTP 401/403 or CAPTCHA page, and every fallback failed
FAILED = "failed"                   # other HTTP/network error
TOO_SHORT = "too_short"             # page fetched, but too little article text
DECODE_FAILED = "decode_failed"     # Google News link could not be turned into a publisher URL
OFFICIAL_ENTRY = "official_entry_text"  # entry text already in the listing (not fetched)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# Input
# --------------------------------------------------

def load_cases():
    """One single-article "case" per finalized item (the structure process_case works on)."""
    data = read_json(INPUT_FILE)
    if data is None:
        raise FileNotFoundError(f"Input file not found or unreadable: {INPUT_FILE}")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Expected a JSON object with an 'items' list.")

    cases = []
    for item in data["items"]:
        url = normalize_url(item.get("url")) if isinstance(item, dict) else None
        if not url:
            continue
        cases.append({
            "case_id": "item-" + hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12],
            "confidence": "finalized",
            "open_first": url,
            "title": item.get("title"),
            "publisher": item.get("source_name"),
            "sources": [{
                "url": url,
                "title": item.get("title"),
                "description": item.get("description"),
                "publisher": item.get("source_name"),
                "published_at": item.get("published_date"),
                "source_type": "news",
            }],
        })
    return cases


def normalize_url(url):
    """Absolute http(s) URL, or None. "//host/path" becomes "https://host/path"."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("https://", "http://")) else None


def case_sources(case):
    """The case's sources (best-first, as ordered by source collection), open_first first."""
    sources = [s for s in case.get("sources") or [] if isinstance(s, dict) and normalize_url(s.get("url"))]
    first = normalize_url(case.get("open_first"))
    if first and not any(normalize_url(s["url"]) == first for s in sources):
        sources.insert(0, {"url": first, "title": case.get("title"), "publisher": case.get("publisher")})
    return sources[:MAX_ARTICLES_PER_CASE]


def is_listing_entry(url):
    """Coroner entries without their own page (".../coroner-press-release/#case-123")."""
    return "#case-" in (url or "")


# --------------------------------------------------
# Fetching
# --------------------------------------------------

def _is_blocked(diagnostics):
    diagnostics = diagnostics or {}
    return diagnostics.get("status_code") in (401, 403) or bool(diagnostics.get("block_type"))


def _extract(html, url, final_url, method):
    """Result dict for fetched HTML: ok with text, or a failure explaining why."""
    if urlsplit(final_url or "").netloc in AGGREGATOR_HOSTS:
        return {"ok": False, "fetch_status": FAILED,
                "error": "Redirect stayed on news.google.com; the publisher article was not reached.",
                "diagnostics": {"requested_url": url, "final_url": final_url}}

    extracted = extract_article(html, final_url)
    length = len(extracted["text"].strip()) if extracted["text"] else 0
    if length < MIN_TEXT_CHARS:
        return {"ok": False, "fetch_status": TOO_SHORT, "final_url": final_url, "metadata": extracted["metadata"],
                "error": f"Suspiciously short extracted text ({length} characters).",
                "diagnostics": {"requested_url": url, "final_url": final_url, "method": method}}

    return {"ok": True, "fetch_status": OK, "text": extracted["text"], "metadata": extracted["metadata"],
            "final_url": final_url, "method": method}


def _failure(exc):
    status = BLOCKED if _is_blocked(exc.diagnostics) else FAILED
    return {"ok": False, "fetch_status": status, "error": str(exc), "diagnostics": exc.diagnostics}


def attempt(url):
    """Fetch and extract one publisher/official URL. Returns a result dict (never raises).

    Order: the page itself (browser impersonation via curl_cffi when installed)
    -> WordPress REST API, only when the page was blocked (401/403/CAPTCHA)
    -> Wayback Machine snapshot, for any remaining failure (block, 429, timeout,
       server error, too little text).
    CAPTCHA pages are never solved; the next fallback is used instead.
    A failed result keeps the page's own fetch_status (e.g. "blocked").
    """
    errors = []

    try:
        html, final_url = fetch_page_with_url(url)
        result = _extract(html, url, final_url, "page")
    except PageFetchError as exc:
        result = _failure(exc)
    except Exception as exc:
        result = {"ok": False, "fetch_status": FAILED, "error": f"{type(exc).__name__}: {exc}",
                  "diagnostics": {"requested_url": url}}
    if result["ok"]:
        return result
    errors.append(("page", result))

    if result["fetch_status"] == BLOCKED:
        try:
            html, api_url = fetch_wordpress_article(url)
            result = _extract(html, url, api_url, "wordpress_api")
        except PageFetchError as exc:
            result = _failure(exc)
        if result["ok"]:
            return result
        errors.append(("wordpress_api", result))

    try:
        html, snapshot_url = fetch_archived_article(url)
        result = _extract(html, url, snapshot_url, "wayback_snapshot")
    except PageFetchError as exc:
        result = _failure(exc)
    if result["ok"]:
        return result
    errors.append(("wayback_snapshot", result))

    first = errors[0][1]
    return dict(
        first,
        error="; ".join(f"{method}: {r['error']}" for method, r in errors),
        diagnostics=dict(
            first.get("diagnostics") or {},
            fallbacks={method: r.get("diagnostics") for method, r in errors[1:]},
        ),
    )


def resolve_url(url, decoded):
    """(publisher_url, error) - Google News links are decoded once and cached."""
    if not is_google_news_url(url):
        return url, None
    if url not in decoded:
        try:
            target = normalize_url(decode_google_news_url(url))
            if not target or urlsplit(target).netloc in AGGREGATOR_HOSTS:
                # Still a Google page: fetching it would only return a redirect stub.
                decoded[url] = (None, f"Google News link decoded to a non-publisher URL: {target}")
            else:
                decoded[url] = (target, None)
        except GoogleNewsDecodeError as exc:
            decoded[url] = (None, f"Google News link could not be decoded: {exc}")
        except Exception as exc:
            decoded[url] = (None, f"Google News decode failed: {type(exc).__name__}: {exc}")
    return decoded[url]


# --------------------------------------------------
# Article records
# --------------------------------------------------

def article_id(url):
    return "art-" + hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


def new_article(case, source, rss_url):
    """Article record with everything known before fetching (RSS/listing data)."""
    return {
        "article_id": article_id(rss_url),
        "case_id": case.get("case_id"),
        "case_confidence": case.get("confidence"),
        "case_incident_date": case.get("incident_date"),
        "url": rss_url,                   # best known URL: final publisher URL once fetched
        "rss_url": rss_url,
        "title": source.get("title") or case.get("title"),
        "description": source.get("description"),
        "publisher": source.get("publisher"),
        "published_at": source.get("published_at"),
        "source_type": source.get("source_type"),
        "guid": source.get("guid"),
        "discovered_by_queries": list(source.get("discovered_by_queries") or []),
        "discovered_by_sources": list(source.get("discovered_by_sources") or []),
        "resolved_url": None,
        "final_url": None,
        "canonical_url": None,
        "fetch_status": None,
        "error": None,
        "extraction_method": None,
        "text_available": False,
        "text": None,
        "metadata": {},
        "case_number": case.get("case_number") if source.get("source_type") == "coroner" else None,
    }


def fill_from_result(article, target, result):
    metadata = result.get("metadata") or {}
    final_url = result.get("final_url")
    page_canonical = normalize_url(metadata.get("canonical_url"))
    article.update({
        "url": final_url or target,
        "resolved_url": target,
        "final_url": final_url,
        "canonical_url": canonical_url(page_canonical or final_url or target),
        "fetch_status": result["fetch_status"],
        "error": result.get("error"),
        "extraction_method": result.get("method"),
        "text_available": bool(result.get("ok")),
        "text": result.get("text") if result.get("ok") else None,
        "metadata": metadata,
    })
    # Fill gaps in the RSS data from the page itself; RSS values are kept when present.
    article["title"] = article["title"] or metadata.get("page_title")
    article["publisher"] = article["publisher"] or metadata.get("site_name")
    article["description"] = article["description"] or metadata.get("description")
    if result.get("ok"):
        article["article_id"] = article_id(article["canonical_url"])
    return article


def error_entry(case, article, result):
    return {
        "case_id": case.get("case_id"),
        "article_id": article["article_id"],
        "title": article["title"],
        "link": article["rss_url"],
        "resolved_link": article["resolved_url"],
        "fetch_status": article["fetch_status"],
        "error": article["error"],
        "diagnostics": (result or {}).get("diagnostics"),
    }


def process_case(case, attempts, decoded, now):
    """(articles, errors) for one case."""
    articles, errors = [], []
    seen = set()  # canonical URLs already handled in this case

    for source in case_sources(case):
        rss_url = normalize_url(source["url"])
        if canonical_url(rss_url) in seen:
            continue
        seen.add(canonical_url(rss_url))
        article = new_article(case, source, rss_url)

        if is_listing_entry(rss_url):
            # The listing page holds every case; its entry text is the evidence.
            article.update(
                fetch_status=OFFICIAL_ENTRY, text=article["description"],
                text_available=bool(article["description"]), canonical_url=canonical_url(rss_url),
            )
            articles.append(apply_full_text_filter(article, now))
            continue

        target, decode_error = resolve_url(rss_url, decoded)
        if target is None:
            article.update(fetch_status=DECODE_FAILED, error=decode_error)
            errors.append(error_entry(case, article, None))
            articles.append(apply_full_text_filter(article, now))
            continue
        if target != rss_url and canonical_url(target) in seen:
            continue  # a Google link to an article already handled in this case
        seen.add(canonical_url(target))

        if target not in attempts:
            logger.info("  %s", target)
            attempts[target] = attempt(target)
        result = attempts[target]
        fill_from_result(article, target, result)
        if article["final_url"]:
            seen.add(canonical_url(article["final_url"]))

        if result["ok"]:
            logger.info("    [OK] %s characters", len(result["text"]))
        else:
            logger.info("    [%s] %s", result["fetch_status"].upper(), result.get("error"))
            errors.append(error_entry(case, article, result))
        articles.append(apply_full_text_filter(article, now))

    return articles, errors


# --------------------------------------------------
# Run
# --------------------------------------------------

CHECKPOINT_FILE = module_data_dir(MODULE) / "progress.jsonl"
PARTIAL_EVERY = 50  # write articles.json (marked incomplete) after this many more articles


def input_checkpoint(cases):
    """Articles finished by an interrupted run on the same input, if any."""
    urls = "|".join(sorted(case["open_first"] for case in cases))
    checkpoint = Checkpoint(CHECKPOINT_FILE, hashlib.sha1(urls.encode()).hexdigest())
    if checkpoint.items:
        logger.info("Resuming an interrupted run: %s of %s articles already done", len(checkpoint.items), len(cases))
    return checkpoint


def main():
    now = reference_time()
    cases = load_cases()
    checkpoint = input_checkpoint(cases)
    attempts = {}  # publisher/official URL -> result; fetched at most once per run
    decoded = {}   # Google News URL -> (publisher URL, error); decoded at most once
    all_articles, all_errors = [], []

    logger.info("Input file: %s", INPUT_FILE)
    logger.info("Articles to open: %s", len(cases))

    def work(numbered_case):
        index, case = numbered_case
        done = checkpoint.get(case["case_id"])
        if done is not None:
            return done["articles"], done["errors"]
        articles, errors = process_case(case, attempts, decoded, now)
        checkpoint.add(case["case_id"], {"articles": articles, "errors": errors})
        logger.info("[%s/%s] %s: %s", index, len(cases), case.get("case_id"),
                    ", ".join(a["fetch_status"] or "?" for a in articles) or "no article")
        return articles, errors

    for articles, errors in parallel_map(work, enumerate(cases, start=1), FETCH_WORKERS):  # input order
        all_articles.extend(articles)
        all_errors.extend(errors)
        if len(all_articles) // PARTIAL_EVERY != (len(all_articles) - len(articles)) // PARTIAL_EVERY:
            write_stage_json(MODULE, ARTICLES_FILE, {
                "run_at": iso(utc_now()), "input_file": str(INPUT_FILE), "complete": False,
                "articles": all_articles,
            })

    fetch_counts = Counter(a["fetch_status"] for a in all_articles)
    decisions = Counter(a["filter_decision"] for a in all_articles)
    drop_reasons = Counter(r for a in all_articles if a["filter_decision"] != KEEP for r in a["filter_reasons"])
    summary = {
        "cases": len(cases),
        "articles": len(all_articles),
        "unique_urls_fetched": len(attempts),
        "google_news_links_decoded": f"{sum(1 for t, _ in decoded.values() if t)}/{len(decoded)}",
        "by_fetch_status": dict(fetch_counts),
        "by_filter_decision": dict(decisions),
        "drop_reasons": dict(drop_reasons),
        "cases_without_text": sum(
            1 for case in cases
            if not any(a["text_available"] for a in all_articles if a["case_id"] == case.get("case_id"))
        ),
    }

    articles_file = write_stage_json(MODULE, ARTICLES_FILE, {
        "run_at": iso(utc_now()),
        "input_file": str(INPUT_FILE),
        "complete": True,
        "summary": summary,
        "articles": all_articles,
    })
    errors_file = write_stage_json(MODULE, ERRORS_FILE, all_errors)
    checkpoint.clear()  # the real output is written; the next run starts fresh

    logger.info("EXTRACTION COMPLETE: %s", summary)
    logger.info("Articles: %s", articles_file)
    logger.info("Diagnostics: %s", errors_file)

    if cases and not any(a["text_available"] for a in all_articles):
        logger.error("No article text could be read for any case.")
        return False
    return True


if __name__ == "__main__":
    configure_logging()
    main()
