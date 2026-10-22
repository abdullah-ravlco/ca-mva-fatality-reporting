"""Group articles into incidents by the semantic similarity of their text.

Each article is embedded (headline + the start of the story) and articles are
clustered by average-linkage agglomerative clustering on cosine similarity:
two clusters merge only when their members are similar *on average*, so one
article that resembles two different crashes cannot chain them together.

Embeddings alone cannot tell two same-day crashes in one city apart when the
stories read alike, so known facts still veto a merge (case_grouping.conflicts:
crash dates more than a day apart, different case numbers, different places or
counties) and make it harder (a one-day date gap, or victims whose ages and
types do not overlap need a higher similarity).
"""

import hashlib
import logging
import os
import re
from collections import defaultdict

import numpy as np

from src.modules.source_collection.case_grouping import conflicts, facts

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small").strip()
EMBED_TEXT_CHARS = 3000
EMBED_BATCH = 64


def _float(name, default):
    value = (os.getenv(name) or "").strip()
    return float(value) if value else default


# Average cosine similarity two clusters need to merge (tune from the logged pair scores).
SAME_DAY_THRESHOLD = _float("GROUPING_SAME_DAY_THRESHOLD", 0.72)
NEXT_DAY_EXTRA = _float("GROUPING_NEXT_DAY_EXTRA", 0.08)   # crash dates one day apart
UNDATED_EXTRA = _float("GROUPING_UNDATED_EXTRA", 0.08)     # a crash date is missing on either side
VICTIM_MISMATCH_EXTRA = _float("GROUPING_VICTIM_MISMATCH_EXTRA", 0.10)


TITLE_WEIGHT = _float("GROUPING_TITLE_WEIGHT", 0.5)   # share of the score from the headline embedding
BOILERPLATE_CHARS = 500

# Headlines that cover several events (or a pile-up of crashes) say nothing about ONE crash,
# so they must not bridge two incidents.
ROUNDUP_TITLE_RE = re.compile(
    r"\b(shootings?|stabbings?|homicides?|murders?)\b.*\b(crash|collision|wreck)|"
    r"\b(crash|collision|wreck)\w*\b.*\b(shootings?|stabbings?|homicides?)\b|"
    r"\b(\d+(st|nd|rd|th)|second|third|fourth|fifth)\s+(fatal|deadly)?\s*(crash|collision)|"
    r"\b(crashes|collisions)\b.*\b(across|in a day|in one day|in under|within hours)\b|"
    r"\b(two|three|four|five|several|multiple)\s+(separate\s+)?(fatal|deadly)\s+(crashes|collisions)\b",
    re.I,
)


def is_roundup(article):
    return bool(ROUNDUP_TITLE_RE.search(article.get("title") or ""))


def boilerplate_text_ids(articles):
    """Articles whose story text is shared verbatim with an article of a different headline:
    a page fetched wrongly (site-wide text), not the story."""
    by_text = defaultdict(list)
    for index, article in enumerate(articles):
        text = (article.get("text") or "").strip()
        if text:
            by_text[hashlib.sha1(text[:BOILERPLATE_CHARS].encode()).hexdigest()].append(index)
    bad = set()
    for indices in by_text.values():
        if len({(articles[i].get("title") or "").strip().lower() for i in indices}) > 1:
            bad.update(indices)
    return bad


def article_text(article):
    parts = [article.get("title"), (article.get("text") or "")[:EMBED_TEXT_CHARS]]
    return "\n".join(p for p in parts if p) or (article.get("description") or article.get("url") or "")


def _embed(client, texts):
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts[start:start + EMBED_BATCH])
        vectors.extend(item.embedding for item in response.data)
    matrix = np.asarray(vectors, dtype=np.float32)
    return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)


def embed_articles(client, articles):
    """(title vectors, story vectors). Articles without a usable story embed their headline twice."""
    titles = _embed(client, [a.get("title") or a.get("description") or a.get("url") or "" for a in articles])
    stories = _embed(client, [article_text(a) for a in articles])
    return titles, stories


ROAD_MENTION_RE = re.compile(
    r"\b(?:I|US|SR|CA|Route|Highway|Hwy|Interstate|State Route|U\.S\.|Freeway)[-.\s]*(\d{1,3})\b|\b(\d{1,3})\s+Freeway\b", re.I
)


def road_numbers(article):
    text = " ".join(filter(None, (article.get("title"), (article.get("text") or "")[:EMBED_TEXT_CHARS])))
    return {g for m in ROAD_MENTION_RE.finditer(text) for g in m.groups() if g}


