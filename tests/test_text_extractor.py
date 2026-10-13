from pathlib import Path

from src.modules.link_extraction.text_extractor import extract_article

FIXTURE = Path(__file__).parent / "fixtures" / "article_jsonld.html"


def test_page_metadata_from_canonical_og_and_json_ld():
    result = extract_article(FIXTURE.read_text(encoding="utf-8"), "https://news.example.com/amp/123")
    metadata = result["metadata"]

    assert metadata["canonical_url"] == "https://news.example.com/2026/10/10/motorcyclist-killed-fresno"
    assert metadata["date_published"] == "2026-10-10T09:15:00-07:00"
    assert metadata["date_modified"] == "2026-10-10T13:40:00-07:00"
    assert metadata["authors"] == ["Jane Reporter"]
    assert metadata["site_name"] == "Example News"
    assert metadata["json_ld_found"] is True


def test_article_text_excludes_navigation():
    result = extract_article(FIXTURE.read_text(encoding="utf-8"), "https://news.example.com/x")

    assert "pronounced dead at the scene" in result["text"]
    assert "Home" not in result["text"].split()
