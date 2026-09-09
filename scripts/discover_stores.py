#!/usr/bin/env python3
"""Find retailers that stock a product — by barcode, by name, and by asking.

WHY THIS EXISTS: a US-indexed web search answers "who sells EAN
5010996385222" with Belgian, French and Maltese shops and not one Swedish
one, which is the opposite of useful here. Three different mechanisms fix
that, and they are separate because they fail separately:

  1. `search`  — the same query put to several engines under several REGION
     settings. The region parameter is the entire point: an engine's Swedish
     index and its US index return different shops for identical text.
  2. `probe`   — ask each candidate shop's OWN search page for "beyblade".
     THIS IS THE HIGH-YIELD ONE and no search engine can substitute for it:
     a small shop's product pages are often not indexed at all, yet its
     internal search answers instantly. It is also the only method that
     works for a shop nobody has linked to.
  3. `verify`  — fetch a candidate page and read what it actually says:
     JSON-LD `gtin13` (the barcode, proving product identity), price,
     availability, and which e-commerce platform it runs on. The platform is
     the cost estimate for adding it — Shopify means one JSON request,
     "unknown" means an afternoon.

It REPORTS and does not decide, the same rule `audit_store.py` follows: a
domain appearing here is a candidate, not a store worth adding. Every result
carries how it was found so a weak signal can be told from a strong one.

    python scripts/discover_stores.py search --ean 5010996385222
    python scripts/discover_stores.py probe --domains config/candidate_stores.txt
    python scripts/discover_stores.py verify --urls found.txt --ean 5010996385222

Read-only against every site it touches. Writes only the report files it is
asked for (--json / --markdown).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.http import PoliteClient  # noqa: E402

logger = logging.getLogger("discover")

# ---------------------------------------------------------------------------
# Engines.
#
# Chosen for INDEX INDEPENDENCE, not popularity: three front-ends onto the
# same crawl would triple the request count and add nothing. DuckDuckGo
# (Bing-derived), Mojeek (its own crawler) and Marginalia (deliberately
# indexes small independent sites, which is exactly the shape of a shop we
# are missing) genuinely disagree with each other.
#
# `{q}` is url-encoded query, `{region}` the engine's own region token.
# Engines that block us are reported as blocked rather than silently
# contributing zero — an engine returning nothing and an engine refusing to
# answer are completely different facts.
# ---------------------------------------------------------------------------

ENGINES: dict[str, dict] = {
    "bing": {
        # Measured the highest yield of anything tried, but ONLY because every
        # result is wrapped in /ck/a?u=a1<base64url>. Read the raw hrefs and
        # Bing looks like it returned nothing at all.
        "url": "https://www.bing.com/search?q={q}&cc={region}&count=30",
        "regions": {"se": "SE", "world": "US", "de": "DE", "uk": "GB"},
        "redirects_only": True,
        "blocked_markers": ("ref=/challenge", "captcha"),
    },
    "ddg-lite": {
        # The lite endpoint challenges far less often than the main HTML one,
        # and its markup is trivial. It still rate-limits fast — measured
        # 2026-09-09: fine on the first query, HTTP 202 + bot challenge on the
        # second when they were seconds apart. Hence the slow default pacing.
        "url": "https://lite.duckduckgo.com/lite/?q={q}&kl={region}",
        "regions": {"se": "se-sv", "world": "wt-wt", "de": "de-de", "uk": "uk-en"},
        "redirects_only": True,
        "blocked_markers": ("anomaly", "unfortunately, bots"),
    },
    "mojeek": {
        # Its own crawler, so it genuinely disagrees with the Bing-derived
        # indexes — worth keeping despite answering 403 to perhaps half of
        # what we ask. Quoted queries seem to draw the 403 more often.
        "url": "https://www.mojeek.com/search?q={q}&arc={region}",
        "regions": {"se": "se", "world": "none", "de": "de", "uk": "gb"},
        "redirects_only": False,
        "blocked_markers": (),
    },
    "marginalia": {
        # Deliberately indexes small independent sites — the exact shape of
        # shop we are missing, and the reason it earns a slot despite having
        # no region support and a tiny index. NOTE the host: the old
        # `old-search.` one answers 200 with a ~1KB stub, which reads as
        # "nothing found" rather than as the dead endpoint it is.
        "url": "https://search.marginalia.nu/search?query={q}",
        "regions": {"world": ""},
        "redirects_only": False,
        "blocked_markers": (),
    },
}

# Engines block when hurried, and a blocked engine is a silent zero. This is
# the one place in the project where being slow is the cheaper option: the
# whole scan is a handful of queries run by hand, not a per-run cost.
ENGINE_DELAY = (5.0, 11.0)

# Not shops. Splitting "never a lead" from "a lead about other shops" matters:
# an aggregator page is worthless as a retailer and valuable as a source, so
# it is reported in its own group rather than dropped.
JUNK_HOSTS = re.compile(
    r"(duckduckgo|mojeek|marginalia|google|bing|yahoo|yandex|baidu|ecosia|qwant"
    r"|facebook|instagram|twitter|x\.com|tiktok|youtube|pinterest|reddit|linkedin"
    r"|wikipedia|wikia|fandom|archive\.org|irs\.gov|scribd|issuu|slideshare"
    r"|blogspot|wordpress\.com|medium\.com|quora|answers"
    # Engine chrome, all measured in real output rather than guessed: these
    # are the links an engine puts on its OWN results page.
    r"|buttondown\.email|mastodon\.social|creativecommons\.org|github\.com"
    r"|ip2location\.com|marginalia-search\.com|torproject\.org|gitlab\.com"
    r"|patreon\.com|ko-fi\.com|opencollective\.com|microsoft\.com|msn\.com)\.", re.I)

AGGREGATORS = re.compile(
    r"(prisjakt|pricerunner|pricespy|idealo|geizhals|kelkoo|shopalike|priceapi"
    r"|barcodelookup|ean-search|upcitemdb|eandata|brocade\.io|go-upc|buycott"
    r"|barcode-list|gtinsearch|productdb)", re.I)

MARKETPLACES = re.compile(
    r"(amazon\.|ebay\.|aliexpress|alibaba|etsy|walmart|target\.com|fruugo"
    r"|cdiscount|allegro|bol\.com|rakuten|wish\.com|temu|shein|catch\.com)", re.I)

# GEOGRAPHY IS A REQUIREMENT, NOT A GROUPING. The order below is delivered
# cost to Sweden, and it decides what is worth reading at all: a US shop's
# freight and customs turn a cheap top into an expensive one, so finding one is
# not a result. Ranked buckets rather than a boolean, because "cheap enough" is
# a gradient — a German shop beats a Portuguese one on freight alone.
REGION_TLDS: dict[str, tuple[str, ...]] = {
    "se": (".se",),
    # Neighbours: cheap freight, often the same carriers.
    "nordic": (".dk", ".fi", ".no", ".ee", ".lv", ".lt"),
    # Large, wealthy, well-connected EU. The priority target — and the gap this
    # tool had, because nothing ever asked for a German shop by name.
    "eu-core": (".de", ".nl", ".be", ".at", ".lu", ".fr"),
    # EU, so no customs, but further and slower.
    "eu-other": (".it", ".es", ".pl", ".cz", ".ie", ".pt", ".sk", ".si",
                 ".hu", ".gr", ".ro", ".bg", ".hr", ".mt", ".cy"),
}

# Outside the EU: customs, import VAT and freight all count against these. The
# UK is here deliberately — post-Brexit it is a third country like any other.
SKIP_TLDS = (".uk", ".co.uk", ".us", ".jp", ".co.jp", ".au", ".com.au", ".ca",
             ".cn", ".ru", ".tr", ".ae", ".in", ".br", ".mx", ".za", ".sg")
SKIP_DOMAINS = re.compile(
    r"(walmart|target\.com|bigbadtoystore|entertainmentearth|beywarehouse"
    r"|beysandbricks|beyblade-toys|troveofcollectibles|raptorgames|hlj\.com"
    r"|amiami|plazajapan|solarisjapan|nin-nin-game|zavvi|magicmadhouse"
    r"|thetoyshop|smythstoys|rarewaves|staractionfigures|eclipse-gaming"
    r"|hotukdeals|shop\.beyblade|takaratomy|otakume)", re.I)

# Swedish-facing shops that do not live on a .se domain. TLD alone would file
# coolshop.se's sibling coolshop.com and boozt.com as foreign, which is wrong
# in the only way that matters: they deliver here in SEK.
SE_FACING = re.compile(
    r"(boozt|coolshop|lekmer|jollyroom|cdon|webhallen|inet\.se|elgiganten"
    r"|power\.se|netonnet|adlibris|bokus|amazon\.se|apotea|babyland)", re.I)

# Where a .com/.eu/.net shop actually trades cannot be read off the domain, so
# these are neither promoted nor discarded — they are reported as needing a
# look. A rule that guessed would have thrown away probems.be.
REGION_ORDER = ("se", "nordic", "eu-core", "eu-other", "unknown", "skip")

# Multi-label public suffixes we actually meet. A full PSL is a dependency for
# no gain at this scale, but naive last-two-labels turns co.uk into "co.uk".
MULTI_SUFFIX = frozenset({
    "co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.jp", "com.br",
    "co.za", "com.mt", "com.tr", "com.mx", "co.kr", "com.sg", "com.hk",
})

# Search paths shops actually use, cheapest signal first. Every one of these
# is a real platform's default: WooCommerce, Shopify, Magento, and the
# Swedish-market platforms (Jetshop/Norce, Litium, Abicart, Quickbutik) whose
# default is a Swedish word — a scan that only tries English `/search` misses
# a large part of the Swedish market outright.
SEARCH_PATHS = (
    "/search?q={q}",
    "/search?type=product&q={q}",
    "/?s={q}&post_type=product",
    "/?s={q}",
    # German. Shopware is the dominant DE webshop platform and its search
    # parameter is `sSearch`, which nothing else uses — a scan without it reads
    # a large part of the German market as "no results". `/suche` is the plain
    # German word and just as common.
    "/search?sSearch={q}",
    "/suche?q={q}",
    "/suche?sSearch={q}",
    "/suche/{q}",
    # Dutch and French.
    "/zoeken?q={q}",
    "/zoeken?query={q}",
    "/recherche?q={q}",
    "/recherche?controller=search&s={q}",
    # SAP Commerce / Hybris, which the big chains run.
    "/search?text={q}",
    # Swedish.
    "/sok?q={q}",
    "/sok/?q={q}",
    "/sok?query={q}",
    "/catalogsearch/result/?q={q}",
    "/search/{q}",
)

# A shop saying "nothing found", in the two languages this project meets. Used
# only to explain a zero — never to declare a hit, because the absence of
# these phrases proves nothing.
EMPTY_MARKERS = (
    "inga resultat", "hittade inga", "inga produkter", "inga träffar",
    "no results", "no products were found", "no products found",
    "0 products", "0 produkter", "nothing found", "sorry, no",
)

PRODUCT_LINK = re.compile(
    r"""href=["']([^"']*/(?:products?|produkt(?:er)?|p|item|vara|artikel|dp)/[^"'#?]+)""",
    re.I)

