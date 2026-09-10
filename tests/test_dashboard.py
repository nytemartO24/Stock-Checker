"""Tests for the dashboard's redaction and the bundle classifier.

Both of these had real defects that the dashboard's own verification caught, and
both fail SILENTLY — a leaked postcode looks like a normal log line, and a
misclassified bundle just quietly removes a store from a comparison. They are
exactly the things worth pinning down with a test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_dashboard import redact  # noqa: E402
from resolve_watchlist import looks_like_bundle, tokens  # noqa: E402

WANTED = ["Sterling Wolf", "Scale Shark", "Whip Brachio", "Nether Incendio",
          "Suppress Superion", "Tread Croc"]


class TestRedaction:
    """Every one of these strings was taken from the real log."""

    def test_confirmed_location_is_stripped(self):
        line = "[se] delivery location confirmed: 'Karlskrona 371 16'"
        assert "371 16" not in redact(line)
        assert "Karlskrona" not in redact(line)

    def test_not_applied_warning_is_stripped(self):
        # The shape that actually leaked onto the page: the env-var fallback was
        # empty under cron, and no pattern covered "wanted Sweden/37116".
        line = ("[es] DELIVERY LOCATION NOT APPLIED — widget reads 'Germany', "
                "wanted Sweden/37116. Results from this market describe "
                "Amazon's guessed destination.")
        cleaned = redact(line)
        assert "37116" not in cleaned
        assert "NOT APPLIED" in cleaned          # the useful part survives
        assert "guessed destination" in cleaned

    def test_widget_reads_quoted_city(self):
        line = "[se] DELIVERY LOCATION NOT APPLIED — widget reads 'Karlskrona 371 16', wanted Sweden/37116."
        cleaned = redact(line)
        assert "Karlskrona" not in cleaned
        assert "371 16" not in cleaned

    def test_bare_postcode_either_spacing(self):
        assert "371 16" not in redact("destination 371 16 reached")
        assert "37116" not in redact("destination 37116 reached")

    def test_ordinary_text_is_untouched(self):
        line = "[amazon] se B0FCYT9DG6: unavailable no date"
        assert redact(line) == line

    def test_prices_are_not_mangled(self):
        # A five-digit run inside a price must not be eaten by the postcode
        # pattern; redaction that destroys the data is its own failure.
        assert "149.00" in redact("price kr149.00 at toysnowman")
        assert "B0H1RB48HK" in redact("asin B0H1RB48HK checked")


class TestBundleClassifier:
    def test_amazon_marketing_title_is_not_a_bundle(self):
        # THE BUG: two "and"s in one product's title classified every Amazon
        # listing as a bundle, which removed Amazon from the dashboard's
        # cross-store price comparison — the only place a scalp is visible.
        title = ("Beyblade X Sterling Wolf 3-80FB UX Starter Pack Set with "
                 "Endurance Type Top and Launcher, Authentic Takara Tomy "
                 "Battle Tops and Games for Kids")
        assert not looks_like_bundle(title, WANTED)

    def test_real_bundle_is_detected(self):
        title = ("Beyblade X Tread Croc TQ 5-50GN CX & Whip Brachio OW 5-70Nr CX "
                 "& Nether Incendio Z UX & Suppress Superion 0-70LP BX")
        assert looks_like_bundle(title, WANTED)

    def test_two_wanted_products_in_one_title_is_a_bundle(self):
        # Even without a join character, naming two tracked products is enough.
        assert looks_like_bundle("Scale Shark 4-50UF plus Sterling Wolf 3-80FB", WANTED)

    def test_single_product_is_not_a_bundle(self):
        assert not looks_like_bundle("Beyblade X Scale Shark 4-50UF UX Booster Pack",
                                     WANTED)

    def test_vs_collab_is_a_bundle(self):
        title = "Beyblade X and Marvel Collab, Miles Morales 1-60GN vs. Green Goblin 9-80HT"
        assert looks_like_bundle(title, WANTED)


class TestTokens:
    def test_word_order_does_not_matter(self):
        # "Shark Scale" must match a listing titled "Scale Shark 4-50UF".
        assert tokens("Shark Scale") <= tokens("Beyblade X - Booster: Scale Shark 4-50UF")

    def test_brand_words_do_not_identify_a_product(self):
        assert tokens("Beyblade X") == set()
