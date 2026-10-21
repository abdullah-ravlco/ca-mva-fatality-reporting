
import json
from pathlib import Path

from sentence_transformers import SentenceTransformer, util

FILE = Path("raw_articles.json")

# Temporary threshold for flagging candidates, not automatic deletion.
THRESHOLD = 0.70

# Example results returned by your news-discovery step.
new_articles = [
    {
        "title": "Two people killed in Highway 99 crash",
        "snippet": "A fatal collision occurred near Howsley Road.",
        "url": "https://example.com/article-a",
        "publisher": "News A",
    },
    {
        "title": "Two dead in Sutter County Route 99 collision",
        "snippet": "The crash happened near Howsley Road.",
        "url": "https://example.com/article-b",
        "publisher": "News B",
    },
]


def article_text(article):
    """Text used to compare the meaning of articles."""
    title = article.get("title") or ""
    snippet = article.get("snippet") or article.get("description") or ""
    return f"{title}. {snippet}".strip()


# Load previously collected raw source records.
if FILE.exists():
    with FILE.open("r", encoding="utf-8") as f:
        stored_articles = json.load(f)
else:
    stored_articles = []

model = SentenceTransformer("all-MiniLM-L6-v2")

# Create embeddings for previously collected sources.
stored_texts = [article_text(a) for a in stored_articles]
if stored_texts:
    stored_embeddings = model.encode(
        stored_texts, convert_to_tensor=True
    )
else:
    stored_embeddings = None

known_urls = {a.get("url") for a in stored_articles}

for article in new_articles:
    url = article.get("url")

    # First check: exact article URL already collected?
    if not url or url in known_urls:
        print("Skipping missing or existing URL:", url)
        continue

    text = article_text(article)
    embedding = model.encode(text, convert_to_tensor=True)

    article["possible_match_url"] = None
    article["similarity_score"] = None
    article["review_needed"] = False

    # Second check: compare against existing collected sources.
    if stored_embeddings is not None:
        scores = util.cos_sim(embedding, stored_embeddings)[0]
        best_index = int(scores.argmax())
        best_score = float(scores[best_index])

        article["similarity_score"] = round(best_score, 4)

        if best_score >= THRESHOLD:
            article["possible_match_url"] = (
                stored_articles[best_index].get("url")
            )
            article["review_needed"] = True

            print(
                "POSSIBLE MATCH:",
                article["title"],
                "Score:",
                round(best_score, 4),
            )
        else:
            print("No strong semantic match:", article["title"])
    else:
        print("No previous records to compare:", article["title"])

    # Keep the new source even if it resembles another article.
    # It may provide useful independent reporting of the incident.
    stored_articles.append(article)
    known_urls.add(url)

    # Make this new article available for comparisons with
    # subsequent results in this same run.
    if stored_embeddings is None:
        stored_embeddings = embedding.unsqueeze(0)
    else:
        import torch
        stored_embeddings = torch.cat(
            [stored_embeddings, embedding.unsqueeze(0)],
            dim=0,
        )

# Persist all collected source records.
with FILE.open("w", encoding="utf-8") as f:
    json.dump(stored_articles, f, indent=2, ensure_ascii=False)

print(f"Total stored articles: {len(stored_articles)}")
