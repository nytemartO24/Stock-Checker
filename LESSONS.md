# Lessons Learned

Append-only log of specific mistakes made and how they were fixed. Newest
entries at the bottom. Periodically distill recurring patterns into
CLAUDE.md and trim this file.

<!-- Example entry format:
- 2026-09-08: Assumed site X's stock status was in a data attribute; it's
  actually inferred from button text ("Add to Cart" vs "Sold Out"). Fixed
  the parser; added a fixture test to catch this if the site changes.
-->

- 2026-09-08: Tested whether Amazon needs Playwright by fetching
  `/-/en/dp/<asin>` with no session, got each market's native layout back,
  and concluded 23/24 pages were blocked. They were not — `browser.py`
  already documented that you must warm up on the `/-/en/` homepage so the
  session language cookie is set. Re-run with a cookie jar: 6/6 English
  product pages. Read the rationale in existing code before designing an
  experiment whose result would contradict it.

- 2026-09-08: Reported a toysnowman price as `CA$185.00` by taking the
  currency from config while the store had priced in SEK. Shopify Markets
  localises by visitor country, and merely sending an `Accept-Language`
  header flipped the same URL from 25.99 to 185.00. Fixed by pinning
  `?country=SE` on every Shopify request and asserting in tests that each
  configured site declares both a country and a currency. Don't label a
  scraped price with a currency the response never stated.
- 2026-09-08: First build shipped three self-inflicted alert bugs that
  `/code-review` caught: pruning state after a partially-failed run (which
  makes missed products re-alert as new when they return), recording state
  after a FAILED Discord send (which loses the alert permanently), and a
  `max_retries: 0` config crashing with UnboundLocalError. All three were
  invisible to the passing test suite because the tests only covered the
  happy path.

- 2026-09-08: Hardcoded a currency per Amazon marketplace (de -> EUR) and
  the fixture immediately disproved it: with delivery pinned to Sweden,
  amazon.de quotes `SEK766.25`. Converting that as EUR would have inflated
  it ~11x and flagged every cross-border listing as a scalp. Fixed with
  `detect_currency()`, reading the symbol off the price string. Second time
  in one session that assuming a currency was wrong (see the Shopify entry
  above) - if a price is displayed, the currency is displayed with it, so
  read it.

- 2026-09-08: Wrote the Amazon "is this page about my country?" guard as
  `delivery_country not in page_text`. Dead code: once the delivery location
  is pinned, the glow ingress names the destination on EVERY page, so the
  condition was never true and an amazon.de page dispatching to the United
  States would have been read as a genuine in-stock result. The pilot got
  this right by parsing the country out of the banner with a regex anchored
  on "showing you items that dispatch to"; I replaced proven code with a
  looser check. My test passed only because the synthetic fixture omitted
  the glow ingress that real pages always carry - when a fixture is
  hand-written, check it against a real page for what it is MISSING, not
  just what it contains.
- 2026-09-08: An empty Amazon watchlist yielded no products and reported no
  error, so the run looked like a complete view of zero products and pruned
  all 24 stored entries. Commenting out a watchlist to pause tracking - the
  exact convention the pilot used - would have caused an alert storm on
  restore. Fixed at both levels: the checker reports an error, and
  `SiteState.prune()` now refuses to prune against an entirely empty view.

- 2026-09-08: Ported news-notifier's `safe_goto()` (which tolerates Amazon's
  spurious "Download is starting" navigation error) but left `open_market()`'s
  warm-up calling `page.goto()` directly. When it fired on .se the whole
  market was lost for that run - location never pinned, so nothing from it
  was comparable. The pilot had fixed exactly this bug one level down and
  said so in a comment. Porting proven code means porting the rule it
  encodes ("every navigation goes through safe_goto"), not just the function.

- 2026-09-08: Two config edits reported success while changing nothing —
  `str.replace()` on a target that no longer matched, with no assertion. The
  title filter looked implemented and tested (110 passing) but the live scan
  dropped 0 of 60 products. Every scripted edit gets
  `assert s.count(old) == 1` before the replace; a silent no-op that still
  prints "done" is worse than a crash.

- 2026-09-09: The .se warm-up retry I added could never have worked. Amazon's
  spurious download prompt leaves the page at `chrome-error://chromewebdata/`,
  and my "fix" retried by re-navigating THAT page, which lands on chrome-error
  again — 6 of 36 overnight runs failed identically, all three attempts. A
  retry has to change something: replacing the page (keeping the context, so
  the warmed-up cookies survive) escapes the broken state, re-issuing the same
  call on the same object cannot. Also: the failure was only visible because
  the unpinned-location guard logs loudly and marks the run incomplete —
  without it, .se would have been quietly reporting a German destination.

