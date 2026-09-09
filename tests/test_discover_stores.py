"""Tests for the store-discovery tool.

Every case here is a real failure this script produced against live sites
before it was fixed, which is why they are worth keeping: the tool's whole
job is to tell "this shop has none" apart from "we asked wrong", and every
one of these bugs collapsed that distinction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from discover_stores import (  # noqa: E402
    CONTROL_TERM, judge, link_set, read_jsonld, registrable, unwrap, usable,
)


class TestJudge:
    """The control query: did the shop's search actually listen?"""

    def test_identical_results_for_nonsense_is_not_a_match(self):
        # hlj.com's /?s=beyblade&post_type=product returned 714 "matches"
        # because it is not WooCommerce and the query was simply ignored.
        links = {f"/product/{n}" for n in range(30)}
        hit = {"links": links, "mentions": 40}
        control = {"links": set(links), "mentions": 40}
        assert judge(hit, control).startswith("query IGNORED")

    def test_disjoint_results_is_a_match(self):
        hit = {"links": {"/product/beyblade-x"}, "mentions": 40}
        control = {"links": set(), "mentions": 2}
        assert judge(hit, control).startswith("REAL")

    def test_term_no_more_common_than_nonsense_is_not_a_match(self):
        # A template echoing whatever it was handed proves nothing.
        hit = {"links": {"/product/a"}, "mentions": 3}
        control = {"links": {"/product/b"}, "mentions": 5}
        assert judge(hit, control).startswith("query IGNORED")

    def test_partial_overlap_still_counts_as_real(self):
        # A real Shopify search page still carries its nav and "featured"
        # tiles, so some overlap with any other page is normal. beywarehouse
        # measured 0.21 while genuinely searching.
        hit = {"links": {f"/products/{n}" for n in range(20)}, "mentions": 500}
        control = {"links": {"/products/1", "/products/2"}, "mentions": 10}
        assert judge(hit, control).startswith("REAL")

    def test_empty_control_does_not_veto(self):
        hit = {"links": {"/product/a"}, "mentions": 20}
        assert judge(hit, {}).startswith("REAL")


class TestLinkSet:
    def test_finds_marker_segment_urls(self):
        body = '<a href="/product/beyblade-x-scale-shark">x</a>'
        assert link_set(body, "beyblade") == {"/product/beyblade-x-scale-shark"}

    def test_finds_products_with_no_marker_segment(self):
        # jollyroom (Litium) files products at /<category>/<slug> with nothing
        # to match on, so the marker pattern alone reported "no results" for a
        # page mentioning beyblade 413 times.
        body = '<a href="/leksaker/spel/beyblade-x-startset">x</a>'
        assert link_set(body, "beyblade") == {"/leksaker/spel/beyblade-x-startset"}

    def test_ignores_search_and_pagination_links(self):
        body = ('<a href="/?s=beyblade&post_type=product">search</a>'
                '<a href="/sok?q=beyblade">sok</a>'
                '<a href="/catalogsearch/result/?q=beyblade">m</a>'
                '<a href="/img/beyblade.png">img</a>')
        assert link_set(body, "beyblade") == set()

    def test_query_strings_do_not_split_one_product_into_many(self):
        body = ('<a href="/product/scale-shark?variant=1">a</a>'
                '<a href="/product/scale-shark?variant=2">b</a>')
        assert link_set(body, "beyblade") == {"/product/scale-shark"}

    def test_control_term_finds_nothing_in_a_real_catalogue(self):
        body = '<a href="/product/beyblade-x-scale-shark">x</a>'
        assert link_set(body, CONTROL_TERM) == {"/product/beyblade-x-scale-shark"}
        # ^ the marker pattern still matches; what distinguishes the control
        # run is that the SHOP returns different links, not that we parse
        # differently. Guards against "optimising" the term into the parser.


class TestUnwrap:
    def test_duckduckgo_redirect(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.rarewaves.com%2Fp&rut=x"
        assert unwrap(href) == "https://www.rarewaves.com/p"

    def test_bing_base64_redirect(self):
        import base64
        target = "https://www.probems.be/en/BEYBLADE.html"
        blob = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        assert unwrap(f"https://www.bing.com/ck/a?!&&u=a1{blob}") == target

    def test_undecodable_bing_payload_is_dropped_not_crashed(self):
        # A payload that decodes to something that is not a URL used to reach
        # `url.split('/')[2]` and take the whole scan down.
        assert unwrap("https://www.bing.com/ck/a?u=a1bm90YXVybA") is None

    def test_engine_chrome_is_not_a_result(self):
        assert unwrap("https://github.com/MarginaliaSearch/x") is None
        assert unwrap("https://en.wikipedia.org/wiki/Beyblade") is None

    def test_plain_result_passes_through(self):
        assert unwrap("https://gameshop.se/product/1") == "https://gameshop.se/product/1"

    def test_html_entities_in_href(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.se%2Fp&amp;rut=1"
        assert unwrap(href) == "https://a.se/p"

    def test_non_http_schemes_rejected(self):
        assert usable("javascript:void(0)") is None
        assert usable("mailto:a@b.se") is None


class TestRegistrable:
    def test_strips_www_and_subdomains(self):
        assert registrable("www.rarewaves.com") == "rarewaves.com"
        assert registrable("shop.beyblade.com") == "beyblade.com"

    def test_multi_label_suffix(self):
        assert registrable("www.magicmadhouse.co.uk") == "magicmadhouse.co.uk"

    def test_bare_domain_unchanged(self):
        assert registrable("ginza.se") == "ginza.se"


class TestJsonLd:
    def test_reads_gtin_from_nested_graph(self):
        # rarewaves ships this shape; the barcode is the one cross-retailer
        # identity that exists, so finding it whatever the nesting matters.
        body = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "Organization", "name": "shop"},
                    {"@type": "Product", "name": "Scale Shark 4-50UF",
                     "gtin13": "5010996385222",
                     "offers": {"@type": "Offer", "price": "134.00",
                                "priceCurrency": "SEK"}}]}
        </script>"""
        products = read_jsonld(body)
        assert len(products) == 1
        assert products[0]["gtin13"] == "5010996385222"

    def test_malformed_block_does_not_kill_the_page(self):
        body = ('<script type="application/ld+json">{not json</script>'
                '<script type="application/ld+json">'
                '{"@type":"Product","name":"ok"}</script>')
        assert [p["name"] for p in read_jsonld(body)] == ["ok"]

    def test_no_structured_data(self):
        assert read_jsonld("<html><body>nothing</body></html>") == []
