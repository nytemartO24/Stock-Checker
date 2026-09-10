#!/usr/bin/env python3
"""Set up the dashboard: web root, credentials, Caddy vhost, cron entry.

Idempotent — safe to re-run. Run ON THE VPS as root:

    ./.venv/bin/python deploy/setup_dashboard.py

WHY A PATH AND NOT A SUBDOMAIN: `beyblade.nytemart1.xyz` already resolves here
with a valid Let's Encrypt certificate, so a path costs no DNS change and no new
cert. Its `/` keeps reverse-proxying to :3001 (the old tournament interface) so
reviving that needs nothing from us.

THE PASSWORD IS GENERATED HERE AND NEVER PRINTED. It is written to
/root/dashboard-credentials.txt, chmod 600, for the user to read. Only the bcrypt
hash goes into the Caddyfile, which is the only part safe to keep anywhere else.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path("/root/stock-checker")
WEB_ROOT = Path("/var/www/stock-checker")
CADDYFILE = Path("/etc/caddy/Caddyfile")
CREDENTIALS = Path("/root/dashboard-credentials.txt")
DOMAIN = "beyblade.nytemart1.xyz"
USERNAME = "nytemart"
CRON_MARKER = "# >>> stock-checker-dashboard >>>"
CRON_END = "# <<< stock-checker-dashboard <<<"

BLOCK = f"""	# --- stock-checker dashboard (added by deploy/setup_dashboard.py) ---
	# Static HTML regenerated after every cron run. handle_path strips the
	# prefix, so /stock/ serves {WEB_ROOT}/index.html.
	handle_path /stock* {{
		basic_auth {{
			{USERNAME} {{HASH}}
		}}
		root * {WEB_ROOT}
		file_server
	}}
"""


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def ensure_credentials() -> str:
    """Return the bcrypt hash, generating the password once if needed."""
    if CREDENTIALS.exists():
        text = CREDENTIALS.read_text(encoding="utf-8")
        found = re.search(r"^hash:\s*(\S+)", text, re.M)
        if found:
            print(f"  credentials already exist at {CREDENTIALS} — reusing")
            return found.group(1)

    # url-safe, no shell-hostile characters, ~120 bits.
    password = secrets.token_urlsafe(18)
    result = run(["caddy", "hash-password", "--plaintext", password])
    if result.returncode != 0:
        raise SystemExit(f"caddy hash-password failed: {result.stderr[:200]}")
    hashed = result.stdout.strip()
    CREDENTIALS.write_text(
        "Stock Checker dashboard\n"
        f"url:      https://{DOMAIN}/stock/\n"
        f"username: {USERNAME}\n"
        f"password: {password}\n"
        f"hash:     {hashed}\n"
        f"created:  {datetime.now().isoformat(timespec='seconds')}\n"
        "\nThe password is stored only here. To rotate it, delete this file and\n"
        "re-run deploy/setup_dashboard.py.\n",
        encoding="utf-8")
    CREDENTIALS.chmod(0o600)
    print(f"  generated a new password -> {CREDENTIALS} (chmod 600, not printed)")
    return hashed


def patch_caddyfile(hashed: str) -> bool:
    original = CADDYFILE.read_text(encoding="utf-8")
    if "/stock*" in original:
        print("  Caddyfile already has the /stock block — leaving it alone")
        return False
    if f"{DOMAIN} {{" not in original:
        raise SystemExit(f"expected a '{DOMAIN}' site block in {CADDYFILE}")

    backup = CADDYFILE.with_suffix(f".backup-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(CADDYFILE, backup)
    print(f"  backed up Caddyfile -> {backup}")

    # Insert BEFORE the reverse_proxy so the more specific path wins; Caddy
    # orders handle/route blocks by specificity, but being explicit costs
    # nothing and reads correctly to a human.
    block = BLOCK.replace("{HASH}", hashed)
    updated = original.replace("\treverse_proxy", block + "\n\treverse_proxy", 1)
    CADDYFILE.write_text(updated, encoding="utf-8")

    check = run(["caddy", "validate", "--config", str(CADDYFILE)])
    if check.returncode != 0:
        shutil.copy2(backup, CADDYFILE)
        raise SystemExit("caddy validate FAILED, Caddyfile restored:\n"
                         + (check.stderr or check.stdout)[:600])
    print("  caddy validate: ok")
    return True


def ensure_cron() -> None:
    current = run(["crontab", "-l"]).stdout
    if CRON_MARKER in current:
        print("  cron entry already present")
        return
    # :20/:50 — after this project's :15/:45 run, so the page reflects it.
    entry = (f"{CRON_MARKER}\n"
             f"20,50 * * * * {REPO}/.venv/bin/python {REPO}/scripts/build_dashboard.py "
             f"--out {WEB_ROOT}/index.html >> {REPO}/logs/dashboard.log 2>&1\n"
             f"{CRON_END}\n")
    merged = current.rstrip("\n") + "\n" + entry
    proc = subprocess.run(["crontab", "-"], input=merged, text=True,
                          capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"crontab install failed: {proc.stderr[:200]}")
    print("  installed cron entry at :20/:50")


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    print("stock-checker dashboard setup")

    WEB_ROOT.mkdir(parents=True, exist_ok=True)
    # Caddy runs as its own user and only needs to read.
    shutil.chown(WEB_ROOT, user="caddy", group="caddy") if shutil.which("caddy") else None
    WEB_ROOT.chmod(0o755)
    print(f"  web root: {WEB_ROOT}")

    hashed = ensure_credentials()
    changed = patch_caddyfile(hashed)
    ensure_cron()

    build = run([str(REPO / ".venv" / "bin" / "python"),
                 str(REPO / "scripts" / "build_dashboard.py"),
                 "--out", str(WEB_ROOT / "index.html")])
    print("  build:", (build.stdout or build.stderr).strip()[:160])
    if build.returncode != 0:
        raise SystemExit("dashboard build failed")

    if changed:
        reload_result = run(["systemctl", "reload", "caddy"])
        if reload_result.returncode != 0:
            raise SystemExit("caddy reload failed: " + reload_result.stderr[:200])
        print("  caddy reloaded")

    print(f"\nDone. https://{DOMAIN}/stock/  (credentials in {CREDENTIALS})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
