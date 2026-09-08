import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def popsplanet_payload() -> dict:
    """A real (trimmed) products.json capture: one in-stock, two sold out."""
    return json.loads((FIXTURES / "shopify_popsplanet.json").read_text(encoding="utf-8"))


class FakeClient:
    """Stands in for PoliteClient. Records the URLs asked for so tests can
    assert on request volume, which is a correctness property here — the
    polite-scraper principle is not decoration."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.requested: list[str] = []

    def get_json(self, url: str) -> dict:
        self.requested.append(url)
        return self.pages.pop(0) if self.pages else {"products": []}


@pytest.fixture
def fake_client():
    return FakeClient
