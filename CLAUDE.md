# Stock Checker — Project Guide for Claude

## What this project is

A Python tool that monitors product pages across multiple retailer websites
and notifies me (via Discord webhook) the moment an out-of-stock product
becomes available. Personal-use, but built cleanly enough to run unattended
on a VPS long-term.

Two run modes, same core logic:
- **Local dev**: a polling loop process that checks all enabled sites on an
  interval.
- **VPS/production**: a single-run script invoked by an external cron job
  (already set up on the user's VPS). No built-in scheduler needed for this
  mode — cron owns the timing.

## Core design principles (do not violate without discussing first)

1. **Pluggable site architecture.** New retailers must be addable by adding
   one new file, not by editing shared/core code. Every site checker
   implements the same small interface (see Architecture below). If you find
   yourself editing `core/` to support a new site, stop — that's a sign the
   interface is wrong, not that this site is special.
2. **Be a polite scraper.** This is personal use, but requests must stay
   reasonable: randomized delay between requests to the same site, a real
   User-Agent, no hammering a site faster than a human would refresh a page,
   and back off (don't retry-loop aggressively) on 429/403 responses. Never
   add a feature that increases request volume without flagging it to me
   first.
3. **Lean over clever.** Default to the simplest implementation that works.
   No abstraction, config option, or framework should be added until at
   least two real sites need it. Prefer a few straightforward functions over
   a deep class hierarchy. If an implementation feels bulky, stop and look
   for the smaller version before continuing.
4. **Secrets never get committed.** Discord webhook URL and any per-site
   credentials live in `.env` (gitignored), loaded via environment
   variables. Never hardcode a webhook URL or write one into a committed
   file, example config included.
5. **Question the approach before building.** For anything beyond a small
   fix (a new site checker, a new notification channel, a storage change),
   briefly state the approach and at least one alternative considered, and
   why, before writing code. Don't silently pick the first idea for
   non-trivial changes.

## Architecture

```
stock_checker/
  sites/              # one file per retailer, e.g. sites/example_store.py
    base.py           # SiteChecker interface all site modules implement
    __init__.py       # registry that discovers/loads enabled site modules
  core/
    scheduler.py       # polling loop for local dev mode
    notifier.py         # Discord webhook sending
    storage.py           # tracks last-known stock state per product (avoid duplicate alerts)
    http.py                # shared politeness layer: UA, rate limiting, backoff — obeyed by BOTH transports
  config/
    sites.yaml          # which sites/products are enabled, check interval, etc. (no secrets)
  state/                # runtime state, gitignored: one file per site
                        #   (amazon.json keys products as "<market>:<asin>"),
                        #   plus amazon_reference_prices.json and
                        #   amazon_delivery.json — long-lived knowledge kept
                        #   apart so clearing alert state destroys neither
  main.py                # entrypoint: single-run mode (for cron) — checks all enabled sites once
  run_loop.py            # entrypoint: polling-loop mode (for local dev)
  tests/
  .env.example           # documents required env vars, no real values
  requirements.txt
```

Each site module in `sites/` implements `check(product_config) -> StockResult`:

```python
@dataclass
class StockResult:
    in_stock: bool              # purchasable OR pre-orderable
    product_name: str
    url: str
    price_text: str | None      # as displayed, e.g. "68.63 EUR"
    price_value: float | None   # normalized, for comparison
    currency: str | None
    seller: str | None          # context in the alert, never a gate
    delivery_date: str | None   # when it would arrive, as displayed
    alertable: bool = True      # site may veto; core does not ask why
    alert_reason: str | None    # site may REQUEST an alert, stating why
    notes: list[str] = field(default_factory=list)
```

`core/storage.py` decides whether a result is "new" (worth notifying
about) vs. unchanged — site modules must not implement their own
duplicate-suppression logic. `alertable` is a *separate* gate: it lets a
site veto a notification for a site-specific reason (Amazon's scalp
detection is the only current user) without `core/` learning what that
reason is. Keeping the two distinct is deliberate — collapsing them drags
site-specific policy into shared code, which is the smell principle 1
warns about.

(This structure is the intended target — if the current folder doesn't
match it yet, that's expected early on. Build toward it, and update this
section once the real structure diverges intentionally.)

## Conventions

- Python 3.11+, type hints on all function signatures.
- Four transports, all first-class, chosen per site, cheapest first:
  - **JSON API** (`httpx`, sync) wherever a site exposes one — always
    prefer it. Shopify stores do: `/products.json?limit=250` returns
    `available`, `price` and `compare_at_price` per variant, so there is
    nothing to parse and no browser to run.
  - **HTTP + HTML** (`httpx` + BeautifulSoup) where a site renders what we
    need server-side but exposes no usable API. gameshop.se is WooCommerce:
    its Store API answers 403 (Cloudflare) and the WordPress core API carries
    no price or stock, but each product tile in the listing HTML is stamped
    `instock`/`outofstock` by the shop itself — as good a signal as Shopify's
    `available`, and no browser required.
  - **A site's own JSON endpoint**, when the page renders client-side and
    watching the network finds one. ginza.se's search ships 413KB of HTML with
    zero product links; `/api/Apptus/Search` returns the whole catalogue in a
    single call. Check for this BEFORE reaching for a browser — the difference
    is one request versus one page-load per product.
  - **Browser** (Playwright) only where the data is injected client-side.
    Amazon is confirmed to need it: delivery, seller and price blocks all
    arrive after `domcontentloaded`. This is the expensive one — reach for it
    last, and only after checking whether the server-rendered HTML already
    answers the question.
  BeautifulSoup is for browser-rendered HTML, not a default. Whichever
  transport a site uses it must obey `core/http.py`'s politeness policy
  (UA, delay, backoff) — `core/http.py` owns that policy for both, while
  Chromium itself lives in the site module that needs it. Don't make raw
  `requests`/`httpx` calls inside a site module.
- Config over code: which products/sites are checked and how often lives in
  `config/sites.yaml`, not hardcoded in Python.
- Errors must never cascade — catch and log per-site AND per-product. A
  failure on one product must not kill the rest of that site's run.
  (Inherited from news-notifier, where one aborted navigation left the page
  mid-transition and became "navigation interrupted" for every subsequent
  product in that run.)
- Tests live in `tests/`, mirroring the `sites/`/`core/` structure. New site
  modules should get at least a basic parsing test against a saved fixture
  — HTML for browser sites, JSON for API sites (don't hit the live site in
  tests).

`SiteChecker` also carries `state_dir` (for a site's own auxiliary state,
which today means Amazon's reference prices) and `errors`, a count of what
the run failed to see. A non-zero `errors` means the results are an
INCOMPLETE view, and `main.py` skips pruning state in that case — pruning
against a partial view deletes products that were merely missed, and they
then re-alert as new when they reappear.

Browser transports obey the same politeness policy as HTTP ones via
`core/http.PoliteClient.pacer`: a Playwright navigation is a request to the
site like any other.

## Workflow expectations

- Implement, then self-review before calling something done: check for bugs,
  unnecessary complexity, and whether the interface in Architecture was
  actually followed.
- After any nontrivial change, run existing tests (or the relevant script
  manually) rather than assuming it works.
- A `PostToolUse` hook (`.claude/settings.json` -> `.claude/hooks/run_tests.py`)
  runs the suite after any `.py` edit in this project and reports failures
  back, so a regression surfaces on the edit that caused it. It fails open —
  no venv, unparseable payload, or a file outside the project all exit
  silently — so it can never block work. It is a safety net, not a substitute
  for running tests deliberately after a nontrivial change.
- Run `/code-review` on the diff before calling a nontrivial piece done —
  without being asked. Self-review shares the session's own assumptions; a
  review pass is what catches what I already talked myself into. (The
  deeper `/code-review ultra` can only be launched by the user.)
- Verification has three tiers and ALL THREE are available. Use the
  cheapest one that answers the question:
  1. **Fixture tests** — offline, deterministic, catch regressions in our
     own parsing. Never hit a live site here.
  2. **Live run from the dev machine** — verified 2026-09-08 against both
     Shopify stores AND amazon.de: a plain HTTPS GET of
     `/-/en/dp/<asin>` returned `lang="en-gb"`, no CAPTCHA, with seller,
     `#availability`, the delivery block and the price all present in the
     RAW server HTML. This is the normal way to debug a parsing change.
  3. **Live run on the VPS** — reachable over key-based SSH, so production
     behaviour can be checked directly rather than inferred. ALWAYS
     dry-run first (omit `--send-discord`) so debugging cannot fire real
     alerts, and do not casually overwrite production state.
  news-notifier's CLAUDE.md says Amazon is unreachable from a sandboxed
  session. That described ITS environment, not this one — do not inherit
  the claim.
- One successful live request is NOT proof of sustained access. Amazon
  rate-limits and serves CAPTCHAs under volume, and a home IP behaves
  differently from a datacenter one. Treat repeated live runs as a real
  cost and prefer fixtures for iteration.
- If a mistake gets caught and corrected during a session (a bug, a bad
  assumption, a design decision that didn't pan out), record it in
  `LESSONS.md` in one or two lines — see below.

## VPS access

The production VPS is reachable over key-based SSH with no password prompt.
An SSH config alias lives in WSL at `/home/kali/.ssh/config`, so commands
are written as:

    wsl.exe -d kali-linux -- ssh vps '<command>'

Verified: git operations, log reads, and file writes under `/root` all work.
Two repos live there — `/root/news-notifier` (the pilot) and
`/root/stock-checker` (this project).

Deployed 2026-09-08 to `/root/stock-checker`, cron-only (no systemd — cron
owns the timing and there is nothing to keep alive). `deploy/README.md` has
the full picture; the short version:

- One job at `:15/:45`, clear of news-notifier's Playwright jobs at `:00/:30`
  and `:10/:40` so two Chromium instances never start together.
- **Live and sending** since 2026-09-08. The pilot's `track_delivery_multi.py`
  was retired at the same time, because both watch the same ASINs and both
  sending would double-alert from diverging state; `deploy/status.sh` has an
  explicit conflict check for exactly that. The pilot's catalog scraper and
  hypixel job still run — this project replaces neither. Crontab backup at
  `/root/crontab.backup.pre-golive`.
- `deploy/setup.sh` MERGES its cron entries into the existing crontab. Never
  make it replace: news-notifier's live jobs share that crontab.
- The VPS is a clone of https://github.com/nytemartO24/Stock-Checker (public,
  because the box has no git credentials at all). Updating is
  `cd /root/stock-checker && git pull && ./deploy/setup.sh`. `.env`, `state/`
  and `.venv/` are gitignored, so a pull never touches the webhook, the
  accumulated reference prices, or the browser.

Notes for whoever runs this next:
- The key lives only in WSL (`/home/kali/.ssh/id_ed25519`), not on the
  Windows side. Copying it out is blocked by the permission classifier and
  isn't needed — the alias above is enough.
- Git Bash rewrites Linux-looking paths passed through `wsl.exe` — including
  paths meant for the REMOTE host, not just WSL-side ones. Prefix with
  `MSYS_NO_PATHCONV=1` whenever an absolute Linux path is an argument, or
  `ssh vps '/root/stock-checker/deploy/status.sh'` fails with
  `C:/Program: No such file or directory`.
- `wsl.exe` output can carry NUL bytes; pipe through `tr -d '\0'` when the
  result looks mangled.
- ALWAYS dry-run against production first (omit `--send-discord`), and
  don't `git pull` or overwrite state on the VPS without being asked — it
  is running live and its state files are not in git.

## Working with the user

Kept here rather than only in Claude's per-project memory, because memory is
keyed to the directory a session STARTS in and so does not survive the
project being opened from a different folder. This file travels with the
repo, including to the VPS clone.

- **Work autonomously.** Make routine engineering calls without asking. Run
  verification (tests, a live run, `/code-review`) as part of finishing, not
  as an optional extra, and report what it found — including your own
  mistakes, which is the expected standard here, not a failure.
- **Escalate instead of grinding.** If something isn't working, say so
  rather than burning tokens on retries: "notify me rather than bashing your
  head for too long and wasting tokens." Timebox open-ended investigations,
  state the box out loud, and report the result either way. The user often
  holds context that resolves it in one sentence.
- **Check proven code before experimenting.** When an experiment's result
  would contradict code that demonstrably works in production, the
  experiment is probably wrong — read that code's rationale first. This
  session lost a detour to probing Amazon without the session warm-up that
  `news-notifier`'s `browser.py` already documented.
- **Watch the budget.** This is a personal project on a metered plan.
  Prefer the cheapest verification that answers the question, and prefer
  finishing one thing to starting three.

## Self-updating this file

This CLAUDE.md is expected to evolve as the project does. Whenever a
session produces a durable decision, correction, or convention that isn't
already captured here — a new site turns out to need special handling, a
performance issue changes how checks run, a "we tried X, it didn't work,
do Y instead" moment — update the relevant section of this file directly
as part of that session's work, not just in conversation. Keep additions
short and concrete; prune or rewrite sections that go stale rather than
letting the file grow indefinitely. If unsure whether a change to this file
is warranted, ask before committing to it.

Also maintain `LESSONS.md` alongside this file as an append-only log of
specific mistakes and their fixes (one entry per line/bullet, newest last).
Periodically (when it gets long, or when asked), distill recurring
lessons from it into a proper convention here in CLAUDE.md, and trim the
log.

## Sites

**Shopify (`sites/shopify.py`)** — one module for every Shopify store,
parameterized by domain + collections in `sites.yaml`: `popsplanet.it`
(booster / starter-pack / double-pack collections) and `toysnowman.com`.
`/products.json?limit=250` gives `available` (authoritative, and it already
covers pre-orders), `price`, `compare_at_price` and `sku` per variant.

**Always pin `?country=` — this is load-bearing.** A Shopify Markets store
prices in the VISITOR's country, and nothing in the payload says which
currency you got. Measured 2026-09-08 on toysnowman: the same URL returned
`25.99` bare and `185.00` when any `Accept-Language` header was sent (the
same price in SEK). `country=` overrides that guessing, so `currency` in
`sites.yaml` becomes a fact rather than an assumption. Both stores are
pinned to `SE`, which means every price this project reports is what it
costs delivered to Sweden — comparable across sites and against Amazon's
SEK figures, and it removes the need for FX conversion on the Shopify side
entirely.

`compare_at_price` is not reliably an RRP (some items have it equal to
`price`), so don't treat it as one.

**Amazon (`sites/amazon/`)** — a package rather than one file: the browser
plumbing, the market table and the price/reference logic are each
substantial and independently testable. Still one unit per retailer.
Playwright, ported from news-notifier's
`pilot/eu_multimarket/`. Market is config, not a separate module. Carried
over from that project: the `/-/en/` URL override with merged
English+native month tables, delivery-location pinning (postcode on the
domestic market, country picker elsewhere, no cross-fallback), scoped
extraction (never a whole-page regex), and the named outcome taxonomy in
which UNKNOWN means "we did not understand this page", not "no stock".

### What was harvested from news-notifier (audited 2026-09-09)

A line-by-line diff of this module against
`news-notifier/pilot/eu_multimarket/`. Recorded so the comparison is not
redone, and so nothing here gets "simplified" back out.

**Adopted, because they fix real defects:**

- **Wait for the client-side blocks before reading the page.**
  `CONTENT_SELECTORS` is awaited for up to 6s before `page.content()`. Amazon
  injects delivery, seller and price AFTER `domcontentloaded` — confirmed
  against a real `.de` page whose raw server HTML had none of them despite
  being a normal purchasable listing. Reading immediately is a race, and
  losing it looks exactly like "this product has no date": a confidently wrong
  answer, not a visible failure. This was missing here and very likely caused
  some of the `no date` results in the first overnight run.
- **`channel="chromium"`** on launch, which news-notifier ran on for months.
  It is a different binary from Playwright's bundled build, so navigation and
  download-prompt behaviour can genuinely differ. Wrapped in a fallback: if
  that channel is not installed, launching would fail and take the whole
  market with it, which is worse than any behavioural difference.

**Deliberately NOT adopted, with reasons:**

- Its `no_date_signals` ("release date has not been announced", "coming
  soon"). It needed them to distinguish NO DATE YET from UNKNOWN in its
  outcome taxonomy. Here a product with no delivery block simply has
  `delivery_date=None`, saying the same thing. They were briefly ported, never
  read, and have been removed — config that looks live but is never used is
  worse than absent.
- Its NO OFFER case ("See All Buying Options" with no add-to-cart). Already
  covered: no buyable button means `in_stock=False`.
- Its `describe_selector` / page-kind diagnostics (ABSENT vs PRESENT BUT
  EMPTY, the client-side-injection signature). Genuinely useful for debugging
  a parse failure, but the content wait above should remove most of them.
  Worth revisiting if `no date` shows up on a listing that visibly has one.

**Where this project is now AHEAD of news-notifier — do not "restore" these:**

- **The warm-up goes through `safe_goto`.** news-notifier's `open_market` uses
  a bare `page.goto`, so a download prompt during warm-up loses the whole
  market. Its `safe_goto` comment says every navigation must go through it;
  its own warm-up does not.
- **Warm-up failure is recovered by replacing the PAGE**, not re-navigating
  it. A download-aborted navigation leaves the page at
  `chrome-error://chromewebdata/`, and re-issuing the goto on that same page
  lands there again — 6 of 36 overnight runs failed that way, all attempts
  together. A fresh page in the same context keeps the warmed-up cookies.
- **The location modal is awaited by its contents**, not a fixed 1500ms.
  `de` and `es` each failed once overnight with "no country picker".
- **Delivery dates are compared as RESOLVED dates, not by re-parsing the
  display string.** This is the important one. Amazon shows dates without a
  year, and the parser assumes next year for one already past — so a stored
  baseline of "22 September" silently became September NEXT year the moment
  that day went by, ~350 days out, and every real date then looked like an
  enormous improvement: a nonsense "moved earlier: 22 September -> 5 October",
  guaranteed on a schedule. news-notifier screens stored dates by
  plausibility, which CANNOT catch this — 350 days is inside the 400-day
  window. `DeliveryState` stores `date_iso`/`alerted_iso` and compares those.
  An entry without them (written by an older version) re-anchors quietly
  rather than firing.

### The cross-site naming problem (unsolved)

**The same product has a different NAME at every retailer**, and not as a
word-order variation — a genuinely different name. Scale Shark 4-50UF is:

| store | title |
|---|---|
| popsplanet | Beyblade X - Booster: Scale Shark 4-50UF |
| toysnowman | Beyblade X Scale Shark 4-50UF UX Booster Pack |
| gameshop | Beyblade X Scale Shark 4-50Uf (Attack) |
| **ginza** | **BEYBLADE Bbx Kobuk Valley** |

Ginza uses Takara Tomy naming; the others use Hasbro's. Its titles say "BBX",
never "Beyblade X", so a search for the latter returns nothing there. This is
why every watchlist is per-store and made of that store's own exact
identifier (Shopify handle, WooCommerce slug, Ginza numeric id) — a shared
list is not merely inconvenient, it is impossible.

**What would actually solve it: EAN/GTIN.** Ginza's product pages carry one
(`5010996385222` on the Kobuk Valley page) — the manufacturer's barcode,
identical at every retailer on earth. Shopify's public products.json exposes
`sku` but not `barcode`; WooCommerce listings expose neither, though product
pages often do. So a cross-store identity map is possible but costs one
product-page fetch per item per store, and has not been built. This is the
open route to "am I missing stock purely because of naming", and the thing to
design before the watchlists grow.

### Adding a store

```bash
python scripts/audit_store.py <any URL on the store> --match beyblade
```

It reports whether the store is Shopify, every collection matching the
keyword with **sample titles**, the currency the market resolves to, and a
ready-to-paste `sites.yaml` block. Re-run with
`--collections a,b,c` once you have decided which to keep.

**It does not choose for you, and must not start doing so.** A store groups
by its own logic: popsplanet files anime merchandise and launcher
accessories under "beyblade" next to the actual toys, so an earlier version
that maximised product count "found" 38 extra products that were
deliberately excluded. Coverage is not the goal — the user's filter is.
Overlap between collections is reported as advisory only (a fully-covered
collection costs one request per run and adds nothing, since the checker
dedupes by handle), but whether to include one is a judgement about content.

Currently tracked: popsplanet's `beyblade-x-booster` / `-starter-pack` /
`-double-pack` (102 products, EUR), toysnowman's `beyblade` (59 after
excluding Beyblade Burst, SEK), and gameshop.se via the WooCommerce module
(131 after the same exclusion, SEK). 16 watchlisted items across the three.
A store that is not Shopify needs its own module — `audit_store.py` only
probes Shopify, and says so when a store is not.

**Watchlist vs. tracking — keep these separate.** A Shopify collection
arrives in ONE request, so tracking every product in it is free and worth
doing: it builds price history and gives each product a `first_seen` date.
What the per-site `watchlist` controls is only whether a restock may
INTERRUPT you, via `StockResult.alertable`. With 161 products tracked,
alerting on all of them is noise; the watchlist is how that gets quiet
without giving up the data. Empty watchlist = alert on everything.

Entries are exact product HANDLES, not title terms. Handles are Shopify's
equivalent of an ASIN — exact, unique, and PER STORE, so unlike one ASIN
covering every Amazon market, each store needs its own list. Title matching
was tried and dropped: names are not word-order stable (the item quoted as
"Shark Scale" is titled "Scale Shark 4-50UF"), and a term also matches
multi-item bundles containing the product. Decisive case — popsplanet's
Delta Unicorn has the handle
`beyblade-x-starter-pack-guadalupe-mountains`, containing no part of the
product name, so a handle can only ever be looked up, never derived.

Look one up with `scripts/audit_store.py <store> --find "<term>"`. A
watchlist handle matching no product is reported at the end of a clean run,
because a typo fails silently — you simply never hear about that item again.

Bundles count. At toysnowman, Tread Croc exists ONLY inside a four-item
bundle, and one such bundle went by unnoticed; both bundles are watchlisted
on purpose.

`title_include` / `title_exclude` answer a different question — is this
product in scope to track at all — and are for excluding whole product
lines a store mixes into one collection.

New products need no special handling for alerting: an unseen handle has no
stored state, so a watchlisted one alerts the first time it appears in
stock.

### Delivery dates (Amazon only)

A long estimate is itself a form of unavailability: an add-to-cart button and
a date six months out is not something you can have. So the date is part of
the availability answer, not decoration.

- Extraction is scoped to a matched delivery container, NEVER the whole page.
  A page-wide search matches "Reviewed in Spain on 21 January 2019", and
  because that carries an explicit year the assume-next-year correction never
  fires, so a stale unrelated date sails through looking real.
- Extracted dates are range-checked (0..400 days) before being stored. A bogus
  date is worse than none: it becomes the baseline a future alert fires
  against.
- **The baseline is the date last ALERTED about, not last seen.** Comparing
  against the last reading fails two ways and Amazon does both: a date
  flickering between 22 and 23 February pings every time, and a date creeping
  earlier one day at a time never pings at all because no single step clears
  `min_improvement_days`. Anchoring on the last alerted date lets small moves
  accumulate, then re-anchors. Slipping later re-anchors silently — you are
  not pinged for bad news, but future improvements are judged against what is
  actually promised now.
- `max_delivery_days` (90) treats a further-out estimate as NOT in stock. It
  sets `in_stock` false rather than muting the alert, and that choice is the
  mechanism: the estimate later coming inside the window then reads as an
  ordinary restock, so you are told when the item becomes actually available.
  Muting via `alertable` would be worse twice over — it would also gag the
  date-moved-earlier alert, the very signal that matters. Nothing is hidden:
  the alert says it was orderable but too far out, and names the date.

`StockResult.alert_reason` is how a site REQUESTS an alert core cannot judge —
the mirror of `alertable` letting it veto one. A date moving earlier is not a
stock transition, so no rule in `core/storage.py` could ever surface it.
`alert_kind` orders these as new -> veto -> site -> restock, so a suppressed
listing cannot reach you by the side door of a date change.

### In stock, and the scalper problem

"In stock" means **available for purchase or pre-order**. On Shopify that is
just `variant.available`. Amazon is the only site with multiple vendors per
listing, so it is the only one needing price defence — do not generalise
this logic to other sites.

Seller identity is NOT the discriminator. From news-notifier's real data:
third-party seller Weltstore quoted 21.69 EUR / 247 SEK consistently across
markets (fair), while `is_amazon_seller` returns True for "Amazon UK".
Price against a reference is the test; the seller name is context in the
alert.

**Reference price = the lowest FX-normalized price ever observed from an
Amazon-sold offer**, persisted in `state/amazon_reference_prices.json`,
kept out of per-market alert state so a state reset does not destroy it.
Flag anything above `reference * scalp_multiplier` (default 2.0, set in
`sites.yaml`). Scalping on this line runs 4-5x — the motivating example was
68.63 EUR against a 12.99 EUR street price — so threshold precision matters
far less than having a trustworthy reference.

- A flagged listing is still `in_stock=True` (it genuinely is purchasable),
  carrying an explanatory note. `alert_on_suspected_scalp: true` in
  `sites.yaml` tags it; set it false to suppress the notification.
  Detection, logging and state recording continue either way.
- **No per-ASIN reference -> fall back to a title-derived tier ceiling**
  (`sites/amazon/tiers.py`). This is what makes the scalper case judgeable
  at all: a product whose only sightings anywhere are third-party never
  earns an Amazon-sold reference, so without this it could only ever be
  reported as "price unverified". Beyblade X titles are regular enough to
  classify — the key insight is that expensive items are MULTI-ITEM
  BUNDLES, spotted by counting the `&` / ` e ` / ` vs. ` joins, without
  which the `starter` tier spanned 93-605 SEK and was useless.
  The ceilings are calibrated from 162 real retail products and validated
  to produce ZERO false positives across them; 3 remain unclassifiable and
  correctly fall through to "unverified". They are CEILINGS, not medians —
  weaker evidence must not cry wolf on a legitimately pricier item.
- **Still no reference and no tier -> alert anyway, labelled.** Silently
  suppressing a real restock is the dangerous failure; a false positive
  costs nothing.
- Cold-start poisoning is the real trap: if a product's first sighting IS
  the scalp price, that becomes the reference. So only ever record a
  reference from an Amazon-sold offer, keep it provisional (and say so)
  until corroborated, and allow an optional `max_price_sek` override in
  `sites.yaml`. That override is deliberately NOT required up front —
  auto-reference first, hand-set ceilings only where it misbehaves.
- **Corroboration is counted in DISTINCT prices, not sightings.** At a
  half-hourly cadence, re-reading the same unchanged price would clear the
  provisional flag within an hour while confirming nothing. A reference
  stops being provisional only once a second, different Amazon-sold price
  has been seen.
- An **unpinned delivery location** makes a market's results describe
  wherever Amazon guessed. That marks the run incomplete (so state is not
  pruned against it) and adds a note to every alert from that market,
  rather than being only a log line nothing acts on.
- Seed reference prices for currently-tracked ASINs from news-notifier's
  existing per-market state to skip the cold-start window. That seed
  correctly ignores the scalped example, whose only observation is
  third-party.
- **Never assume a currency — read it off the page.** With delivery pinned
  to Sweden, amazon.de quotes `SEK766.25`, not euros, so a per-market
  currency table would convert an already-SEK figure as though it were EUR
  and overstate it ~11x, flagging every cross-border listing as a scalp.
  `prices.detect_currency()` reads the symbol/code from the price string and
  falls back to the market default only when the page says nothing. This is
  the same mistake as trusting a Shopify store's configured currency; it has
  now cost us twice.
- FX normalization to SEK uses a static table in `prices.py`. Deliberately
  approximate: this needs 2x discrimination, not accounting accuracy, and a
  static table cannot fail a run the way a live rate lookup could.

## Open questions / not yet decided

- Does Amazon actually need Playwright? Evidence so far says **maybe not**,
  but the blocker is delivery-location pinning, not page fetching.
  Measured 2026-09-08 with plain HTTP + a cookie jar, warming up on the
  `/-/en/` homepage first exactly as `browser.py:open_market()` does:
  6/6 real product pages, 6/6 `lang="en-gb"`, 6/6 resolved to a definite
  state, and the known third-party listing returned its seller and price
  (`London Lane Company`, EUR 68.63) matching stored pilot state. So the
  page content is server-rendered and reachable without a browser.
  What is NOT yet solved without a browser is `set_delivery_location()`:
  the pilot drives Amazon's glow modal (postcode fields / country
  `<select>`), and a delivery date is meaningless without a pinned
  destination. Replicating that over plain HTTP means reproducing whatever
  cookie or form POST the modal performs — plausible, unproven, and the
  thing to settle before dropping Playwright. Until then the Amazon module
  stays browser-based as designed.
  **The warm-up is mandatory either way**: a cookieless request to
  `/-/en/dp/<asin>` gets served the market's NATIVE layout, where none of
  the pilot's selectors match. That looks exactly like being blocked and
  is not.
- Cross-site RRP matching as a scalp reference — using the Shopify stores'
  prices as an anchor for Amazon. Deliberately deferred: coverage is
  partial (some tracked ASINs are not stocked there at all) and
  title-to-ASIN matching is fuzzy. Revisit only if auto-reference proves
  insufficient.

(Resolved: retailer list — see Sites. Per-site rate-limit overrides — yes,
needed now, not deferred: one Shopify JSON request per collection and one
browser page-load per ASIN per market cannot share a global limit.)
