import json
from urllib.parse import urlsplit

from config.settings import module_data_dir
from src.modules.link_extraction.archive_fallback import fetch_archived_article
from src.modules.link_extraction.google_news_decoder import GoogleNewsDecodeError, decode_google_news_url, is_google_news_url
from src.modules.link_extraction.page_fetcher import fetch_page_with_url, PageFetchError
from src.modules.link_extraction.text_extractor import extract_text
from src.modules.link_extraction.wordpress_fallback import fetch_wordpress_article


INPUT_FILE = module_data_dir("source_collection") / "sources_to_open.json"

DATA_DIR = module_data_dir("link_extraction")
OUTPUT_FILE = DATA_DIR / "extracted_text.json"
ERROR_FILE = DATA_DIR / "extraction_errors.json"

# Shorter text is treated as an error page or redirect stub; the next URL is tried.
MIN_TEXT_CHARS = 100

# Redirect pages that don't contain the article (Google News needs JavaScript).
AGGREGATOR_HOSTS = ("news.google.com",)


def load_records():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE}"
        )

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")

    records = []

    # Include both confirmed and doubtful records.
    # Exclude anything explicitly marked rejected.
    for category in ("confirmed", "doubtful"):
        items = data.get(category, [])

        if not isinstance(items, list):
            raise ValueError(f"'{category}' must contain a list.")

        for record in items:
            if not isinstance(record, dict):
                continue

            confidence = str(
                record.get("confidence", category)
            ).strip().lower()

            if confidence == "rejected":
                continue

            record = record.copy()
            record["_input_category"] = category
            records.append(record)

    return records