# Second, platform-independent signal: a link whose own URL contains the term.
# Litium and PrestaShop shops put products at /<category>/<slug> with no marker
# segment, so the pattern above cannot see them — but the slug still says
# "beyblade". Search and filter URLs are excluded or the results page's own
# pagination and facet links would count as products.
ANY_LINK = re.compile(r"""href=["']([^"'\s]+)["']""", re.I)
NOT_A_PRODUCT = re.compile(r"([?&](s|q|query|search|page|p|sort|filter)=|/search|/sok|"
                           r"/catalogsearch|/cart|/checkout|/wp-json|\.(css|js|jpg|png|webp|svg))",
                           re.I)

PLATFORMS = (
    # (label, marker) — first match wins, so order by specificity.
    ("shopify", "cdn.shopify.com"),
    ("shopify", "Shopify.shop"),
    ("woocommerce", "woocommerce"),
    ("magento", "Magento"),
    ("magento", "/static/version"),
    ("prestashop", "prestashop"),
    ("bigcommerce", "bigcommerce"),
    ("squarespace", "squarespace"),
    ("wix", "wixstatic"),
    ("apptus", "Apptus"),
    ("jetshop/norce", "jetshop"),
    ("litium", "litium"),
    ("abicart", "abicart"),
    ("quickbutik", "quickbutik"),
    ("starweb", "starweb"),
    ("centra", "centra.com"),
)

# The two redirect wrappers we know how to open. Used as a filter, not just a
# decoder — see `redirects_only` above.
REDIRECT = re.compile(r"(uddg=|/ck/a[?])", re.I)

# A word no catalogue contains. The control query: a search path that returns
# the same thing for this as for "beyblade" is not searching.
CONTROL_TERM = "qzzxwvk"

# OpenStreetMap, queried through Overpass, is the store DATABASE this project
# was missing. Every mapped toy/game/hobby shop carrying a `website` tag, per
# country, free and with no key or bot wall — measured 2026-09-09: 609 distinct
# sites in one DE/AT/NL/CZ box, none of which any search engine had surfaced.
#
# Why it beats the alternatives, all of which were tried:
#   * Price aggregators (idealo.de, geizhals.de, prisjakt, pricespy,
#     ledenicheur) have HIGHER yield per request — a product page lists every
#     retailer with an offer — but all answer 403 to plain HTTP from BOTH the
#     dev machine and the VPS. They are DataDome/Cloudflare walls, not IP
#     reputation, so they need the browser transport.
#   * Search engines cannot be steered by country at all: Bing silently ignores
#     a bare-TLD `site:.de`, returning the identical result set.
# The trade is precision: OSM lists SHOPS, not stockists, so its output is
# input to `probe` rather than an answer.
# Overpass answers JSON, and the shared client's default Accept advertises
# HTML first. Seen once as a bare 406 Not Acceptable, so it is stated
# explicitly rather than left to a default that has no reason to be right.
OVERPASS_HEADERS = {"Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded"}

