import pytest

from src.modules.contact_enrichment import config as enrichment_config


@pytest.fixture(autouse=True)
def no_google_sheet(request, monkeypatch):
    """The offline tests never reach the real Google Sheet, even when .env has the web app URL and token.
    Only tests/test_google_sheet_live.py talks to it."""
    if request.module.__name__.endswith("test_google_sheet_live"):
        return
    monkeypatch.setattr(enrichment_config, "GOOGLE_SCRIPT_URL", "")
    monkeypatch.setattr(enrichment_config, "GOOGLE_SCRIPT_TOKEN", "")