def normalize_url(url):
    """Absolute http(s) URL, or None. "//host/path" becomes "https://host/path"."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.startswith(("https://", "http://")) else None


def candidate_urls(record):
    """open_first, then the alternatives in "sources" (already ordered best-first
    by source collection), without repeats."""
    raw = [record.get("open_first"), record.get("url"), record.get("link")]
    raw += [
        source.get("url")
        for source in record.get("sources") or []
        if isinstance(source, dict)
    ]

    urls = []
    for item in raw:
        url = normalize_url(item)
        if url and url not in urls:
            urls.append(url)
    return urls


def confidence_of(record):
    return record.get("confidence", record.get("_input_category"))


def _extract(html, url, final_url, method):
    """Result dict for fetched HTML: ok with text, or a failure explaining why."""
    if urlsplit(final_url or "").netloc in AGGREGATOR_HOSTS:
        return {
            "ok": False,
            "error": "Redirect stayed on news.google.com; the publisher article was not reached.",
            "diagnostics": {"requested_url": url, "final_url": final_url},
        }

    text = extract_text(html)
    length = len(text.strip()) if text else 0

    # Flag short pages, such as error pages or redirect stubs.
    if length < MIN_TEXT_CHARS:
        return {
            "ok": False,
            "error": f"Suspiciously short extracted text ({length} characters).",
            "diagnostics": {"requested_url": url, "final_url": final_url, "method": method},
        }

    return {"ok": True, "text": text, "final_url": final_url, "method": method}


def _failure(exc):
    return {"ok": False, "error": str(exc), "diagnostics": exc.diagnostics}


def _is_blocked(diagnostics):
    diagnostics = diagnostics or {}
    return diagnostics.get("status_code") in (401, 403) or bool(diagnostics.get("block_type"))


def attempt(url):
    """Fetch and extract one publisher/official URL. Returns a result dict (never raises).

    Order: the page itself (browser impersonation via curl_cffi when installed)
    -> WordPress REST API, only when the page was blocked (401/403/CAPTCHA)
    -> Wayback Machine snapshot, for any remaining failure (block, 429, timeout,
       server error, too little text).
    CAPTCHA pages are never solved; the next fallback is used instead.
    """
    errors = []

    try:
        html, final_url = fetch_page_with_url(url)
        result = _extract(html, url, final_url, "page")
    except PageFetchError as exc:
        result = _failure(exc)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "diagnostics": {"requested_url": url}}
    if result["ok"]:
        return result
    errors.append(("page", result))

    if _is_blocked(result.get("diagnostics")):
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
    return {
        "ok": False,
        "error": "; ".join(f"{method}: {r['error']}" for method, r in errors),
        "diagnostics": dict(
            first.get("diagnostics") or {},
            fallbacks={method: r.get("diagnostics") for method, r in errors[1:]},
        ),
    }


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


def print_failure(result):
    print(f"    [FAILED] {result['error']}")
    diagnostics = result.get("diagnostics") or {}
    if diagnostics.get("status_code") or diagnostics.get("server"):
        print("    HTTP status:", diagnostics.get("status_code", "not available"))
        print("    Final URL:", diagnostics.get("final_url", "not available"))
        print("    Server:", diagnostics.get("server", "not available"))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    records = load_records()
    extracted_records = []
    errors = []
    attempts = {}  # publisher/official URL -> result; fetched at most once per run
    decoded = {}   # Google News URL -> (publisher URL, error); decoded at most once

    print(f"Input file: {INPUT_FILE}")
    print(f"Confirmed + doubtful records: {len(records)}")
    print("\nStarting webpage extraction...\n")

    for index, record in enumerate(records, start=1):
        urls = candidate_urls(record)

        print(f"[{index}/{len(records)}] [{confidence_of(record)}] {record.get('case_id')} ({len(urls)} URL(s))")

        if not urls:
            errors.append({
                "case_id": record.get("case_id"),
                "confidence": confidence_of(record),
                "title": record.get("title"),
                "error": "Record has no valid URL in open_first or sources.",
            })
            continue

        success_url = None
        success_link = None
        tried = []

        for position, url in enumerate(urls, start=1):
            target, decode_error = resolve_url(url, decoded)
            tried.append(url)

            if target is None:
                print(f"  ({position}) {url}\n    [FAILED] {decode_error}")
                errors.append({
                    "case_id": record.get("case_id"),
                    "confidence": confidence_of(record),
                    "title": record.get("title"),
                    "link": url,
                    "attempt": position,
                    "error": decode_error,
                })
                continue

            label = url if target == url else f"{url}\n      -> decoded: {target}"

            if target in attempts:
                result = attempts[target]
                print(f"  ({position}) {label} [already attempted: {'success' if result['ok'] else 'failed'}]")
            else:
                print(f"  ({position}) {label}")
                result = attempt(target)
                attempts[target] = result

                if not result["ok"]:
                    print_failure(result)
                    errors.append({
                        "case_id": record.get("case_id"),
                        "confidence": confidence_of(record),
                        "title": record.get("title"),
                        "link": url,
                        "resolved_link": target if target != url else None,
                        "attempt": position,
                        "error": result["error"],
                        "diagnostics": result.get("diagnostics"),
                    })

            if result["ok"]:
                success_url, success_link = target, url
                break

        if success_url is None:
            errors.append({
                "case_id": record.get("case_id"),
                "confidence": confidence_of(record),
                "title": record.get("title"),
                "link": urls[0],
                "error": f"All {len(urls)} candidate URL(s) failed or returned too little text.",
                "attempted_links": tried,
            })
            print("  [CASE FAILED] no URL produced article text.")
            continue

        result = attempts[success_url]
        extracted_records.append({
            "case_id": record.get("case_id"),
            "confidence": confidence_of(record),
            "title": record.get("title"),
            "publisher": record.get("publisher"),
            "incident_date": record.get("incident_date"),
            "location": record.get("location"),
            "link": success_url,
            "final_url": result.get("final_url"),
            "google_news_link": success_link if success_link != success_url else None,
            "extraction_method": result.get("method"),
            "used_fallback": success_link != urls[0],
            "attempted_links": tried,
            "text": result["text"],
        })

        print(f"  [SUCCESS] Extracted {len(result['text'])} characters from {success_url}")

    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            extracted_records,
            file,
            indent=2,
            ensure_ascii=False,
        )

    with ERROR_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            errors,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print("\n" + "=" * 45)
    print("EXTRACTION COMPLETE")
    print("=" * 45)
    print(f"Records processed: {len(records)}")
    print(f"Unique URLs attempted: {len(attempts)}")
    print(f"Google News links decoded: {sum(1 for target, _ in decoded.values() if target)}/{len(decoded)}")
    print(f"Extractions via WordPress API: {sum(1 for r in extracted_records if r['extraction_method'] == 'wordpress_api')}")
    print(f"Extractions via Wayback Machine: {sum(1 for r in extracted_records if r['extraction_method'] == 'wayback_snapshot')}")
    print(f"Successful extractions: {len(extracted_records)}")
    print(f"Extractions using a fallback URL: {sum(1 for r in extracted_records if r['used_fallback'])}")
    print(f"Failed attempts / failed cases logged: {len(errors)}")
    print(f"\nOutput: {OUTPUT_FILE}")
    print(f"Diagnostics: {ERROR_FILE}")


if __name__ == "__main__":
    main()