OVERPASS_HOSTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Retail categories that plausibly stock a Beyblade. `department_store` is
# deliberately absent: it adds supermarket chains by the hundred and almost
# none of them run a webshop that lists individual toys.
OSM_SHOP_TYPES = "toys|games|hobby|model|video_games"

# BOUNDING BOXES, not `area["ISO3166-1"=...]`. The area form is the obvious way
# to write this and it TIMES OUT: measured 2026-09-09, BE and DK returned but
# DE, NL, AT, FI and FR all came back 504 Gateway Timeout, because resolving a
# national boundary relation and testing every shop against it is far more work
# than a coordinate comparison. A box answers the same question in seconds.
#
# The cost is that a box crosses borders — a German box catches Swiss and Czech
# shops. That is handled downstream by `region_of`, which files them by TLD, so
# the imprecision changes which bucket a shop lands in and never whether it is
# found. (south, west, north, east)
COUNTRY_BOXES: dict[str, tuple[float, float, float, float]] = {
    "DE": (47.27, 5.87, 55.06, 15.04),
    "NL": (50.75, 3.36, 53.56, 7.23),
    "BE": (49.49, 2.54, 51.51, 6.41),
    "AT": (46.37, 9.53, 49.02, 17.16),
    "DK": (54.56, 8.07, 57.75, 15.20),
    "FI": (59.80, 20.60, 70.09, 31.59),
    "FR": (41.33, -5.14, 51.09, 9.56),
    "IT": (36.60, 6.60, 47.10, 18.50),
    "ES": (36.00, -9.30, 43.80, 3.40),
    "PL": (49.00, 14.10, 54.90, 24.20),
    "CZ": (48.55, 12.09, 51.06, 18.86),
}

# ---------------------------------------------------------------------------
# Price aggregators, read with a browser.
#
# THE HIGHEST-YIELD SOURCE AVAILABLE, and the reason is structural: one product
# page lists every retailer that has the product in stock right now, which is
# the exact question this script exists to ask. A search engine can only tell
# you a page mentioning the product exists somewhere.
#
# All of them answer 403 to plain HTTP from BOTH the dev machine and the VPS —
# DataDome/Cloudflare fingerprinting, not IP reputation — so they need
# core/browser.py. Measured 2026-09-09: idealo 558KB and prisjakt 985KB through
# the browser, versus 3.9KB and 5.8KB of refusal over httpx.
#
# `shop_attr` is where the retailer's identity lives. On idealo it is
# `data-shop-name`, and the value IS USUALLY THE DOMAIN ("galaxus.de",
# "kaufland.de", "otto.de (Marktplatzhändler)"), which is why no redirect
# following is needed — there are no /relocate links on the offer list at all.
#
# SEARCH BY NAME, NOT BY BARCODE. Measured on idealo: the name query returns
# the product, the EAN query returns NOTHING. Aggregators index manufacturer
# titles, not barcodes.
AGGREGATORS_BROWSER: dict[str, dict] = {
    "idealo.de": {
        "search": "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={q}",
        # The origin prefix is OPTIONAL and that is the whole point: idealo
        # emits both relative and absolute hrefs for the same kind of link, and
        # a pattern anchored on "/preisvergleich" matched only the relative
        # ones — which on a real search page were an ad for a fidget cube while
        # every actual result was absolute.
        "product_link": r'href="(?:https?://[^"/]+)?(/preisvergleich/OffersOfProduct/\d+_-[^"]*)"',
        "base": "https://www.idealo.de",
        "shop_attr": r'data-shop-name="([^"]+)"',
        "region_hint": "eu-core",
    },
    "kieskeurig.nl": {
        # The Dutch one. Its shop identity is NOT in a data attribute — it is
        # the alt text of each offer's logo image, "Top1Toys.nl - Affiliate
        # logo", so the carrier is the alt pattern below and the label needs the
        # affiliate suffix stripped. Found by dumping every plausible carrier on
        # a real product page rather than assuming idealo's shape travels.
        "search": "https://www.kieskeurig.nl/search?q={q}",
        "product_link": r'href="(?:https?://[^"/]+)?(/[a-z0-9\-]+/product/[^"]+)"',
        "base": "https://www.kieskeurig.nl",
        "shop_attr": r'alt="([^"]{2,40}?) logo"',
        "region_hint": "eu-core",
    },
    "geizhals.de": {
        # Kept although its challenge did NOT clear on 2026-09-09 ("bot
        # challenge not cleared", 11.9KB). Recorded rather than deleted so the
        # next attempt knows it was tried and how it failed.
        "search": "https://geizhals.de/?fs={q}&hloc=de",
        "product_link": r'href="(?:https?://[^"/]+)?(/[a-z0-9\-]+-a\d+\.html)"',
        "base": "https://geizhals.de",
        "shop_attr": r'data-merchant-name="([^"]+)"',
        "region_hint": "eu-core",
    },
}

# A shop-name string that is a marketplace stall rather than a shop with its
# own site. Reported separately: "otto.de (Marktplatzhändler)" means the
# product is on otto.de, which IS a lead, while "eBay - Shop aus Bern" is one
# person's eBay listing and is not.
STALL_NAMES = re.compile(r"(ebay|amazon marketplace|marketplace$|^kds-|hood\.de)", re.I)

# Labels that are the aggregator talking about itself, or an affiliate-network
# suffix bolted onto a real shop name ("Top1Toys.nl - Affiliate").
NOT_A_SHOP = re.compile(r"^(kieskeurig|idealo|geizhals|prisjakt|pricerunner)$", re.I)
LABEL_NOISE = re.compile(r"\s*-\s*affiliate$", re.I)

DEFAULT_COUNTRIES = ("DE", "NL", "BE", "AT", "DK", "FI", "FR")

# Politeness cap per shop: trying every path plus a control for each would be
# ~36 requests at one shop to answer one question, which is not a reasonable
# thing to do to a small retailer. The paths are ordered so the common ones come
# first, and the budget buys roughly the first eight of them.
MAX_FETCHES_PER_DOMAIN = 16

# Fuller headers than the shared client's defaults. Measured 2026-09-09: a
# meaningful share of shops answer 403 to a bare UA and 200 to this — the
# difference is Cloudflare, not policy, and these are the headers a real
# browser sends anyway. `core/http.py` still owns pacing and backoff.
BROWSERISH = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en-GB;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="124", "Not:A-Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

JSONLD = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)


