#!/usr/bin/env python3
"""Full health audit of a running deployment. Read-only, no network.

Answers "is everything actually working" with evidence rather than a green tick,
and every check exists because the corresponding failure was SILENT at least
once in this project:

  * a run that produced a partial catalogue and pruned real products;
  * a market whose delivery location was never pinned, so its answers described
    the wrong country;
  * discovery returning zero and reading as "no new products";
  * a watchlist entry matching nothing, so an item simply stopped alerting;
  * an alert generated but never delivered.

    python scripts/audit_health.py                 # last 24h
    python scripts/audit_health.py --hours 72
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

LOG = ROOT / "logs" / "stock-checker.log"
DASH_LOG = ROOT / "logs" / "dashboard.log"
STATE = ROOT / "state"
CATALOG_LOG = Path("/root/news-notifier/logs/catalog-multi.log")
DASH_HTML = Path("/var/www/stock-checker/index.html")

RUN_DONE = re.compile(r"^(\S+) \[INFO\] run complete: (\d+) alert\(s\) across (\d+) site\(s\), (\d+) failure")
SITE_LINE = re.compile(r"^(\S+) \[INFO\] \[([\w.-]+)\] (\d+) product\(s\), (\d+) in stock, (\d+) alert")
LEVEL_LINE = re.compile(r"^(\S+) \[(WARNING|ERROR)\] (.*)$")
PINNED = re.compile(r"^(\S+) \[INFO\] \[([a-z]{2})\] delivery location confirmed")
NOT_PINNED = re.compile(r"^(\S+) \[WARNING\] \[([a-z]{2})\] DELIVERY LOCATION NOT APPLIED")
DISCOVERY = re.compile(r"([a-z]{2})\s+(\d+) new of (\d+) found")
PRUNED = re.compile(r"^(\S+) \[INFO\] (\S+): pruned (\d+) entr")
UNDELIVERED = re.compile(r"alert for (\S+) not delivered")

ok_count = 0
issues: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok_count
    if passed:
        ok_count += 1
        print(f"  OK    {label}{(' — ' + detail) if detail else ''}")
    else:
        issues.append(f"{label}: {detail}")
        print(f"  ISSUE {label} — {detail}")


def tail_lines(path: Path, cutoff: str, max_bytes: int = 8_000_000) -> list[str]:
    if not path.exists():
        return []
    data = path.read_bytes()[-max_bytes:]
    lines = data.decode("utf-8", "replace").splitlines()
    return [ln for ln in lines if ln[:19] >= cutoff]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    now = datetime.now()
    cutoff = (now - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"=== Stock Checker health audit — last {args.hours}h (since {cutoff}) ===")
    print(f"    now: {now:%Y-%m-%d %H:%M:%S}\n")

    lines = tail_lines(LOG, cutoff)

    # --- 1. cron ----------------------------------------------------------
    print("[cron]")
    crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    for label, needle in (("stock-checker check", "stock-checker/deploy/run.sh main.py"),
                          ("dashboard build", "build_dashboard.py"),
                          ("amazon discovery", "scrape_catalog_multi.py")):
        active = [ln for ln in crontab.splitlines()
                  if needle in ln and not ln.strip().startswith("#")]
        check(label, bool(active), active[0].split()[0:2] and " ".join(active[0].split()[:5])
              if active else "no active crontab entry")

    # --- 2. run cadence ---------------------------------------------------
    print("\n[runs]")
    runs = [(m.group(1), int(m.group(2)), int(m.group(4)))
            for m in (RUN_DONE.match(ln) for ln in lines) if m]
    check("runs completed", bool(runs), f"{len(runs)} in {args.hours}h "
          f"(expected ~{args.hours * 2})")
    if runs:
        expected = args.hours * 2
        check("run cadence", len(runs) >= expected * 0.9,
              f"{len(runs)}/{expected} — a shortfall means runs are being skipped "
              f"or overrunning their slot")
        failures = sum(f for _, _, f in runs)
        check("site failures", failures == 0, f"{failures} failure(s) across {len(runs)} runs")
        last = datetime.strptime(runs[-1][0][:19], "%Y-%m-%dT%H:%M:%S")
        gap = (now - last).total_seconds() / 60
        check("last run recent", gap < 45, f"{gap:.0f} min ago")
        # A long gap between consecutive runs is a skipped slot, which the
        # per-run view cannot show.
        stamps = [datetime.strptime(r[0][:19], "%Y-%m-%dT%H:%M:%S") for r in runs]
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])]
        worst = max(gaps) if gaps else 0
        check("no skipped slots", worst < 45, f"largest gap {worst:.0f} min")

    # --- 3. per-site ------------------------------------------------------
    print("\n[sites]")
    per_site: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ln in lines:
        m = SITE_LINE.match(ln)
        if m:
            per_site[m.group(2)].append((int(m.group(3)), int(m.group(4))))
    config = yaml.safe_load((ROOT / "config" / "sites.yaml").read_text(encoding="utf-8"))["sites"]
    enabled = {n for n, c in config.items() if c.get("enabled")}
    for site in sorted(enabled):
        seen = per_site.get(site, [])
        if not seen:
            check(f"{site} ran", False, "no run lines in this window")
            continue
        counts = [c for c, _ in seen]
        spread = max(counts) - min(counts)
        # A catalogue that wobbles run to run is the shape of the rarewaves
        # incident: a short page looks exactly like products being delisted.
        check(f"{site} catalogue stable", spread <= max(2, min(counts) * 0.05),
              f"{min(counts)}-{max(counts)} products over {len(seen)} runs"
              + (" — WOBBLE" if spread > max(2, min(counts) * 0.05) else ""))

    # --- 4. completeness + pruning ---------------------------------------
    print("\n[data integrity]")
    incomplete = [ln for ln in lines if "INCOMPLETE" in ln]
    check("no partial catalogues", not incomplete,
          f"{len(incomplete)} INCOMPLETE warning(s) — guard worked, but the API is flaky"
          if incomplete else "")
    prunes = [(m.group(2), int(m.group(3))) for m in
              (PRUNED.match(ln) for ln in lines) if m]
    check("no surprise pruning", not prunes,
          "; ".join(f"{f} dropped {n}" for f, n in prunes) if prunes else "")
    undelivered = [ln for ln in lines if UNDELIVERED.search(ln)]
    check("all alerts delivered", not undelivered, f"{len(undelivered)} undelivered")

    # --- 5. amazon pinning ------------------------------------------------
    print("\n[amazon]")
    pinned = Counter(m.group(2) for m in (PINNED.match(ln) for ln in lines) if m)
    unpinned = Counter(m.group(2) for m in (NOT_PINNED.match(ln) for ln in lines) if m)
    markets = sorted(set(pinned) | set(unpinned))
    for market in markets:
        good, bad = pinned[market], unpinned[market]
        rate = bad / (good + bad) if (good + bad) else 0
        check(f"{market} location pinned", rate == 0,
              f"{bad} failure(s) of {good + bad} ({rate:.0%})")
    if not markets:
        check("amazon markets seen", False, "no pin lines at all in this window")

    # --- 6. discovery (news-notifier) ------------------------------------
    print("\n[discovery]")
    disc_lines = tail_lines(CATALOG_LOG, cutoff, 2_000_000)
    found: dict[str, list[int]] = defaultdict(list)
    for ln in disc_lines:
        m = DISCOVERY.search(ln)
        if m:
            found[m.group(1)].append(int(m.group(3)))
    if not found:
        check("discovery ran", False, "no results in the catalogue log")
    for market in sorted(found):
        counts = found[market]
        zeros = sum(1 for c in counts if c == 0)
        check(f"amazon.{market} discovery", zeros == 0,
              f"{zeros} zero-result run(s) of {len(counts)}; latest {counts[-1]}")

    # --- 7. watchlist integrity ------------------------------------------
    print("\n[watchlists]")
    for site in sorted(enabled):
        path = STATE / f"{site}.json"
        if not path.exists():
            continue
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries = entries.get("products", entries)
        keys = {(k.split(":", 1)[1] if ":" in k else k) for k in entries}
        watch = [str(w).strip() for w in (config[site].get("watchlist") or [])]
        missing = [w for w in watch if w not in keys]
        check(f"{site} watchlist resolves", not missing,
              f"{len(missing)} entry/entries match nothing: {', '.join(missing)[:90]}"
              if missing else f"{len(watch)} entries")

    # --- 8. state + history ----------------------------------------------
    print("\n[state]")
    stale_sites = []
    for site in sorted(enabled):
        path = STATE / f"{site}.json"
        if not path.exists():
            check(f"{site} state exists", False, "missing state file")
            continue
        age_min = (now.timestamp() - path.stat().st_mtime) / 60
        if age_min > 45:
            stale_sites.append(f"{site} ({age_min:.0f}m)")
    check("state files fresh", not stale_sites, ", ".join(stale_sites))

    history_path = STATE / "price_history.json"
    if history_path.exists():
        hist = json.loads(history_path.read_text(encoding="utf-8"))
        points = sum(len(v.get("points", [])) for v in hist.values())
        multi = sum(1 for v in hist.values() if len(v.get("points", [])) > 1)
        check("price history growing", points >= len(hist),
              f"{len(hist)} products, {points} points, {multi} with a price change")
    else:
        check("price history exists", False, "no price_history.json")

    # --- 9. dashboard -----------------------------------------------------
    print("\n[dashboard]")
    if DASH_HTML.exists():
        age_min = (now.timestamp() - DASH_HTML.stat().st_mtime) / 60
        check("dashboard fresh", age_min < 45,
              f"rebuilt {age_min:.0f} min ago, {DASH_HTML.stat().st_size // 1024} KB")
    else:
        check("dashboard exists", False, "no index.html")
    if DASH_LOG.exists():
        errs = [ln for ln in tail_lines(DASH_LOG, cutoff)
                if "Traceback" in ln or "Error" in ln]
        check("dashboard builds clean", not errs, f"{len(errs)} error line(s)")

    # --- 10. warnings -----------------------------------------------------
    print("\n[warnings]")
    levels = Counter()
    grouped = Counter()
    for ln in lines:
        m = LEVEL_LINE.match(ln)
        if m:
            levels[m.group(2)] += 1
            grouped[re.sub(r"\d+", "N", m.group(3))[:70]] += 1
    check("no ERROR lines", levels["ERROR"] == 0, f"{levels['ERROR']} ERROR line(s)")
    print(f"        {levels['WARNING']} warning(s) in window; most common:")
    for text, n in grouped.most_common(6):
        print(f"          {n:>4}  {text}")

    # --- 11. disk ---------------------------------------------------------
    print("\n[disk]")
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()
    if len(df) > 1:
        used = df[1].split()[4]
        check("disk space", int(used.rstrip("%")) < 85, f"{used} used")
    logsize = sum(p.stat().st_size for p in (ROOT / "logs").glob("*") if p.is_file())
    check("log size sane", logsize < 200_000_000, f"{logsize // 1_048_576} MB in logs/")

    print(f"\n=== {ok_count} checks passed, {len(issues)} issue(s) ===")
    for issue in issues:
        print(f"  ! {issue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
