from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "new-energy-daily" / "scripts" / "new_energy_daily.py"
SPEC = importlib.util.spec_from_file_location("new_energy_daily", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def scored_item(score: int, selected: bool, suffix: str) -> object:
    candidate = MODULE.Candidate(title=suffix, url=f"https://example.com/{suffix}", source="test")
    return MODULE.ScoredItem(candidate, score, "reason", "topic", selected)


def test_project_root_is_skill_directory() -> None:
    assert MODULE.PROJECT_ROOT == ROOT / "skills" / "new-energy-daily"
    assert MODULE.DEFAULT_SOURCES_FILE.exists()


def test_collection_window_handles_italian_dst() -> None:
    start, end = MODULE.collection_window(
        date(2026, 3, 29),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("12:30"),
    )
    assert start.isoformat() == "2026-03-28T12:30:00+01:00"
    assert end.isoformat() == "2026-03-29T12:30:00+02:00"


def test_monday_collection_window_starts_on_friday() -> None:
    start, end = MODULE.collection_window(
        date(2026, 6, 29),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("12:30"),
    )
    assert start.isoformat() == "2026-06-26T12:30:00+02:00"
    assert end.isoformat() == "2026-06-29T12:30:00+02:00"


def test_monday_window_preserves_dst_offsets() -> None:
    start, end = MODULE.collection_window(
        date(2026, 3, 30),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("12:30"),
    )
    assert start.isoformat() == "2026-03-27T12:30:00+01:00"
    assert end.isoformat() == "2026-03-30T12:30:00+02:00"


def test_tuesday_collection_window_starts_on_monday() -> None:
    start, end = MODULE.collection_window(
        date(2026, 6, 30),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("12:30"),
    )
    assert start.isoformat() == "2026-06-29T12:30:00+02:00"
    assert end.isoformat() == "2026-06-30T12:30:00+02:00"


def test_selection_respects_model_decision_threshold_and_limit() -> None:
    items = [
        scored_item(95, False, "rejected"),
        scored_item(80, True, "selected"),
        scored_item(59, True, "below-threshold"),
    ]
    selected = MODULE.select_daily_items(items, minimum_score=60, max_items=15)
    assert [item.candidate.title for item in selected] == ["selected"]


def test_confirmation_token_is_parsed_and_redacted() -> None:
    output = '{"data":{"confirmation_token":"secret-token"}}'
    assert MODULE.parse_confirmation_token(output) == "secret-token"
    assert "secret-token" not in MODULE.redact_confirmation_tokens(output)


def test_send_state_round_trip(tmp_path: Path) -> None:
    state_file = tmp_path / "state" / "sent.json"
    expected = {"2026-06-29": {"body_sha256": "abc"}}
    MODULE.save_send_state(state_file, expected)
    assert MODULE.load_send_state(state_file) == expected


def test_firecrawl_429_retry_delay_uses_server_hint() -> None:
    error = RuntimeError("Firecrawl interact failed (429): retry after 30s")
    assert MODULE.firecrawl_retry_delay(error, attempt=0, max_attempts=2) == 32.0
    assert MODULE.firecrawl_retry_delay(error, attempt=1, max_attempts=2) == 0.0


def test_weekend_delivery_never_calls_agent_mail(tmp_path: Path, monkeypatch: object) -> None:
    body_file = tmp_path / "report.html"
    body_file.write_text("<p>report</p>", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setenv("AGENT_MAIL_RECIPIENTS", "reader@example.com")
    monkeypatch.setattr(MODULE, "run_agent_mail_command", lambda command: calls.append(command))

    MODULE.send_with_agent_mail(
        "Weekend report",
        body_file,
        dry_run=False,
        report_day=date(2026, 7, 4),
        force_send=True,
    )

    assert calls == []