def different_roads(a, b):
    """Both name a numbered road and neither story mentions the other's road (I-280 vs I-680)."""
    return bool(a["road"] and b["road"] and a["road"] != b["road"]
                and a["road"] not in b["road_numbers"] and b["road"] not in a["road_numbers"])


def required_similarity(a, b):
    """Similarity a pair of articles needs, or None when known facts rule it out."""
    if conflicts(a, b) or different_roads(a, b):
        return None
    needed = SAME_DAY_THRESHOLD
    if a["date"] and b["date"]:
        if a["date"].date() != b["date"].date():
            needed += NEXT_DAY_EXTRA
    else:
        needed += UNDATED_EXTRA
    if a["ages"] and b["ages"] and not a["ages"] & b["ages"] and a["victims"] and b["victims"] and not a["victims"] & b["victims"]:
        needed += VICTIM_MISMATCH_EXTRA
    return needed


def cluster(similarity, needed):
    """Average-linkage clustering. `needed[i][j]` is NaN for pairs that must not merge.

    Two clusters merge when the average of (similarity - needed) over their cross pairs
    is >= 0 and no cross pair is vetoed. Returns the clusters as lists of indices."""
    n = len(similarity)
    vetoed = np.isnan(needed)
    margin_sum = np.where(vetoed, 0.0, similarity - needed).astype(np.float64)
    veto_count = vetoed.astype(np.int64)
    size = np.ones(n)
    members = {i: [i] for i in range(n)}
    active = np.ones(n, dtype=bool)

    while True:
        average = margin_sum / np.outer(size, size)
        average[(veto_count > 0) | ~active[:, None] | ~active[None, :]] = -np.inf
        np.fill_diagonal(average, -np.inf)
        i, j = divmod(int(average.argmax()), n)
        if average[i, j] < 0:
            return [sorted(members[k]) for k in members]
        margin_sum[i, :] += margin_sum[j, :]
        margin_sum[:, i] += margin_sum[:, j]
        veto_count[i, :] += veto_count[j, :]
        veto_count[:, i] += veto_count[:, j]
        size[i] += size[j]
        members[i] += members.pop(j)
        active[j] = False


def group_with_reasons(articles, client=None, vectors=None):
    """Return [(members, rationale)], like case_grouping.group_with_reasons.

    `vectors` is the (title vectors, story vectors) pair from embed_articles."""
    if not articles:
        return []
    bad_text = boilerplate_text_ids(articles)
    if bad_text:
        logger.warning("%s articles share identical text with a different headline (bad extraction); "
                       "grouping them on their headline only", len(bad_text))
    # What grouping sees: no story text for wrongly fetched pages.
    seen = [dict(a, text="") if i in bad_text else a for i, a in enumerate(articles)]
    if vectors is None:
        if client is None:
            from src.core.llm_client import build_llm
            client = build_llm()
        vectors = embed_articles(client, seen)
    title_vectors, story_vectors = vectors
    story_vectors = np.array(story_vectors)
    for i in bad_text:
        story_vectors[i] = title_vectors[i]

    all_facts = [dict(facts(a), road_numbers=road_numbers(a)) for a in seen]
    n = len(articles)
    similarity = TITLE_WEIGHT * (title_vectors @ title_vectors.T) + (1 - TITLE_WEIGHT) * (story_vectors @ story_vectors.T)
    roundups = {i for i, a in enumerate(articles) if is_roundup(a)}
    needed = np.full((n, n), np.nan)
    for i in range(n):
        for j in range(i + 1, n):
            if (i in roundups) != (j in roundups):
                continue  # a roundup never bridges to a single-crash story
            threshold = required_similarity(all_facts[i], all_facts[j])
            if threshold is not None:
                needed[i, j] = needed[j, i] = threshold

    results = []
    for indices in cluster(similarity, needed):
        indices = sorted(indices)
        members = []
        for i in indices:
            member = articles[i]
            if i in roundups:
                member = dict(member, multi_incident_roundup=True)
            members.append(member)
        rationale = []
        for position, i in enumerate(indices[1:], start=1):
            best = max(indices[:position], key=lambda k: similarity[i, k])
            rationale.append({
                "joined": articles[i].get("url"),
                "matched": articles[best].get("url"),
                "reason": f"semantic similarity {similarity[i, best]:.2f}",
            })
        results.append((members, rationale))
    return results
