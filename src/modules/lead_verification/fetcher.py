"""Open article URLs with step 2's fetcher (Google News decoding, page fetch,
WordPress and Wayback fallbacks). Results are cached between runs, so a URL
is fetched at most once."""

import json

from src.modules.link_extraction import pipeline as _link
from src.modules.lead_verification.config import FETCH_CACHE_FILE, MAX_CACHED_TEXT_CHARS


class Fetcher:
    def __init__(self):
        self.decoded = {}
        self.cache = {}
        if FETCH_CACHE_FILE.exists():
            try:
                self.cache = json.loads(FETCH_CACHE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}

    def fetch(self, url):
        """{"ok", "url", "final_url", "text" | "error"} for one URL (never raises)."""
        target, decode_error = _link.resolve_url(url, self.decoded)
        if target is None:
            return {"ok": False, "url": url, "error": decode_error}

        if target in self.cache:
            return dict(self.cache[target], url=target, cached=True)

        try:
            result = _link.attempt(target)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        entry = {
            "ok": bool(result.get("ok")),
            "final_url": result.get("final_url"),
            "method": result.get("method"),
        }
        if entry["ok"]:
            entry["text"] = (result.get("text") or "")[:MAX_CACHED_TEXT_CHARS]
        else:
            entry["error"] = result.get("error")
        self.cache[target] = entry
        return dict(entry, url=target, cached=False)

    def save(self):
        FETCH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FETCH_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FETCH_CACHE_FILE)