@dataclass
class Candidate:
    """One domain, and everything learned about it."""

    domain: str
    urls: set[str] = field(default_factory=set)
    found_by: set[str] = field(default_factory=set)
    kind: str = "shop"          # shop | marketplace | aggregator
    # Filled in by `verify` only.
    platform: str | None = None
    gtins: set[str] = field(default_factory=set)
    titles: list[str] = field(default_factory=list)
    price: str | None = None
    ean_in_page: bool = False
    note: str | None = None

    @property
    def region(self) -> str:
        """Which shipping bucket this domain falls in. See REGION_TLDS."""
        domain = self.domain.lower()
        if domain.endswith(".se") or SE_FACING.search(domain):
            return "se"
        if SKIP_DOMAINS.search(domain) or domain.endswith(SKIP_TLDS):
            return "skip"
        for region, suffixes in REGION_TLDS.items():
            if domain.endswith(suffixes):
                return region
        return "unknown"


class HostPool:
    """One PoliteClient per host — independent pacing, independent cookies.

    Sharing one client across a 60-shop scan would be wrong twice: the pacer
    would throttle the whole scan as though 60 hosts were one host (60x
    slower than politeness needs, since each host sees a single request), and
    one cookie jar spanning unrelated shops is just incorrect. Per host is
    also what makes `max_retries=1` safe — a shop that answers 403 to
    everything costs one request, not three plus backoff.
    """

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self._clients: dict[str, PoliteClient] = {}
        self._min, self._max = min_delay, max_delay

    def get(self, url: str, headers: dict[str, str] | None = None) -> tuple[str, str | None]:
        """Return (body, error). Never raises — a scan must survive any host."""
        host = urlparse(url).netloc.lower()
        client = self._clients.get(host)
        if client is None:
            client = PoliteClient(min_delay=self._min, max_delay=self._max,
                                  timeout=20.0, max_retries=1)
            self._clients[host] = client
        try:
            return client.get(url, headers=headers).text, None
        except Exception as e:  # noqa: BLE001 — every failure is data here
            detail = getattr(getattr(e, "response", None), "status_code", None)
            return "", f"{type(e).__name__}{f' {detail}' if detail else ''}: {e}"[:160]

    def post(self, url: str, data: dict[str, str],
             headers: dict[str, str] | None = None) -> tuple[str, str | None]:
        """Form POST. Overpass only accepts the query as a POST body."""
        host = urlparse(url).netloc.lower()
        client = self._clients.get(host)
        if client is None:
            client = PoliteClient(min_delay=self._min, max_delay=self._max,
                                  timeout=180.0, max_retries=1)
            self._clients[host] = client
        try:
            return client.post_form(url, data, headers).text, None
        except Exception as e:  # noqa: BLE001
            return "", f"{type(e).__name__}: {e}"[:160]

    def close(self) -> None:
        for client in self._clients.values():
            client.close()


def registrable(host: str) -> str:
    host = host.lower().split(":")[0].removeprefix("www.")
    labels = host.split(".")
    if len(labels) > 2 and ".".join(labels[-2:]) in MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) > 2 else host


