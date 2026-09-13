"""The audit must not cry wolf the morning after a log rotation.

logrotate runs weekly, so once a week the live log covers only a few hours.
Reading just that file made the 24h audit report "17/48 runs — a shortfall
means runs are being skipped" while, in the same breath, reporting "no skipped
slots, largest gap 30 min". A health check that contradicts itself trains you
to skim its output.
"""

import gzip

from scripts.audit_health import tail_lines


def write(first_ts, count, step_min=30):
    """`count` log lines, 30 minutes apart, starting at hour `first_ts`."""
    lines = []
    for i in range(count):
        minute = (i * step_min) % 60
        hour = first_ts + (i * step_min) // 60
        lines.append(f"2026-09-13T{hour:02d}:{minute:02d}:00 [INFO] run complete: line {i}")
    return "\n".join(lines) + "\n"


def test_reads_only_the_live_log_when_it_spans_the_window(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(write(0, 10), encoding="utf-8")
    assert len(tail_lines(log, "2026-09-13T00:00:00")) == 10


def test_includes_the_rotated_archive_the_window_reaches_back_into(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(write(6, 4), encoding="utf-8")  # live: 06:00 onwards
    with gzip.open(tmp_path / "app.log.1.gz", "wt", encoding="utf-8") as handle:
        handle.write(write(0, 12))  # rotated: 00:00-05:30

    lines = tail_lines(log, "2026-09-13T00:00:00")
    assert len(lines) == 16, "the rotated log's lines are inside the window too"
    # Oldest first, so cadence and gap checks see a correctly ordered timeline.
    assert lines[0].startswith("2026-09-13T00:00:00")
    assert lines[-1].startswith("2026-09-13T07:30:00")


def test_does_not_reach_past_the_cutoff(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(write(6, 4), encoding="utf-8")
    with gzip.open(tmp_path / "app.log.1.gz", "wt", encoding="utf-8") as handle:
        handle.write(write(0, 12))

    lines = tail_lines(log, "2026-09-13T04:00:00")
    assert all(ln[:19] >= "2026-09-13T04:00:00" for ln in lines)
    assert len(lines) == 8


def test_missing_archive_is_not_an_error(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(write(6, 2), encoding="utf-8")
    assert len(tail_lines(log, "2026-09-13T00:00:00")) == 2


def test_missing_log_is_empty(tmp_path):
    assert tail_lines(tmp_path / "nope.log", "2026-09-13T00:00:00") == []
