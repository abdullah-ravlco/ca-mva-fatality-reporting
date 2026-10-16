"""Which sources are not trusted for this purpose.

Dropped (reason in brackets):
  law_firm      - personal-injury marketing sites and blogs; they rewrite news to sell services
  social_media  - Facebook, Instagram, X, TikTok, YouTube, Reddit, Nextdoor, Citizen app posts
  aggregator    - NewsBreak, which republishes stories without a verifiable origin

Kept on purpose: Yahoo, AOL and MSN (syndicated copies of real outlets), Hoodline and
local aggregators that cite their source.
"""

import re
from urllib.parse import urlsplit

LAW_FIRM_NAME_RE = re.compile(
    r"\b(?:law|lawyers?|attorneys?|legal|injury|firm|llp|apc|plc)\b", re.IGNORECASE
)
LAW_FIRM_HOST_RE = re.compile(
    r"attorney|lawyer|injury|legal|lawfirm|lawgroup|lawoffice|accidentlaw|(?:^|[.-])law(?:[.-]|apc|pc|group|firm|\.)|law\.com$",
    re.IGNORECASE,
)
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "youtu.be",
    "reddit.com", "nextdoor.com", "threads.net", "citizen.com",
)
SOCIAL_NAMES = {"facebook", "instagram", "twitter", "x", "tiktok", "youtube", "reddit", "nextdoor", "citizen app"}
AGGREGATOR_HOSTS = ("newsbreak.com",)
AGGREGATOR_NAME_RE = re.compile(r"^newsbreak\b", re.IGNORECASE)


def host_of(url):
    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def low_value_reason(source_name, url):
    """"law_firm" | "social_media" | "aggregator", or None when the source is accepted.

    Google News links carry no publisher host, so the source name decides for them.
    """
    host = host_of(url)
    name = (source_name or "").strip()

    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS) or name.lower() in SOCIAL_NAMES:
        return "social_media"
    if any(host == h or host.endswith("." + h) for h in AGGREGATOR_HOSTS) or AGGREGATOR_NAME_RE.search(name):
        return "aggregator"
    if LAW_FIRM_NAME_RE.search(name) or (host != "news.google.com" and LAW_FIRM_HOST_RE.search(host)):
        return "law_firm"
    # Google News items from a law-firm site name the site itself ("contreras-law.com").
    if "." in name and LAW_FIRM_HOST_RE.search(name):
        return "law_firm"
    return None
