"""rarewaves / Klevu paging, and the completeness guard.

The guard exists because of a real incident on 2026-09-11: a transient short
page returned 100 of 112 products, the run reported NO errors, main.py pruned
the 12 missing entries, and the next run re-alerted every one of them as a brand
new product. Twelve false pings from one flaky HTTP response.

That failure is silent by construction — a short page looks exactly like the end
of the catalogue — so it needs a test rather than an eye.
"""

from __future__ import annotations

import pytest

from sites.rarewaves import KLEVU_PAGE, RarewavesChecker

OPTIONS = {"search": "beyblade", "currency": "SEK", "currency_symbol": "kr"}


def record(sku: str, name: str = "Beyblade X Thing", in_stock: str = "yes") -> dict:
    return {"sku": sku, "name": name, "inStock": in_stock, "salePrice": "9.99",
            "currency": "GBP", "url": f"https://www.rarewaves.com/products/{sku}-thing"}


class FakeClient:
    """Returns the given pages in order; `total` is what Klevu claims exists."""

    def __init__(self, pages: list[list[dict]], total: int | None = None):
        self.pages = list(pages)
        self.total = total
        self.calls = 0

    def post_json(self, url, json=None, headers=None):
        self.calls += 1
        records = self.pages.pop(0) if self.pages else []
        meta = {} if self.total is None else {"totalResultsFound": self.total}
        return {"queryResults": [{"records": records, "meta": meta}]}

    def get_json(self, url, headers=None):       # price_country lookups
        return {"variants": [{"price": 13400}]}


def checker(client, **overrides) -> RarewavesChecker:
    return RarewavesChecker("rarewaves", {**OPTIONS, **overrides}, client)


class TestPaging:
    def test_single_short_page_is_complete(self):
        client = FakeClient([[record(f"{i:013d}") for i in range(5)]], total=5)
        check = checker(client)
        assert len(list(check.check())) == 5
        assert check.errors == 0

    def test_two_full_pages_are_followed(self):
        pages = [[record(f"{i:013d}") for i in range(KLEVU_PAGE)],
                 [record(f"9{i:012d}") for i in range(58)]]
        check = checker(FakeClient(pages, total=KLEVU_PAGE + 58))
        assert len(list(check.check())) == KLEVU_PAGE + 58
        assert check.errors == 0


class TestCompletenessGuard:
    def test_empty_later_page_marks_the_run_incomplete(self):
        # THE INCIDENT: page 0 full, page 1 empty, Klevu says 158 exist. Before
        # the guard this returned 100 products with errors == 0, so main.py
        # pruned the rest and the next run re-alerted them as new.
        pages = [[record(f"{i:013d}") for i in range(KLEVU_PAGE)], []]
        check = checker(FakeClient(pages, total=158))
        results = list(check.check())
        assert len(results) == KLEVU_PAGE
        assert check.errors > 0, "a partial catalogue must not look like a clean run"

    def test_short_later_page_marks_the_run_incomplete(self):
        pages = [[record(f"{i:013d}") for i in range(KLEVU_PAGE)],
                 [record(f"9{i:012d}") for i in range(10)]]
        check = checker(FakeClient(pages, total=158))
        list(check.check())
        assert check.errors > 0

    def test_complete_run_is_not_flagged(self):
        pages = [[record(f"{i:013d}") for i in range(KLEVU_PAGE)],
                 [record(f"9{i:012d}") for i in range(58)]]
        check = checker(FakeClient(pages, total=158))
        list(check.check())
        assert check.errors == 0

    def test_empty_first_page_is_still_an_error(self):
        check = checker(FakeClient([[]], total=158))
        assert list(check.check()) == []
        assert check.errors > 0

    def test_missing_total_does_not_invent_an_error(self):
        # No metadata means no promise to compare against; inventing an error
        # would permanently disable pruning for this site.
        pages = [[record(f"{i:013d}") for i in range(5)]]
        check = checker(FakeClient(pages, total=None))
        list(check.check())
        assert check.errors == 0


class TestFiltering:
    def test_title_exclude_does_not_count_against_completeness(self):
        # Filtering happens AFTER the fetch, so excluded products must not make
        # a complete run look short — otherwise every run with a title_exclude
        # would permanently report itself incomplete.
        pages = [[record("0000000000001", "Beyblade Burst Old Thing"),
                  record("0000000000002", "Beyblade X New Thing")]]
        check = checker(FakeClient(pages, total=2), title_exclude=["beyblade burst"])
        results = list(check.check())
        assert len(results) == 1
        assert check.errors == 0

    def test_watchlist_only_gates_alerting(self):
        pages = [[record("0000000000001"), record("0000000000002")]]
        check = checker(FakeClient(pages, total=2), watchlist=["0000000000001"])
        results = list(check.check())
        assert [r.alertable for r in results] == [True, False]
