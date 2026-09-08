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
