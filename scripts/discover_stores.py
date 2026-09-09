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
from dataclasses import dataclass, field
from pathlib import Path
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

# Swedish-facing shops that do not live on a .se domain. Grouping by TLD alone
# would file coolshop.se's sibling coolshop.com and boozt.com as "worldwide",
# which is wrong in the only way that matters: they deliver here in SEK.
SE_FACING = re.compile(
    r"(boozt|coolshop|lekmer|jollyroom|cdon|webhallen|inet\.se|elgiganten"
    r"|power\.se|netonnet|adlibris|bokus|amazon\.se|apotea|babyland)", re.I)

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

# Politeness cap per shop. Nine search paths plus a control each would be 18
# requests at one shop to answer one question, which is not a reasonable thing
# to do to a small retailer.
MAX_FETCHES_PER_DOMAIN = 8

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
        if self.domain.endswith(".se") or SE_FACING.search(self.domain):
            return "se"
        return "world"


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

def queries(eans: list[str], terms: list[str]) -> list[str]:
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
    seen, unique = set(), []
    for q in out:
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def run_search(pool: HostPool, eans: list[str], terms: list[str],
               regions: list[str]) -> tuple[dict[str, Candidate], list[str]]:
    found: dict[str, Candidate] = {}
    log: list[str] = []
    for query in queries(eans, terms):
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

def read_search(pool: HostPool, origin: str, path: str, term: str) -> dict:
    url = urljoin(origin + "/", path.format(q=quote_plus(term)).lstrip("/"))
    body, error = pool.get(url, headers=BROWSERISH)
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


def probe_domain(pool: HostPool, domain: str, term: str) -> dict:
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
    budget = MAX_FETCHES_PER_DOMAIN
    for path in SEARCH_PATHS:
        if budget <= 0:
            break
        attempt = read_search(pool, origin, path, term)
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
        control = read_search(pool, origin, path, CONTROL_TERM)
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
    for kind in ("shop", "marketplace", "aggregator"):
        for region, label in (("se", "Sweden-facing"), ("world", "Worldwide")):
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
    parser.add_argument("mode", choices=["search", "probe", "verify"])
    parser.add_argument("--ean", action="append", default=[],
                        help="barcode to search for (repeatable)")
    parser.add_argument("--term", action="append", default=[],
                        help="product/brand text to search for (repeatable)")
    parser.add_argument("--regions", default="se,world,de,uk",
                        help="engine region tokens to try (default se,world,de,uk)")
    parser.add_argument("--domains", help="probe: file of domains, one per line")
    parser.add_argument("--urls", help="verify: file of URLs, one per line")
    parser.add_argument("--probe-term", default="beyblade")
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
                                    [r.strip() for r in args.regions.split(",") if r.strip()])
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
            for domain in domains:
                result = probe_domain(pool, domain, args.probe_term)
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
            usable_count = sum(r["verdict"].startswith("REAL") for r in results)
            print(f"\n{usable_count}/{len(results)} shop searches usable")
            payload = results

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
