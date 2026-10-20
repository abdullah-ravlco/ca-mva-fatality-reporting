"""The text sent to JEV for one incident: the incident's articles, in full (the "state"). The questions
asked about it are in questions.py."""

from src.modules.severity_screening.config import JEV_MAX_INPUT_CHARS


def _articles(incident):
    return [a for a in incident.get("articles") or [] if (a.get("text") or a.get("description") or a.get("title"))]


def article_block(index, article, text):
    metadata = article.get("metadata") or {}
    return (
        f"[Article {index}]\n"
        f"Headline: {article.get('title') or ''}\n"
        f"Publisher: {article.get('publisher') or 'unknown'}\n"
        f"Published: {metadata.get('date_published') or article.get('published_at') or 'unknown'}\n"
        f"URL: {article.get('final_url') or article.get('url') or ''}\n"
        f"Text:\n{text}"
    )


def article_text(article):
    """Full text when it was read; otherwise the description the feed gave."""
    return (article.get("text") or article.get("description") or "").strip() or "(no text could be read)"


def build_user_message(incident, max_chars=JEV_MAX_INPUT_CHARS):
    """All articles of the incident, in full. max_chars > 0 shares that budget between the articles
    (only for a JEV whose context window is too small for full text)."""
    articles = _articles(incident)
    texts = [article_text(a) for a in articles]
    if max_chars and sum(len(t) for t in texts) > max_chars:
        share = max(500, max_chars // max(1, len(texts)))
        texts = [t if len(t) <= share else t[:share] + " [...]" for t in texts]
    blocks = [article_block(i, a, t) for i, (a, t) in enumerate(zip(articles, texts), start=1)]
    return (
        f"Incident {incident.get('incident_id')}: {len(blocks)} article(s) grouped as reports of the same crash.\n\n"
        + "\n\n-----\n\n".join(blocks)
    )
