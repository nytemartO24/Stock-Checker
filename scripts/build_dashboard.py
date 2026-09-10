#!/usr/bin/env python3
"""Render a single self-contained HTML dashboard from state, config and logs.

WHY STATIC: this project is cron-only — nothing is kept alive, because there is
nothing that needs to be. A generated file served by the Caddy already running on
the box adds no daemon, no dependency and no attack surface beyond the basic_auth
in front of it. A Flask app would buy interactivity we cannot yet specify; build
that once the panels below prove which actions are actually wanted.

WHAT IT IS FOR: the panels are chosen to cover what an automated checker
structurally CANNOT notice, each one earned by something that already went wrong:

  * a new product pinged once and was never watchlisted (Whip Brachio);
  * a product in stock since the first run can never produce a transition, so it
    never alerts however much you want it (the bundle bought by hand);
  * a watchlist entry that matches nothing fails SILENTLY — you simply stop
    hearing about that item;
  * a catalogue quietly shrinking (gameshop 131 -> 98) looks identical to a
    store removing stock;
  * a scalped listing sits there "in stock" at 4x the price (Sterling Wolf at
    EUR 60.18) and no rule fires because nothing transitioned.

Reads only local files and makes NO network requests, so it can run right after
the checker without touching a site.

    python scripts/build_dashboard.py --out /var/www/stock-checker/index.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

from sites.amazon.prices import to_sek  # noqa: E402

# Reused rather than reimplemented: the dashboard's coverage panel must agree
# with the resolver that produced the watchlists, or it becomes its own source
# of truth and starts lying.
import resolve_watchlist as resolver  # noqa: E402

NEW_DAYS = 14          # "recently appeared" for the triage queue
STALE_HOURS = 6        # a product not seen this recently is suspicious
LOG_RUNS = 60          # how many recent runs to chart

# The postcode identifies a town and the log prints it every run. This page sits
# on public DNS behind basic_auth; personal data does not belong on it at all.
POSTCODE_ENV = os.environ.get("DELIVERY_POSTCODE", "").strip()

# EVERY shape the destination appears in, because relying on the env var alone
# already failed: cron does not source .env, so POSTCODE_ENV was empty and the
# postcode reached a page on public DNS. Patterns are primary; the env value is
# belt-and-braces for a shape not yet seen.
REDACTIONS = (
    # [se] delivery location confirmed: 'Karlskrona 371 16'
    (re.compile(r"(delivery location confirmed:\s*)'[^']*'"), r"\1'[redacted]'"),
    # ... widget reads 'Karlskrona 371 16', wanted Sweden/37116
    (re.compile(r"(widget reads\s*)'[^']*'"), r"\1'[redacted]'"),
    (re.compile(r"(wanted\s+)[A-Za-z]+/\S+"), r"\1[redacted]"),
    # Any bare Swedish postcode, with or without its space.
    (re.compile(r"\b\d{3}\s?\d{2}\b(?=[^\d]|$)"), "[redacted]"),
)


def redact(text: str) -> str:
    """Strip the delivery destination from anything rendered.

    The postcode identifies a town. This page is on public DNS behind basic
    auth; personal data should not be on it at all, auth or no auth.
    """
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    if POSTCODE_ENV:
        text = text.replace(POSTCODE_ENV, "[redacted]")
        text = text.replace(POSTCODE_ENV.replace(" ", ""), "[redacted]")
    return text


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_state(state_dir: Path, site: str) -> dict:
    path = state_dir / f"{site}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = data.get("products", data) if isinstance(data, dict) else {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def age(stamp: datetime | None, now: datetime) -> str:
    if stamp is None:
        return "—"
    delta = now - stamp
    if delta < timedelta(minutes=90):
        return f"{int(delta.total_seconds() // 60)}m"
    if delta < timedelta(hours=48):
        return f"{int(delta.total_seconds() // 3600)}h"
    return f"{delta.days}d"


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

RUN_START = re.compile(r"^(\S+) \[INFO\] === START")
RUN_DONE = re.compile(r"^(\S+) \[INFO\] run complete: (\d+) alert\(s\) across (\d+) site\(s\), (\d+) failure")
SITE_LINE = re.compile(r"^(\S+) \[INFO\] \[([\w.-]+)\] (\d+) product\(s\), (\d+) in stock, (\d+) alert")
WARN_LINE = re.compile(r"^(\S+) \[(WARNING|ERROR)\] (.*)$")
ALERT_LINE = re.compile(r"^(\S+) \[INFO\] ALERT:\s*$")


def read_log(path: Path, tail_bytes: int = 3_000_000) -> dict:
    """Runs, per-site counts, warnings and alerts from the log's tail."""
    if not path.exists():
        return {"runs": [], "sites": defaultdict(list), "warnings": [], "alerts": []}
    with path.open("rb") as fh:
        try:
            fh.seek(-tail_bytes, os.SEEK_END)
            fh.readline()  # discard the partial line
        except OSError:
            fh.seek(0)
        lines = fh.read().decode("utf-8", "replace").splitlines()

    runs, warnings, alerts = [], [], []
    sites: dict[str, list] = defaultdict(list)
    pending_alert = None
    for line in lines:
        done = RUN_DONE.match(line)
        if done:
            runs.append({"at": done.group(1), "alerts": int(done.group(2)),
                         "sites": int(done.group(3)), "failures": int(done.group(4))})
            continue
        site = SITE_LINE.match(line)
        if site:
            sites[site.group(2)].append({
                "at": site.group(1), "products": int(site.group(3)),
                "in_stock": int(site.group(4)), "alerts": int(site.group(5))})
            continue
        warn = WARN_LINE.match(line)
        if warn:
            warnings.append({"at": warn.group(1), "level": warn.group(2),
                             "text": redact(warn.group(3))[:240]})
            continue
        if ALERT_LINE.match(line):
            pending_alert = {"at": ALERT_LINE.match(line).group(1), "body": []}
            alerts.append(pending_alert)
            continue
        if pending_alert is not None:
            if line.startswith(("2026-", "2027-")) or not line.strip():
                pending_alert = None
            elif len(pending_alert["body"]) < 3:
                pending_alert["body"].append(redact(line.strip())[:200])
    return {"runs": runs, "sites": sites, "warnings": warnings, "alerts": alerts}


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def table(headers: list[str], rows: list[list[str]], *, empty: str = "Nothing here.",
          classes: str = "") -> str:
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
                   for row in rows)
    return (f'<table class="{classes}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>")


def panel(title: str, why: str, content: str) -> str:
    return (f'<section><h2>{esc(title)}</h2>'
            f'<p class="why">{why}</p>{content}</section>')


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def link(url: str | None, text: str) -> str:
    if not url:
        return esc(text)
    return f'<a href="{esc(url)}" target="_blank" rel="noreferrer">{esc(text)}</a>'


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def panel_health(log: dict, now: datetime) -> str:
    runs = log["runs"][-LOG_RUNS:]
    last = runs[-1] if runs else None
    since_alert = 0
    for run in reversed(log["runs"]):
        if run["alerts"]:
            break
        since_alert += 1
    recent_failures = sum(r["failures"] for r in runs)
    last_at = parse_ts(last["at"]) if last else None
    stale = last_at is None or (now - last_at) > timedelta(hours=1)

    cards = [
        ("Last run", age(last_at, now) + " ago" if last_at else "never",
         "bad" if stale else "good"),
        ("Runs seen", str(len(log["runs"])), "neutral"),
        ("Runs since an alert", str(since_alert),
         "warn" if since_alert > 96 else "neutral"),
        ("Failures in last %d runs" % len(runs), str(recent_failures),
         "bad" if recent_failures else "good"),
    ]
    html_cards = "".join(
        f'<div class="card {kind}"><span class="k">{esc(k)}</span>'
        f'<span class="v">{esc(v)}</span></div>' for k, v, kind in cards)
    return (f'<section class="cards">{html_cards}</section>')


def panel_prices(sites_cfg: dict, states: dict, wanted: list[str], now: datetime) -> str:
    """Every wanted product, at every store that has it, normalised to SEK.

    This is both the shopping view and the scalp detector: the same product side
    by side across stores makes a 4x listing obvious, which no single-store rule
    can see. Sterling Wolf sat in stock at EUR 60.18 against ~130 kr elsewhere.
    """
    catalogues = {site: {pid: (row.get("name") or "")
                         for pid, row in state.items()}
                  for site, state in states.items()}
    rows = []
    for name in wanted:
        learned = resolver.learn_codes(catalogues, name, wanted)
        offers = []
        for site, state in states.items():
            singles, _ = resolver.match(catalogues[site], name, wanted, learned)
            for pid, _title in singles:
                row = state[pid]
                sek = to_sek(row.get("price_value"), row.get("currency"))
                offers.append((sek, site, pid, row))
        if not offers:
            rows.append([f'<strong>{esc(name)}</strong>',
                         pill("nowhere", "bad"), "—", "—", "—"])
            continue
        priced = [o for o in offers if o[0]]
        cheapest = min((o[0] for o in priced), default=None)
        in_stock = [o for o in offers if o[3].get("in_stock")]
        for sek, site, pid, row in sorted(offers, key=lambda o: (o[0] is None, o[0] or 0)):
            flags = []
            if row.get("in_stock"):
                flags.append(pill("IN STOCK", "good"))
            if sek and cheapest and sek > cheapest * 2:
                # Cross-store outlier: the only scalp signal that works without
                # price history, and it caught the real one.
                flags.append(pill(f"{sek / cheapest:.1f}x cheapest", "bad"))
            seen = parse_ts(row.get("last_seen"))
            if seen and (now - seen) > timedelta(hours=STALE_HOURS):
                flags.append(pill(f"stale {age(seen, now)}", "warn"))
            rows.append([
                f'<strong>{esc(name)}</strong>' if (sek, site, pid, row) == sorted(
                    offers, key=lambda o: (o[0] is None, o[0] or 0))[0] else "",
                esc(site),
                link(row.get("url"), row.get("price_text") or "—")
                + (f' <span class="dim">≈{sek:,.0f} kr</span>' if sek else ""),
                " ".join(flags) or "—",
                esc((row.get("name") or "")[:58]),
            ])
        if not in_stock:
            rows.append(["", "", "", pill("none in stock anywhere", "warn"), ""])
    return table(["Wanted", "Store", "Price", "Flags", "Listed as"], rows,
                 classes="prices")


def panel_triage(sites_cfg: dict, states: dict, now: datetime) -> str:
    """Products that appeared recently and are NOT watchlisted.

    The gap that let Whip Brachio slip: a new product alerts exactly once, and
    if nobody acts on that ping it never speaks again. This is the queue of
    decisions only a human can make.
    """
    rows = []
    cutoff = now - timedelta(days=NEW_DAYS)
    for site, state in states.items():
        watch = {str(w).strip() for w in (sites_cfg.get(site, {}).get("watchlist") or [])}
        for pid, row in state.items():
            key = pid.split(":", 1)[1] if ":" in pid else pid
            if key in watch or pid in watch:
                continue
            first = parse_ts(row.get("first_seen"))
            if not first or first < cutoff:
                continue
            rows.append([
                esc(age(first, now) + " ago"),
                esc(site),
                pill("IN STOCK", "good") if row.get("in_stock") else pill("out", "neutral"),
                link(row.get("url"), (row.get("name") or pid)[:64]),
                esc(row.get("price_text") or "—"),
                f'<code>{esc(key)}</code>',
            ])
    rows.sort(key=lambda r: r[0])
    return table(["Appeared", "Store", "Stock", "Product", "Price",
                  "watchlist entry to copy"], rows,
                 empty=f"No unwatchlisted products in the last {NEW_DAYS} days.")


def panel_forever(states: dict, now: datetime) -> str:
    """In stock continuously since we first saw them — these can NEVER alert.

    No transition means no restock event, so however much you want one of these
    the checker will stay silent. Only a human reading a list can catch it.
    """
    rows = []
    for site, state in states.items():
        for pid, row in state.items():
            if not row.get("in_stock"):
                continue
            first = parse_ts(row.get("first_seen"))
            if not first or (now - first) < timedelta(days=1):
                continue
            rows.append((now - first, [
                esc(f"{(now - first).days}d"),
                esc(site),
                link(row.get("url"), (row.get("name") or pid)[:64]),
                esc(row.get("price_text") or "—"),
            ]))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return table(["In stock for", "Store", "Product", "Price"],
                 [r for _, r in rows[:40]],
                 empty="Nothing has been continuously in stock.")


def panel_watchlist_integrity(sites_cfg: dict, states: dict, now: datetime) -> str:
    """Watchlist entries that match no product, and stale matches.

    A typo or a delisting is silent: the item simply never alerts again. The
    checker logs one warning per run; nobody reads logs, which is the point of
    this page.
    """
    rows = []
    for site, cfg in sites_cfg.items():
        if not cfg.get("enabled"):
            continue
        state = states.get(site, {})
        keys = {(pid.split(":", 1)[1] if ":" in pid else pid) for pid in state}
        for entry in (cfg.get("watchlist") or []):
            entry = str(entry).strip()
            matched = entry in keys
            if matched:
                seen = [parse_ts(row.get("last_seen")) for pid, row in state.items()
                        if (pid.split(":", 1)[1] if ":" in pid else pid) == entry]
                newest = max((s for s in seen if s), default=None)
                if newest and (now - newest) < timedelta(hours=STALE_HOURS):
                    continue
                rows.append([esc(site), f'<code>{esc(entry[:60])}</code>',
                             pill(f"last seen {age(newest, now)} ago", "warn")])
            else:
                rows.append([esc(site), f'<code>{esc(entry[:60])}</code>',
                             pill("MATCHES NOTHING", "bad")])
    return table(["Store", "Watchlist entry", "Problem"], rows,
                 empty="Every watchlist entry matched a product in the latest run.")


def panel_sites(log: dict, sites_cfg: dict, states: dict, now: datetime) -> str:
    """Catalogue size per site, and whether it is drifting.

    gameshop went 131 -> 98 products. Store change or a parser quietly losing
    rows? Slow drift is invisible to the checker and obvious in a column.
    """
    rows = []
    for site in sorted(sites_cfg):
        if not sites_cfg[site].get("enabled"):
            continue
        history = log["sites"].get(site, [])[-LOG_RUNS:]
        latest = history[-1] if history else None
        counts = [h["products"] for h in history]
        first_count = counts[0] if counts else None
        drift = ""
        if first_count and latest and latest["products"] != first_count:
            delta = latest["products"] - first_count
            drift = pill(f"{delta:+d} over {len(counts)} runs",
                         "warn" if abs(delta) > max(3, first_count * 0.1) else "neutral")
        spark = ",".join(str(c) for c in counts[-24:])
        rows.append([
            esc(site),
            esc(latest["products"] if latest else "—"),
            esc(latest["in_stock"] if latest else "—"),
            esc(len(sites_cfg[site].get("watchlist") or []) or "all"),
            drift or "steady",
            f'<span class="spark" data-values="{esc(spark)}"></span>',
        ])
    return table(["Site", "Products", "In stock", "Watchlisted", "Catalogue drift",
                  "Last 24 runs"], rows)


def panel_coverage(states: dict, wanted: list[str]) -> str:
    """Which wanted products have no identifier anywhere.

    Ring Aether and Blitz Bahamut are stocked nowhere we track, so no watchlist
    can hold them and only new-product discovery will ever surface them. Worth
    seeing, so their silence is not mistaken for tracking.
    """
    catalogues = {site: {pid: (row.get("name") or "") for pid, row in state.items()}
                  for site, state in states.items()}
    rows = []
    for name in wanted:
        learned = resolver.learn_codes(catalogues, name, wanted)
        hits, bundles = [], []
        for site in catalogues:
            singles, bundle = resolver.match(catalogues[site], name, wanted, learned)
            hits += [site] * len(singles)
            bundles += [site] * len(bundle)
        rows.append([
            esc(name),
            esc(", ".join(sorted(set(hits))) or "—"),
            esc(", ".join(sorted(set(bundles))) or "—"),
            pill("NOT STOCKED ANYWHERE", "bad") if not hits and not bundles
            else pill(f"{len(set(hits))} store(s)", "good" if hits else "warn"),
        ])
    return table(["Wanted product", "Sold as a single at", "Only in a bundle at",
                  "Coverage"], rows)


def panel_alerts(log: dict, now: datetime) -> str:
    rows = []
    for alert in reversed(log["alerts"][-40:]):
        stamp = parse_ts(alert["at"])
        body = " · ".join(alert["body"]) or "—"
        rows.append([esc(age(stamp, now) + " ago"), esc(alert["at"][:19]),
                     esc(body[:180])])
    return table(["When", "Timestamp", "Alert"], rows,
                 empty="No alerts in the log tail.")


def panel_warnings(log: dict, now: datetime) -> str:
    counts = Counter(w["text"].split("—")[0].strip()[:80] for w in log["warnings"])
    summary = table(["Count", "Warning (grouped)"],
                    [[esc(n), esc(text)] for text, n in counts.most_common(12)],
                    empty="No warnings in the log tail.")
    recent = table(["When", "Level", "Message"],
                   [[esc(w["at"][:19]), esc(w["level"]), esc(w["text"])]
                    for w in reversed(log["warnings"][-30:])],
                   empty="—")
    return summary + '<h3>Most recent</h3>' + recent


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f8;--fg:#1b1b1f;--dim:#6b6b76;--line:#e2e2e8;--card:#fff;
--good:#0a7d3f;--goodbg:#e6f5ec;--warn:#8a5a00;--warnbg:#fdf3e0;--bad:#a4232c;--badbg:#fbeaec;}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececf0;--dim:#9a9aa6;
--line:#2c2c34;--card:#1e1e24;--goodbg:#10301f;--good:#6ee7a0;--warnbg:#3a2d10;
--warn:#f0c060;--badbg:#3a1a1e;--bad:#ff9aa2;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:18px 20px 8px}
h1{margin:0;font-size:19px}
.sub{color:var(--dim);font-size:12px;margin-top:3px}
section{margin:18px 20px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px;overflow-x:auto}
section.cards{display:flex;gap:10px;flex-wrap:wrap;background:none;border:0;padding:0}
.card{flex:1 1 160px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.card .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:20px;font-weight:600}
.card.good .v{color:var(--good)}.card.warn .v{color:var(--warn)}.card.bad .v{color:var(--bad)}
h2{font-size:14px;margin:0 0 2px;text-transform:uppercase;letter-spacing:.05em}
h3{font-size:12px;color:var(--dim);margin:14px 0 4px;text-transform:uppercase}
.why{color:var(--dim);font-size:12px;margin:0 0 10px;max-width:70ch}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:11px;color:var(--dim);text-transform:uppercase;
letter-spacing:.04em;padding:4px 8px 6px;border-bottom:1px solid var(--line);
cursor:pointer;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);
padding:1px 4px;border-radius:4px;word-break:break-all}
a{color:inherit}
.dim{color:var(--dim)}
.empty{color:var(--dim);font-size:13px;margin:4px 0}
.pill{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;
border-radius:999px;white-space:nowrap}
.pill.good{background:var(--goodbg);color:var(--good)}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}
.pill.neutral{background:var(--bg);color:var(--dim)}
.spark{display:inline-block}
footer{color:var(--dim);font-size:11px;padding:0 20px 24px}
"""

JS = """
// Click a header to sort. Numeric where the column looks numeric, else text.
for (const th of document.querySelectorAll('th')) {
  th.addEventListener('click', () => {
    const table = th.closest('table');
    const index = [...th.parentNode.children].indexOf(th);
    const rows = [...table.tBodies[0].rows];
    const asc = !(th.dataset.asc === 'true');
    th.dataset.asc = asc;
    const value = tr => (tr.cells[index]?.innerText || '').trim();
    const numeric = rows.every(tr => value(tr) === '' || !isNaN(parseFloat(value(tr))));
    rows.sort((a, b) => numeric
      ? (parseFloat(value(a)) || 0) - (parseFloat(value(b)) || 0)
      : value(a).localeCompare(value(b), 'sv'));
    if (!asc) rows.reverse();
    for (const tr of rows) table.tBodies[0].appendChild(tr);
  });
}
// Tiny inline sparkline for catalogue drift.
for (const el of document.querySelectorAll('.spark')) {
  const values = (el.dataset.values || '').split(',').map(Number).filter(n => !isNaN(n));
  if (values.length < 2) { el.textContent = '—'; return; }
  const min = Math.min(...values), max = Math.max(...values), w = 90, h = 18;
  const step = w / (values.length - 1);
  const y = v => max === min ? h / 2 : h - ((v - min) / (max - min)) * (h - 2) - 1;
  const points = values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  el.innerHTML = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<polyline fill="none" stroke="currentColor" stroke-width="1.3" points="${points}"/></svg>` +
    `<span class="dim" style="margin-left:6px">${min}–${max}</span>`;
}
"""


def render(out: Path, state_dir: Path, config_dir: Path, log_path: Path) -> None:
    now = datetime.now(timezone.utc)
    sites_cfg = load_yaml(config_dir / "sites.yaml").get("sites", {})
    wanted_path = config_dir / "wanted_products.txt"
    wanted = []
    if wanted_path.exists():
        wanted = [ln.split("#")[0].strip()
                  for ln in wanted_path.read_text(encoding="utf-8").splitlines()]
        wanted = [w for w in wanted if w]
    states = {site: load_state(state_dir, site)
              for site, cfg in sites_cfg.items() if cfg.get("enabled")}
    states = {site: state for site, state in states.items() if state}
    log = read_log(log_path)

    tracked = sum(len(s) for s in states.values())
    body = [
        "<header>",
        "<h1>Stock Checker</h1>",
        f'<div class="sub">{tracked} products tracked across {len(states)} sites · '
        f'{len(wanted)} wanted · generated {now.strftime("%Y-%m-%d %H:%M")} UTC</div>',
        "</header>",
        panel_health(log, now),
        panel("What you want, and where", "Every wanted product at every store that "
              "sells it, normalised to SEK. Side by side is the only way a scalped "
              "listing shows up without price history — a 4x outlier is obvious here "
              "and invisible to any single-store rule.",
              panel_prices(sites_cfg, states, wanted, now)),
        panel("Decide on these — new and not watchlisted",
              "A new product alerts exactly ONCE. If nobody acts on that ping it "
              "never speaks again, which is how Whip Brachio slipped past. Copy the "
              "identifier into config/wanted_products.txt or the store's watchlist.",
              panel_triage(sites_cfg, states, now)),
        panel("Watchlist integrity",
              "An entry matching nothing fails silently — you simply stop hearing "
              "about that item. Same for a product that has stopped appearing in "
              "the catalogue.",
              panel_watchlist_integrity(sites_cfg, states, now)),
        panel("In stock the whole time — these can never alert",
              "No stock transition means no restock event, so the checker stays "
              "silent however much you want one of these. This list is the only "
              "way to see them; a bundle bought by hand was sitting here.",
              panel_forever(states, now)),
        panel("Site health and catalogue drift",
              "A catalogue quietly shrinking looks exactly like a store removing "
              "stock. gameshop went 131 → 98 products; drift is the earliest sign "
              "a parser broke.",
              panel_sites(log, sites_cfg, states, now)),
        panel("Coverage of the wanted list",
              "Products stocked nowhere we track cannot be watchlisted at all — "
              "only new-product discovery will ever surface them, so their silence "
              "should not be mistaken for tracking.",
              panel_coverage(states, wanted)),
        panel("Recent alerts", "What was actually sent, so noise can be judged.",
              panel_alerts(log, now)),
        panel("Warnings and errors",
              "Grouped so a recurring problem is visible as a count rather than "
              "buried in a scroll. The delivery postcode is redacted.",
              panel_warnings(log, now)),
        '<footer>Generated by scripts/build_dashboard.py — static, no network '
        'access, regenerated after each cron run.</footer>',
        f"<script>{JS}</script>",
    ]
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Stock Checker</title><style>{CSS}</style></head><body>'
            + "".join(body) + "</body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(out)   # atomic: a reader never sees a half-written page
    print(f"wrote {out} ({len(page) // 1024} KB)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="/var/www/stock-checker/index.html")
    parser.add_argument("--state", default=str(ROOT / "state"))
    parser.add_argument("--config", default=str(ROOT / "config"))
    parser.add_argument("--log", default=str(ROOT / "logs" / "stock-checker.log"))
    args = parser.parse_args(argv)
    render(Path(args.out), Path(args.state), Path(args.config), Path(args.log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