- 2026-09-09: Diffing this module against news-notifier found a defect I had
  introduced by omission: it waits up to 6s for the delivery/seller/price
  blocks to be injected before reading the page, and I read `page.content()`
  immediately. Losing that race looks identical to "this product has no date".
  Porting a module means porting its WAITS, not just its selectors — the
  timing was as load-bearing as the parsing.
- 2026-09-09: Storing delivery dates as displayed ("22 September") and
  re-parsing them was a bug on a timer: the parser assumes next year for a
  past date, so every stored baseline silently jumped ~350 days into the
  future once its day passed, making any real date look like a huge
  improvement. news-notifier's plausibility screen cannot catch it (350 < 400
  days). Fixed by storing the resolved ISO date and comparing that. When a
  value needs comparing later, store the unambiguous form, not the pretty one.
- Counting search results without a CONTROL query measures nothing. `/?s=x&post_type=product` on a non-WooCommerce shop is a catalogue URL with an ignored query, so hlj.com reported 714 "matches" and rarewaves offered 28 Days Later. Asking the same path for a nonsense word and comparing is the whole test — and it must run PER PATH, because stopping at the first path that returns links picked WordPress's generic grid and wrote off gameshop.se, a store we already track.
- Rejecting a results page for containing "inga resultat" / "no results" is wrong: themes ship the empty-state string whether or not it is displayed. It threw away a store known to stock the product.
- A search engine returning nothing and a search engine REFUSING are different facts, and both look like zero. Bing wraps every result in `/ck/a?u=a1<base64>`, so reading raw hrefs reports "0 results" from a perfectly good response; DDG-lite answers HTTP 202 with a bot challenge; Marginalia's `old-search.` host answers 200 with a 1KB stub. Report blocked/failed per engine or the yield is a lie.
- Decoding a redirect wrapper does not guarantee a URL. A Bing payload that decoded to a non-URL reached `url.split('/')[2]` and killed the whole scan; everything now passes through one `usable()` gate.
- Bing silently ignores a bare-TLD `site:.de` — it returns the byte-identical result set with zero new domains. Three sessions' worth of "there are no German retailers" was actually "nothing ever asked", and the request that looked like asking was dropped server-side. Never trust a search operator without a control: compare the restricted result set against the unrestricted one.
- Reaching for a search engine to answer "which shops exist in country X" was the wrong tool the whole time. OpenStreetMap has the answer as structured data (`shop=toys` + `website`, per country, free, no bot wall) and returned 609 EU shops in one query, none of them ever surfaced by an engine.
- A store being on Shopify does NOT mean `sites/shopify.py` can serve it. rarewaves has 1348 collections and no Beyblade one, and `/collections/beyblade` answers 200 with zero products — which is worse than a 404, because it reads as a working empty collection rather than a missing one.
- Do not trust a search index for stock. rarewaves' catalogue comes from Klevu, so its `inStock` was cross-checked against Shopify's authoritative `available` before being believed; they agreed, and that check is now the documented assumption the module rests on.
- A link pattern anchored on a path prefix (`href="(/preisvergleich/...`) silently matched only idealo's RELATIVE hrefs. The one relative link on a real search page was an advert for a fidget cube; every genuine result was absolute. The extractor looked like it worked and returned one wrong product — accept an optional origin.
- Aggregators index manufacturer titles, not barcodes: on idealo the name query returns the product and the EAN query returns nothing at all. The barcode confirms identity; it does not find shops.
- Overpass caps query cost, so a whole-country bounding box 504s while a tile of it answers instantly. When a free API times out, make the question smaller before making the timeout bigger.
- `python x.py > log &` inside the Bash tool plus its own background flag killed the process after one request, leaving a one-line log that looked like a single failed request. Use the tool's backgrounding OR the shell's, never both.
- 0 of 16 German and Dutch shops were readable over plain HTTP. For this market the browser is the normal transport, not a fallback — assuming httpx first cost several rounds of "no results" that were really "we never got the page".
- Harvesting shops from whatever an aggregator's search returns is not evidence. kieskeurig answered "beyblade x starter pack" with televisions and vacuum cleaners, idealo with a fidget cube, and every shop on those pages went into the candidate list — coolblue.nl, mediamarkt.nl and einzigundartig.de all got in that way, and einzigundartig had already been reported to the user. The product must match the search term before its offers count.
- "40 product links" is not "40 products". lereservoir.lu was reported to the user as having 40 when the page holds 36 tiles of which TWO are Beyblade; the count was inflated by add-to-cart hrefs containing the term. Count PRODUCTS, and say which you counted.
- A shop's own search can be WORSE than its category page: lereservoir's search finds 2 Beyblades where the Hasbro category has 4. Poll both and union them rather than assuming search is the complete view.
- Cloudflare's JS challenge is not beaten by a headed browser. kaufland.de refused a persistent profile, a real UA, AutomationControlled disabled and an 18s wait, never issuing cf_clearance — so "run it headed" is not the fix, and the honest move is to report it rather than keep trying.
- Diagnosing from log TEXT instead of from the page is guessing. "location modal did not fill in within 8s" reads like slow content, so a longer wait was proposed; instrumenting the real code path showed the controls present at 1000ms and identical at 15s, while `.a-popover-inner` count was ZERO — the modal never opened at all. The proposed fix would have changed nothing. Render the page and look at it.
- Guessing query parameters is the same mistake in a different costume. `pageIndex/p/pageNumber/offset/resultsPerPage` were all tried against idealo and all silently returned the identical first page; watching the network took one attempt to reveal the real endpoint (`/csr/api/v2/modules/searchResult`), that paging is encoded in the URL PATH, that Akamai is what returns the 403s, and that tiles are `[data-testid="product-tile"]`. Observe the traffic FIRST — this project already learned this on ginza and did not apply it.
- Counting anchors is not counting products: idealo's search-suggest dropdown contains product links, which inflated every tile count in the session and produced market-coverage figures that had to be retracted. Anchor a count to the real tile element.
- `fetch()` cannot set a `Referer` — browsers forbid the header — so a "does it need a Referer?" test done that way proves nothing. Use the request context (`page.request`) or a real navigation.
- Running `main.py` by hand skips `deploy/run.sh`, which is what sources `.env`, so alerts log "DISCORD_WEBHOOK_URL not set" and go nowhere. run.sh's own comment says this. Undelivered alerts correctly did NOT advance state, so nothing was lost.
- Claimed "Amazon has no new-product discovery" when the user had just received an Amazon new-product ping. It was true of THIS project and false of their setup: news-notifier's scrape_catalog_multi.py does it and still runs at :10/:40. The tell was in the message — the alert came from NotifierMan in a different format from ours — and checking which system sent a ping takes one grep. Scope a claim to the component you actually verified.
- Searching Amazon by product name without a brand guard returns garbage that LOOKS like results: "Sterling Wolf" gave 42 sterling-silver wolf pendants, "Clock Mirage" a book called The Clock Mirage, "Dark Perseus" another book — and the script duly emitted them as watchlist entries. Exactly the same failure as harvesting aggregator offers off unrelated products, made twice in two days. Any name search needs a category or brand constraint.
- The brand guard then threw away a genuine hit, because amazon.se titles Blitz Bahamut "Takara Tomy BK1-50I CX-13 Starter Bahamut Blitz" with no "beyblade" in it. A filter tight enough to exclude jewellery was tight enough to exclude a real product; the brand list has to cover the manufacturer's own name too.
- A config comment is not evidence. `B0DN6YLGRX  # Silver Wolf` had been carried for days; the product page says Sterling Wolf 3-80FB. Verify a label before building on it.
- Redaction that depends on an environment variable is not redaction. The dashboard stripped the postcode using $DELIVERY_POSTCODE, but cron does not source .env, so the guard was empty and personal data reached a page on public DNS. Verify the output, not the intent — and prefer patterns over values that may be absent.
- "and" is not a product join. Treating it as one classified every Amazon listing as a multi-item bundle, because marketing titles say "Top and Launcher ... Tops and Games" inside a SINGLE product's title. The effect was invisible: Amazon quietly vanished from the cross-store price comparison, so the scalp flag stopped appearing rather than erroring.
- A shell heredoc turned an intended `\b` into a literal 0x08 BACKSPACE inside a regex, so the pattern silently required a control character and matched nothing — the Amazon discovery table just rendered empty. grep displayed the line as if it were correct. Second invisible-character injection in this repo after the NUL byte; check with repr() and a byte scan, not by eye, and prefer a file-based patch over a heredoc for anything containing backslashes.
- The dashboard's discovery panel caught a REAL live failure on its first correct render: amazon.se discovery returning "0 new of 0 found" on roughly half its runs, because news-notifier's warm-up hits "Download is starting" and its open_market uses a bare page.goto. Stock Checker fixed exactly this bug (safe_goto plus replacing the page, not re-navigating it) and the fix was never ported back. Instrumentation that surfaces a problem nobody knew about is worth more than the feature it was built for.