def usable(url: str) -> str | None:
    """The url, if it is one we could actually fetch. Otherwise None.

    Every path into this module ends here, because a decoded redirect is not
    guaranteed to be a URL at all: Bing's base64 payload is sometimes a
    tracking blob, and taking it on faith crashed this script on
    `url.split('/')[2]`.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        return None
    if JUNK_HOSTS.search(parsed.netloc + "."):
        return None
    return url


def unwrap(href: str) -> str | None:
    """Resolve an engine's redirect wrapper to the real target.

    DuckDuckGo hides results behind /l/?uddg=<encoded> and Bing behind
    /ck/a?u=a1<base64url>. An unwrapped link classifies as the ENGINE's own
    domain, so skipping this step makes every result look like junk — and for
    Bing it means zero results, since it wraps every single one.
    """
    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        return None
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    if "uddg" in query:
        return usable(query["uddg"][0])
    if "/ck/a" in parsed.path and "u" in query:
        raw = query["u"][0]
        if not raw.startswith("a1"):
            return None
        try:
            pad = "=" * (-len(raw[2:]) % 4)
            return usable(base64.urlsafe_b64decode(raw[2:] + pad).decode("utf-8", "replace"))
        except Exception:
            return None
    return usable(href)


def classify(domain: str) -> str:
    if AGGREGATORS.search(domain):
        return "aggregator"
    if MARKETPLACES.search(domain):
        return "marketplace"
    return "shop"


def add(found: dict[str, Candidate], url: str, source: str) -> None:
    host = urlparse(url).netloc
    if not host or JUNK_HOSTS.search(host + "."):
        return
    domain = registrable(host)
    if not domain or "." not in domain:
        return
    entry = found.setdefault(domain, Candidate(domain=domain, kind=classify(domain)))
    entry.urls.add(url.split("#")[0][:300])
    entry.found_by.add(source)


# ---------------------------------------------------------------------------
# Mode 1: engine search
# ---------------------------------------------------------------------------

def queries(eans: list[str], terms: list[str],
            tlds: list[str] | None = None) -> list[str]:
    """Query variants, widest signal first.

    A bare barcode is the strongest possible signal (it can only mean this
    product) and the weakest retrieval term (many shops never put it on the
    page). Pairing it with the brand recovers pages that mention both, and
    the name-only variants reach shops that publish no barcode at all — which
    is most of them.
    """
    out = [f'"{ean}"' for ean in eans]
    out += [f'{ean} beyblade' for ean in eans]
    out += terms
    # `site:.de <name>` is the entire fix for "we found no German store".
    # Nothing in an unrestricted query makes an engine prefer a German shop
    # over a US one, and the US one usually outranks it — so ASK. Applied to
    # the NAME variants and NOT the barcode: a shop that never publishes a
    # barcode still stocks the product, and restricting by country and barcode
    # at once returns nothing at all.
    for tld in tlds or []:
        suffix = tld if tld.startswith(".") else f".{tld}"
        for term in terms:
            out.append(f"site:{suffix} {term}")
    seen, unique = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def run_search(pool: HostPool, eans: list[str], terms: list[str],
               regions: list[str],
               tlds: list[str] | None = None) -> tuple[dict[str, Candidate], list[str]]:
    found: dict[str, Candidate] = {}
    log: list[str] = []
    for query in queries(eans, terms, tlds):
        for name, engine in ENGINES.items():
            for region in regions:
                token = engine["regions"].get(region)
                if token is None:
                    continue
                url = engine["url"].format(q=quote_plus(query), region=token)
                body, error = pool.get(url)
                if error:
                    log.append(f"{name}/{region} {query!r}: FAILED {error}")
                    continue
                low = body.lower()
                if any(m in low for m in engine["blocked_markers"]):
                    log.append(f"{name}/{region} {query!r}: BLOCKED (bot challenge)")
                    continue
                before = len(found)
                hits = 0
                for href in re.findall(r'href=["\']([^"\']+)["\']', body):
                    # An engine that wraps every result in a redirect gives us
                    # a free filter: an unwrapped link on such a page is its
                    # own navigation, never a result.
                    if engine["redirects_only"] and not REDIRECT.search(href):
                        continue
                    target = unwrap(href)
                    if target:
                        hits += 1
                        add(found, target, f"{name}/{region}")
                log.append(f"{name}/{region} {query!r}: {hits} link(s), "
                           f"{len(found) - before} new domain(s)")
    return found, log


# ---------------------------------------------------------------------------
# Mode 2: probe a shop's own search
# ---------------------------------------------------------------------------

def read_search(fetch: Fetch, origin: str, path: str, term: str) -> dict:
    url = urljoin(origin + "/", path.format(q=quote_plus(term)).lstrip("/"))
    body, error = fetch(url)
    if error:
        status = re.search(r"returning (\d{3})", error)
        return {"url": url, "error": error,
                "status": int(status.group(1)) if status else None}
    low = body.lower()
    return {
        "url": url,
        "links": link_set(body, term),
        # The term is echoed by the search box on every results page, so a
        # count of 1 means nothing on its own — only the comparison below
        # makes this number mean anything.
        "mentions": low.count(term.lower()),
        "empty_marker": next((m for m in EMPTY_MARKERS if m in low), None),
        "bytes": len(body),
        "platform": next((label for label, marker in PLATFORMS
                          if marker.lower() in low), None),
    }


# Either transport, same signature: (url) -> (body, error).
Fetch = Callable[[str], tuple[str, str | None]]


def link_set(body: str, term: str) -> set[str]:
    """Links that plausibly point at a product, by either signal."""
    links = {m.split("?")[0] for m in PRODUCT_LINK.findall(body)}
    for href in ANY_LINK.findall(body):
        if term.lower() in href.lower() and not NOT_A_PRODUCT.search(href):
            links.add(href.split("?")[0])
    return links


def judge(hit: dict, control: dict) -> str:
    """Did the shop's search actually answer the question we asked?

    The comparison, not the count, is the evidence. A path returning the same
    links for "beyblade" and for a nonsense word is a catalogue URL with an
    ignored query — which is how hlj.com appeared to stock 714 matches.
    """
    links, ctrl = hit["links"], control.get("links", set())
    overlap = len(links & ctrl) / len(links) if links else 0.0
    if ctrl and (links == ctrl or overlap > 0.9):
        return "query IGNORED (same results for a nonsense word)"
    if control.get("mentions", 0) >= hit["mentions"]:
        return "query IGNORED (term no more common than a nonsense word)"
    return f"REAL — {len(links)} product link(s)"


def probe_domain(fetch: Fetch, domain: str, term: str,
                 max_fetches: int | None = None) -> dict:
    """Ask one shop's own search for `term`, then CHECK THAT IT LISTENED.

    Counting product links is not enough, and believing it produced garbage:
    `/?s=x&post_type=product` on a shop that is not WooCommerce is just a
    catalogue URL with an ignored query, so hlj.com "matched" 714 products and
    rarewaves offered 28 Days Later. A dedicated Beyblade shop hides the same
    error the other way round — every one of its URLs matches the term whether
    its search works or not.

    So the test is a CONTROL QUERY. Ask the same path for a nonsense word: if
    the answers agree, the path ignores what we asked and its count is
    meaningless. That distinguishes "this shop has no Beyblade" from "this URL
    was never a search" without knowing anything about the platform.
    """
    origin = domain if domain.startswith("http") else f"https://{domain}"
    attempts: list[dict] = []
    winner: tuple[dict, dict] | None = None
    budget = max_fetches or MAX_FETCHES_PER_DOMAIN
    for path in SEARCH_PATHS:
        if budget <= 0:
            break
        attempt = read_search(fetch, origin, path, term)
        attempts.append(attempt)
        budget -= 1
        # NOT rejected for containing an empty-state phrase: themes ship that
        # string in the markup whether it is showing or not, which threw away
        # gameshop.se — a store we already track and know stocks these. The
        # control query is what separates real hits from noise.
        if attempt.get("error") or not attempt["links"] or budget <= 0:
            continue
        # Control-test THIS path before moving on. Accepting the first path
        # that merely returns links picks the wrong one constantly: WordPress
        # answers /search?q= with a generic product grid, so gameshop.se
        # "matched" and then failed the control, while /?s= — the path that
        # works — was never tried.
        control = read_search(fetch, origin, path, CONTROL_TERM)
        budget -= 1
        if judge(attempt, control).startswith("REAL"):
            winner = (attempt, control)
            break
        attempt["control_verdict"] = judge(attempt, control)

    if winner is None:
        blocked = [a["status"] for a in attempts if a.get("status")]
        loudest = max((a for a in attempts if "mentions" in a),
                      key=lambda a: a["mentions"], default=None)
        # These four outcomes are routinely confused with each other, and only
        # one of them means "this shop has none". Reporting them all as "no
        # results" would quietly hide every shop we simply failed to ask
        # properly — the exact failure this whole script exists to fix.
        served = [a for a in attempts if "mentions" in a]
        spa = [a for a in served if a["bytes"] > 100_000 and a["mentions"] <= 2]
        if blocked and not served:
            verdict = f"blocked HTTP {blocked[0]}"
        elif not served:
            verdict = "unreachable"
        elif loudest and loudest["mentions"] > 5:
            # The page talks about the term but none of its links look like
            # products to us. That is OUR gap, not the shop's — the fix is a
            # new URL shape in PRODUCT_LINK, so say so instead of filing it
            # under "no stock".
            verdict = (f"mentions the term {loudest['mentions']}x but no known "
                       f"product-URL shape — check {loudest['url']}")
        elif spa:
            # Answered 200 with a big page that barely mentions what we asked
            # for: the query was dropped and we were served a template. Its
            # real search runs client-side, so this needs Ginza's treatment —
            # find the JSON endpoint the page calls — or a browser.
            verdict = ("search renders CLIENT-SIDE (query ignored, template served) "
                       "— needs its JSON endpoint or a browser")
        else:
            verdict = "no results — probably does not stock it"
        return {"domain": domain, "attempts": [strip(a) for a in attempts],
                "verdict": verdict, "product_links": 0,
                "url": loudest["url"] if loudest else None,
                "mentions": loudest["mentions"] if loudest else 0,
                "platform": next((a.get("platform") for a in attempts if a.get("platform")), None)}

    hit, control = winner
    overlap = (len(hit["links"] & control.get("links", set())) / len(hit["links"])
               if hit["links"] else 0.0)
    return {
        "domain": domain, "verdict": judge(hit, control), "url": hit["url"],
        "product_links": len(hit["links"]), "mentions": hit["mentions"],
        "control_links": len(control.get("links", set())),
        "control_mentions": control.get("mentions"),
        "overlap": round(overlap, 2),
        "platform": hit["platform"],
        "samples": sorted(hit["links"])[:4],
        "attempts": [strip(a) for a in attempts],
    }


def strip(attempt: dict) -> dict:
    """JSON-safe view of an attempt (link sets are for comparison, not output)."""
    return {k: (len(v) if isinstance(v, set) else v) for k, v in attempt.items()}


# ---------------------------------------------------------------------------
# Mode 3: verify a candidate page
# ---------------------------------------------------------------------------

def read_jsonld(body: str) -> list[dict]:
    """Every Product object in the page's JSON-LD, flattened.

    This is the payoff: schema.org Product carries `gtin13`, which is the
    barcode. A shop publishing it can be matched to another shop's catalogue
    with no title guessing at all — the one real answer to this project's
    cross-site naming problem. Shops nest it inconsistently (bare object,
    @graph, ItemList), so flatten rather than assume a shape.
    """
    out: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and t.lower() == "product" for t in types):
                out.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for block in JSONLD.findall(body):
        try:
            walk(json.loads(block.strip()))
        except Exception:
            continue
    return out


def verify_url(pool: HostPool, url: str, eans: list[str]) -> dict:
    body, error = pool.get(url)
    if error:
        return {"url": url, "error": error}
    low = body.lower()
    products = read_jsonld(body)
    gtins, prices = set(), []
    for product in products:
        for key in ("gtin13", "gtin", "gtin12", "gtin14", "ean"):
            value = product.get(key)
            if value:
                gtins.add(re.sub(r"\D", "", str(value)))
        offers = product.get("offers")
        for offer in (offers if isinstance(offers, list) else [offers] if offers else []):
            if isinstance(offer, dict) and offer.get("price"):
                prices.append(f"{offer['price']} {offer.get('priceCurrency', '')}".strip())
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return {
        "url": url,
        "platform": next((label for label, marker in PLATFORMS
                          if marker.lower() in low), None),
        "ean_in_page": [e for e in eans if e in body],
        "gtins": sorted(gtins),
        "jsonld_products": len(products),
        "price": prices[0] if prices else None,
        "title": " ".join(title.group(1).split())[:110] if title else None,
    }


# ---------------------------------------------------------------------------
# Mode 4: harvest a shop directory from OpenStreetMap
# ---------------------------------------------------------------------------

def tiles(box: tuple[float, float, float, float], grid: int
          ) -> Iterator[tuple[float, float, float, float]]:
    """Split a bounding box into a grid x grid set of smaller boxes.

    TILING IS WHAT MAKES THIS WORK AT ALL. Measured 2026-09-09: whole-country
    boxes for DE, NL, AT, FI, FR and DK all came back 504 Gateway Timeout or
    read-timed-out against both public Overpass instances, while BE — a small
    country — returned 316 shops immediately. The public instances cap query
    cost, so the fix is smaller queries, not a longer timeout.
    """
    south, west, north, east = box
    dy, dx = (north - south) / grid, (east - west) / grid
    for row in range(grid):
        for col in range(grid):
            yield (south + row * dy, west + col * dx,
                   south + (row + 1) * dy, west + (col + 1) * dx)


def overpass(pool: HostPool, country: str, grid: int = 3) -> tuple[list[str], str | None]:
    """Every mapped shop website in one country's box. Returns (urls, error).

    `nwr` covers nodes, ways and relations: a mapped shop can be any of the
    three, and asking only for nodes silently loses every shop mapped as a
    building outline — which is most of the larger ones.

    A tile that fails is reported and SKIPPED, not fatal: nine tiles of which
    eight answered is a useful, honestly-labelled partial result, whereas
    treating it as a failure would throw away everything.
    """
    box = COUNTRY_BOXES.get(country)
    if box is None:
        return [], f"no bounding box defined for {country}; add one to COUNTRY_BOXES"

    urls: list[str] = []
    failures = 0
    total = 0
    for south, west, north, east in tiles(box, grid):
        total += 1
        query = (
            f'[out:json][timeout:120];'
            f'nwr["shop"~"^({OSM_SHOP_TYPES})$"]["website"]'
            f'({south:.3f},{west:.3f},{north:.3f},{east:.3f});'
            f'out tags;'
        )
        elements = None
        for host in OVERPASS_HOSTS:
            body, error = pool.post(host, {"data": query}, OVERPASS_HEADERS)
            if error:
                continue
            try:
                elements = json.loads(body)["elements"]
                break
            except Exception:
                continue
        if elements is None:
            failures += 1
            continue
        urls += [e.get("tags", {}).get("website", "") for e in elements]

    if failures == total:
        return [], f"all {total} tiles failed"
    note = f"{failures}/{total} tiles failed" if failures else None
    return urls, note


def run_directory(pool: HostPool, countries: list[str], args_grid: int = 3) -> dict[str, Candidate]:
    found: dict[str, Candidate] = {}
    for country in countries:
        urls, error = overpass(pool, country, args_grid)
        if error and not urls:
            print(f"  {country}: FAILED {error}")
            continue
        if error:
            print(f"  {country}: PARTIAL — {error}")
        before = len(found)
        for url in urls:
            target = usable(url if "//" in url else f"https://{url}")
            if target:
                add(found, target, f"osm/{country}")
        print(f"  {country}: {len(urls)} mapped shop(s) with a website, "
              f"{len(found) - before} new domain(s)")
    return found


# ---------------------------------------------------------------------------
# Mode 5: scrape retailer lists off price aggregators (browser)
# ---------------------------------------------------------------------------

def shop_domain(name: str) -> tuple[str | None, str]:
    """Turn an aggregator's shop label into a domain, if it is one.

    idealo writes "galaxus.de - Shop aus Hamburg" and "otto.de
    (Marktplatzhändler)", so the domain is the head of the string with the
    location suffix and any parenthetical stripped. A label that is not a
    domain ("kds-tuning") is returned as a name to look up rather than being
    mangled into one, because inventing "kds-tuning.de" would be a guess
    presented as a finding.
    """
    label = LABEL_NOISE.sub("", name).split(" - ")[0].split("(")[0].strip().lower()
    if NOT_A_SHOP.match(label):
        return None, ""
    if re.fullmatch(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+", label) and "." in label:
        return registrable(label), label
    return None, name.strip()


def run_offers(fetcher, terms: list[str], max_products: int = 3,
               only: list[str] | None = None) -> tuple[dict[str, Candidate], list[str]]:
    """For each aggregator: search by name, open products, harvest retailers."""
    found: dict[str, Candidate] = {}
    log: list[str] = []
    unresolved: set[str] = set()
    for name, config in AGGREGATORS_BROWSER.items():
        if only and name not in only:
            continue
        for term in terms:
            url = config["search"].format(q=quote_plus(term))
            html, error = fetcher.fetch(url)
            if error:
                log.append(f"{name} search {term!r}: FAILED {error}")
                continue
            products = []
            for href in re.findall(config["product_link"], html):
                href = href.replace("&amp;", "&")
                if href not in products:
                    products.append(href)
            log.append(f"{name} search {term!r}: {len(products)} product page(s)")
            # The first word of the search term is the relevance test below.
            keyword = term.split()[0].lower()
            for href in products[:max_products]:
                # AN AGGREGATOR'S SEARCH IS NOT PRECISE, and harvesting shops
                # from whatever it returns is how a Beyblade scan "found"
                # coolblue.nl and mediamarkt.nl: kieskeurig answered "beyblade x
                # starter pack" with televisions and vacuum cleaners, and idealo
                # answered with a fidget cube. Every shop on those pages is a
                # real shop and a false lead. So the product must be THE PRODUCT
                # before its offers count as evidence of stocking it.
                if keyword not in href.lower():
                    log.append(f"  {href[:56]}: skipped, not a {keyword!r} product")
                    continue
                page, error = fetcher.fetch(config["base"] + href)
                if error:
                    log.append(f"  {href[:48]}: FAILED {error}")
                    continue
                labels = set(re.findall(config["shop_attr"], page))
                shops, stalls = 0, 0
                for label in labels:
                    if STALL_NAMES.search(label):
                        stalls += 1
                        continue
                    domain, raw = shop_domain(label)
                    if domain is None:
                        if raw:
                            unresolved.add(raw)
                        continue
                    add(found, f"https://{domain}/", f"{name}")
                    shops += 1
                log.append(f"  {href[:56]}: {shops} shop(s), {stalls} marketplace stall(s)")
    if unresolved:
        log.append("shop labels that are not domains (look these up by hand): "
                   + ", ".join(sorted(unresolved)[:20]))
    return found, log


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def confirm(pool: HostPool, found: dict[str, Candidate], eans: list[str],
            terms: list[str]) -> None:
    """Fetch each candidate and check the page mentions what we searched for.

    Engines pad results, and how much they pad depends on where you ask from:
    the same query that came back clean from a home connection returned Czech
    legal databases and industrial filter vendors from the VPS. A candidate
    list nobody checked is therefore mostly noise, and checking is one request
    per domain — cheap for a research run, and it upgrades "a page somewhere
    mentioned this" into "this shop's page carries the barcode".
    """
    needles = [e for e in eans] + [t.split()[0].lower() for t in terms if t]
    for candidate in found.values():
        if candidate.kind == "aggregator":
            continue
        url = sorted(candidate.urls, key=len, reverse=True)[0]
        result = verify_url(pool, url, eans)
        if result.get("error"):
            candidate.note = result["error"][:60]
            continue
        candidate.platform = result["platform"]
        candidate.gtins = set(result["gtins"])
        candidate.price = result["price"]
        candidate.ean_in_page = bool(result["ean_in_page"])
        if result["title"]:
            candidate.titles = [result["title"]]
        body_hit = candidate.ean_in_page or any(
            n and result["title"] and n in result["title"].lower() for n in needles)
        candidate.note = "confirmed" if body_hit else "page does not mention the product"


def report_search(found: dict[str, Candidate], log: list[str]) -> str:
    lines = ["# Store discovery — engine search", "", "## Engine yield", ""]
    lines += [f"    {entry}" for entry in log]
    skipped = sorted(c.domain for c in found.values() if c.region == "skip")
    if skipped:
        lines += ["", f"## Outside the EU — not worth the freight ({len(skipped)})", "",
                  "    " + ", ".join(skipped)]
    labels = {"se": "Sweden", "nordic": "Nordic neighbours",
              "eu-core": "EU core (DE/NL/BE/AT/LU/FR) — the target",
              "eu-other": "EU, further out", "unknown": "Region unknown — check these"}
    for kind in ("shop", "marketplace", "aggregator"):
        for region in REGION_ORDER:
            label = labels.get(region)
            if label is None:
                continue
            group = sorted((c for c in found.values()
                            if c.kind == kind and c.region == region),
                           key=lambda c: c.domain)
            if not group:
                continue
            confirmed = [c for c in group if c.note == "confirmed"]
            group = confirmed + [c for c in group if c.note != "confirmed"]
            lines += ["", f"## {label} — {kind} ({len(group)}"
                          + (f", {len(confirmed)} confirmed)" if confirmed else ")"), ""]
            for c in group:
                mark = "**CONFIRMED** " if c.note == "confirmed" else ""
                lines.append(f"- {mark}**{c.domain}** — found by {', '.join(sorted(c.found_by))}")
                if c.platform or c.gtins or c.price:
                    lines.append(f"  - platform `{c.platform or '?'}`"
                                 f"  price `{c.price or '?'}`"
                                 f"  gtin `{','.join(sorted(c.gtins)) or '-'}`")
                for title in c.titles:
                    lines.append(f"  - {title}")
                for url in sorted(c.urls)[:2]:
                    lines.append(f"  - {url}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode",
                        choices=["search", "probe", "verify", "directory", "offers"])
    parser.add_argument("--ean", action="append", default=[],
                        help="barcode to search for (repeatable)")
    parser.add_argument("--term", action="append", default=[],
                        help="product/brand text to search for (repeatable)")
    parser.add_argument("--aggregator", action="append", default=[],
                        help="offers mode: restrict to these aggregators by name")
    parser.add_argument("--max-products", type=int, default=3,
                        help="offers mode: product pages to open per search (default 3)")
    parser.add_argument("--show-browser", action="store_true",
                        help="run the browser headed, to watch what it does")
    parser.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES),
                        help="directory mode: ISO country codes to harvest from OpenStreetMap")
    parser.add_argument("--grid", type=int, default=3,
                        help="directory mode: split each country box into grid x grid tiles. "
                             "Whole-country queries time out on the public Overpass instances.")
    parser.add_argument("--out-domains",
                        help="directory mode: write the domains here, ready for `probe`")
    parser.add_argument("--tld",
                        help="comma-separated TLDs to restrict NAME queries to, e.g. "
                             "de,nl,fr,at,dk. The fix for finding no German shop.")
    parser.add_argument("--regions", default="se,world,de,uk",
                        help="engine region tokens to try (default se,world,de,uk)")
    parser.add_argument("--domains", help="probe: file of domains, one per line")
    parser.add_argument("--urls", help="verify: file of URLs, one per line")
    parser.add_argument("--probe-term", default="beyblade")
    parser.add_argument("--max-fetches", type=int,
                        help="probe: cap requests per domain (default %d). Lower it for a "
                             "fast triage pass over a large candidate pool."
                             % MAX_FETCHES_PER_DOMAIN)
    parser.add_argument("--browser", action="store_true",
                        help="probe/verify through Chromium instead of plain HTTP. NOT a "
                             "fallback for this market: measured 2026-09-09, 0 of 16 German "
                             "and Dutch shops were readable over HTTP — every one either "
                             "renders search client-side or answers 403.")
    parser.add_argument("--verify-found", action="store_true",
                        help="search mode: fetch each candidate and keep only those whose "
                             "page actually mentions the product (strongly recommended)")
    parser.add_argument("--min-delay", type=float,
                        help="default 1.0; search mode defaults slower, see ENGINE_DELAY")
    parser.add_argument("--max-delay", type=float)
    parser.add_argument("--json", dest="json_out", help="write raw findings here")
    parser.add_argument("--markdown", dest="md_out", help="write a report here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Engines block when hurried; shops do not care. Same politeness policy,
    # different constant, chosen by what we are talking to.
    floor, ceiling = ENGINE_DELAY if args.mode == "search" else (1.0, 2.5)
    pool = HostPool(args.min_delay if args.min_delay is not None else floor,
                    args.max_delay if args.max_delay is not None else ceiling)
    payload: object

    try:
        if args.mode == "search":
            if not args.ean and not args.term:
                parser.error("search needs at least one --ean or --term")
            found, log = run_search(pool, args.ean, args.term,
                                    [r.strip() for r in args.regions.split(",") if r.strip()],
                                    [t.strip() for t in (args.tld or "").split(",") if t.strip()])
            if args.verify_found:
                # Shops answer at shop pace, not engine pace — the slow
                # ENGINE_DELAY is for engines only.
                shop_pool = HostPool(1.0, 2.5)
                try:
                    confirm(shop_pool, found, args.ean, args.term)
                finally:
                    shop_pool.close()
            for entry in log:
                print("   ", entry)
            report = report_search(found, log)
            print()
            print(report)
            payload = {"log": log, "candidates": [
                {**vars(c), "urls": sorted(c.urls), "found_by": sorted(c.found_by),
                 "gtins": sorted(c.gtins), "region": c.region}
                for c in sorted(found.values(), key=lambda c: c.domain)]}
            if args.md_out:
                Path(args.md_out).write_text(report, encoding="utf-8")

        elif args.mode == "probe":
            if not args.domains:
                parser.error("probe needs --domains")
            domains = [d.split("#")[0].strip() for d in
                       Path(args.domains).read_text(encoding="utf-8").splitlines()]
            domains = [d for d in domains if d]
            results = []
            # One browser for the whole run when asked for, so a cleared
            # challenge and its cookies carry across domains.
            fetcher = None
            if args.browser:
                from core.browser import BrowserFetcher

                fetcher = BrowserFetcher(headless=not args.show_browser)
                fetcher.start()
            try:
                fetch: Fetch = ((lambda u: fetcher.fetch(u)) if fetcher is not None
                                else (lambda u: pool.get(u, headers=BROWSERISH)))
                for domain in domains:
                    result = probe_domain(fetch, domain, args.probe_term,
                                          args.max_fetches)
                    results.append(result)
                    real = result["verdict"].startswith("REAL")
                    print(f"  {'OK ' if real else '   '} {domain:<26} "
                          f"[{result.get('platform') or 'platform?':<12}] {result['verdict']}")
                    if real:
                        print(f"       {result['url']}")
                        print(f"       control: {result['control_links']} link(s), "
                              f"overlap {result['overlap']}, "
                              f"mentions {result['mentions']} vs {result['control_mentions']}")
                        for sample in result["samples"]:
                            print(f"       {sample[:94]}")
            finally:
                if fetcher is not None:
                    fetcher.close()
            usable_count = sum(r["verdict"].startswith("REAL") for r in results)
            print(f"\n{usable_count}/{len(results)} shop searches usable")
            # The user's rule: only stores with a WIDE selection are worth
            # tracking, so rank by how much they actually returned rather than
            # listing every shop that technically answered.
            wide = sorted((r for r in results if r["verdict"].startswith("REAL")),
                          key=lambda r: -r["product_links"])
            if wide:
                print("\nby selection size:")
                for r in wide:
                    print(f"  {r['product_links']:>4} products  {r['domain']}")
            payload = results

        elif args.mode == "offers":
            if not args.term:
                parser.error("offers needs at least one --term (a NAME; the barcode "
                             "returns nothing on an aggregator)")
            from core.browser import browser_fetcher

            with browser_fetcher(headless=not args.show_browser) as fetcher:
                found, log = run_offers(fetcher, args.term, args.max_products,
                                        args.aggregator or None)
                if args.verify_found:
                    shop_pool = HostPool(1.0, 2.5)
                    try:
                        confirm(shop_pool, found, args.ean, args.term)
                    finally:
                        shop_pool.close()
            for entry in log:
                print("   ", entry)
            report = report_search(found, log)
            print()
            print(report)
            if args.md_out:
                Path(args.md_out).write_text(report, encoding="utf-8")
            payload = {"log": log, "candidates": [
                {**vars(c), "urls": sorted(c.urls), "found_by": sorted(c.found_by),
                 "gtins": sorted(c.gtins), "region": c.region}
                for c in sorted(found.values(), key=lambda c: c.domain)]}

        elif args.mode == "directory":
            countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
            found = run_directory(pool, countries, args.grid)
            # Ordered by shipping cost, and the out-of-EU bucket is dropped
            # rather than written: a US toy shop is not a lead here.
            keep = [c for c in found.values() if c.region != "skip"]
            print()
            for region in REGION_ORDER:
                group = sorted(c.domain for c in keep if c.region == region)
                if group:
                    print(f"  {region:<10} {len(group):>4}  {', '.join(group[:6])}"
                          f"{' ...' if len(group) > 6 else ''}")
            dropped = len(found) - len(keep)
            print(f"\n  {len(keep)} in-scope domain(s); {dropped} outside the EU, dropped")
            if args.out_domains:
                lines = ["# Harvested from OpenStreetMap via Overpass on "
                         f"{', '.join(countries)}.",
                         "# Shops, not confirmed stockists — feed this to `probe`.", ""]
                for region in REGION_ORDER:
                    group = sorted(c.domain for c in keep if c.region == region)
                    if group:
                        lines += [f"# --- {region} ---"] + group + [""]
                Path(args.out_domains).write_text("\n".join(lines), encoding="utf-8")
                print(f"  wrote {args.out_domains}")
            payload = [{"domain": c.domain, "region": c.region,
                        "found_by": sorted(c.found_by), "urls": sorted(c.urls)}
                       for c in sorted(keep, key=lambda c: (c.region, c.domain))]

        else:
            if not args.urls:
                parser.error("verify needs --urls")
            urls = [u.strip() for u in
                    Path(args.urls).read_text(encoding="utf-8").splitlines() if u.strip()]
            results = [verify_url(pool, url, args.ean) for url in urls]
            for r in results:
                if r.get("error"):
                    print(f"  FAIL {r['url']}\n       {r['error']}")
                    continue
                mark = "EAN" if r["ean_in_page"] else ("gtin" if r["gtins"] else "   ")
                print(f"  [{mark}] {r['url']}")
                print(f"        {r['title']}")
                print(f"        platform={r['platform'] or '?'}  price={r['price'] or '?'}"
                      f"  gtin={','.join(r['gtins']) or '-'}")
            payload = results
    finally:
        pool.close()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str),
                                       encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
