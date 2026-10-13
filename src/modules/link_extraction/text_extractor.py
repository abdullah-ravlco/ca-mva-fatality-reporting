import trafilatura
from bs4 import BeautifulSoup


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