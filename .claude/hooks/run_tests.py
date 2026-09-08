#!/usr/bin/env python3
"""PostToolUse hook: run the test suite whenever a Python file is edited.

Exists because the review of the first two commits found five real bugs that
a passing suite hadn't caught, and the ones the suite COULD have caught only
got caught because someone remembered to run it. This removes the
remembering.

Reads the hook payload on stdin, runs pytest only when the edited file is a
.py file inside this project, and reports failures back to Claude so a
regression surfaces on the edit that caused it rather than at the end of
the task.

Deliberately fails open: a broken hook must never block work, so any
unexpected condition (no venv yet, unparseable payload, pytest missing)
exits 0 silently.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # non-Windows layout
    PYTHON = ROOT / ".venv" / "bin" / "python"

MAX_REPORT_CHARS = 3000


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    raw_path = tool_input.get("file_path") or tool_response.get("filePath") or ""
    if not raw_path.endswith(".py"):
        return 0

    # Only react to files in THIS project — scratchpad and unrelated repos
    # would otherwise trigger a pointless run.
    try:
        Path(raw_path).resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return 0

    if not PYTHON.exists() or not (ROOT / "tests").is_dir():
        return 0

    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", "tests/", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return 0

    report = (result.stdout or "") + (result.stderr or "")
    if len(report) > MAX_REPORT_CHARS:
        report = report[-MAX_REPORT_CHARS:]
    # decision "block" on PostToolUse feeds `reason` back to Claude and lets
    # the turn continue — surfacing the failure without halting the work.
    print(json.dumps({
        "decision": "block",
        "reason": f"pytest failed after editing {Path(raw_path).name}:\n{report}",
        "systemMessage": "Tests failing — see hook output.",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
