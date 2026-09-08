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
