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


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """The offline tests never reach the real Postgres, even when .env has DATABASE_URL: the stages would
    copy their test output over the real documents and read the real ones. Tests that need a database set
    their own (tests/test_postgres_*.py)."""
    for name in ("DATABASE_URL", "PGHOST", "PGDATABASE", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
