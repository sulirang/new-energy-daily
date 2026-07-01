from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


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


def test_verified_news_sources_use_rss_and_exa_fallback() -> None:
    config = MODULE.load_sources(MODULE.DEFAULT_SOURCES_FILE)
    sources = {source["name"]: source for source in config["sources"]}
    assert sources["ENTSO-E"]["type"] == "rss"
    assert sources["ENTSO-E"]["url"] == "https://www.entsoe.eu/rss/news.xml"
    assert sources["European Commission Energy"]["type"] == "rss"
    assert sources["European Commission Energy"]["url"] == "https://energy.ec.europa.eu/node/2/rss_en"
    assert sources["Euractiv Energy"]["type"] == "exa"
    assert sources["Euractiv Energy"]["include_domains"] == ["www.euractiv.com"]


def test_collection_window_handles_italian_dst() -> None:
    start, end = MODULE.collection_window(
        date(2026, 3, 29),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("07:00"),
    )
    assert start.isoformat() == "2026-03-28T07:00:00+01:00"
    assert end.isoformat() == "2026-03-29T07:00:00+02:00"


def test_monday_collection_window_starts_on_friday() -> None:
    start, end = MODULE.collection_window(
        date(2026, 6, 29),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("07:00"),
    )
    assert start.isoformat() == "2026-06-26T07:00:00+02:00"
    assert end.isoformat() == "2026-06-29T07:00:00+02:00"


def test_monday_window_preserves_dst_offsets() -> None:
    start, end = MODULE.collection_window(
        date(2026, 3, 30),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("07:00"),
    )
    assert start.isoformat() == "2026-03-27T07:00:00+01:00"
    assert end.isoformat() == "2026-03-30T07:00:00+02:00"


def test_tuesday_collection_window_starts_on_monday() -> None:
    start, end = MODULE.collection_window(
        date(2026, 6, 30),
        ZoneInfo("Europe/Rome"),
        MODULE.parse_collection_cutoff("07:00"),
    )
    assert start.isoformat() == "2026-06-29T07:00:00+02:00"
    assert end.isoformat() == "2026-06-30T07:00:00+02:00"


def test_exa_query_uses_the_exact_weekday_cutoff_window() -> None:
    tz = ZoneInfo("Europe/Rome")
    start, end = MODULE.collection_window(
        date(2026, 6, 30),
        tz,
        MODULE.parse_collection_cutoff("07:00"),
    )
    query = MODULE.build_exa_query(
        {"query": "energy published between {window_start} and {window_end}"},
        tz,
        date(2026, 6, 30),
        start,
        end,
    )
    assert "2026-06-29 07:00" in query
    assert "2026-06-30 07:00" in query
    assert "Europe/Rome" in query


def test_exa_legacy_date_placeholder_covers_every_monday_window_date() -> None:
    tz = ZoneInfo("Europe/Rome")
    start, end = MODULE.collection_window(
        date(2026, 6, 29),
        tz,
        MODULE.parse_collection_cutoff("07:00"),
    )
    query = MODULE.build_exa_query(
        {"query": "energy published on {date}"},
        tz,
        date(2026, 6, 29),
        start,
        end,
    )
    assert "(2026-06-26 OR 2026-06-27 OR 2026-06-28 OR 2026-06-29)" in query
    assert "published between 2026-06-26 07:00 and 2026-06-29 07:00" in query


def test_firecrawl_key_pool_rotates_and_persists(tmp_path: Path) -> None:
    state_file = tmp_path / "firecrawl-state.json"
    pool = MODULE.FirecrawlKeyPool(["first", "second"], state_file)
    assert [key for _, key in pool.reserve_attempts()] == ["first", "second"]
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"next_index": 1}
    assert [key for _, key in pool.reserve_attempts()] == ["second", "first"]
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"next_index": 0}


def test_firecrawl_key_pool_loads_multiple_environment_keys(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setenv("FIRECRAWL_KEY_FILE", str(tmp_path / "missing-keys.txt"))
    monkeypatch.setenv("FIRECRAWL_KEY_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("FIRECRAWL_API_KEYS", "first, second;third")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "third")
    pool = MODULE.FirecrawlKeyPool.from_environment()
    assert pool.keys == ["first", "second", "third"]


def test_firecrawl_key_failure_uses_next_key_and_redacts_errors(tmp_path: Path, capsys: object) -> None:
    pool = MODULE.FirecrawlKeyPool(["first-secret", "second-secret"], tmp_path / "state.json")
    attempted = []

    def request(api_key: str) -> str:
        attempted.append(api_key)
        if api_key == "first-secret":
            raise RuntimeError("Firecrawl smoke failed (401): invalid api key first-secret")
        return "ok"

    assert MODULE.run_with_firecrawl_keys("smoke", request, pool) == "ok"
    assert attempted == ["first-secret", "second-secret"]
    captured = capsys.readouterr()
    assert "first-secret" not in captured.err


class FakeCompletions:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        message = SimpleNamespace(content='{"ok": true}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_client(completions: FakeCompletions) -> object:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_json_mode_does_not_retry_authentication_errors() -> None:
    error = RuntimeError("authentication failed")
    error.status_code = 401
    completions = FakeCompletions([error])
    with pytest.raises(RuntimeError, match="authentication failed"):
        MODULE.chat_completion(fake_client(completions), "model", [], 0.0, json_object=True)
    assert len(completions.calls) == 1


def test_json_mode_retries_only_when_response_format_is_unsupported() -> None:
    error = RuntimeError("unsupported parameter: response_format")
    error.status_code = 400
    completions = FakeCompletions([error])
    content = MODULE.chat_completion(fake_client(completions), "model", [], 0.0, json_object=True)
    assert content == '{"ok": true}'
    assert len(completions.calls) == 2
    assert "response_format" in completions.calls[0]
    assert "response_format" not in completions.calls[1]


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
