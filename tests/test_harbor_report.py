"""Tests for :mod:`ai_benchmark.harbor_report` parsing and rendering."""

import json
from pathlib import Path

import pytest

from ai_benchmark.harbor_report import (
    TrialReport,
    find_trial_dirs,
    parse_paths,
    parse_trial,
    reports_to_json,
    reports_to_markdown,
)

_FIXTURE_JOB = Path(__file__).parent / "fixtures" / "harbor_job"
_ORACLE_TRIAL = _FIXTURE_JOB / "django__django-15098__oracle"
_SABOTEUR_TRIAL = _FIXTURE_JOB / "django__django-15098__saboteur"

# Fix 3 (zero-call validity) fixtures: a live arm that made calls, a live arm
# that made none (nop-shaped), and a deterministic arm that legitimately makes
# none. See the module docstring in ai_benchmark.harbor_report.
_LIVE_JOB = Path(__file__).parent / "fixtures" / "live_job"
_LIVE_RESOLVED = _LIVE_JOB / "live_resolved"
_LIVE_ZERO_CALL = _LIVE_JOB / "live_zero_call"
_DETERMINISTIC_NOP = _LIVE_JOB / "deterministic_nop"


def test_find_trial_dirs_skips_job_aggregate() -> None:
    """The job-level result.json (no task_name) must not count as a trial."""
    trials = list(find_trial_dirs(_FIXTURE_JOB))
    assert trials == [_ORACLE_TRIAL, _SABOTEUR_TRIAL]


def test_find_trial_dirs_accepts_direct_trial_dir() -> None:
    """A trial directory passed directly should yield itself."""
    assert list(find_trial_dirs(_SABOTEUR_TRIAL)) == [_SABOTEUR_TRIAL]


def test_parse_trial_oracle_resolved() -> None:
    """The oracle trial resolves with all tests passing and no regressions."""
    report = parse_trial(_ORACLE_TRIAL)
    assert report.task == "django__django-15098"
    assert report.condition == "oracle"
    assert report.resolved is True
    assert report.reward == pytest.approx(1.0)
    assert report.f2p_fail_count == 0
    assert report.p2p_fail_count == 0
    assert report.f2p_pass_count == 2


def test_parse_trial_saboteur_regression_named() -> None:
    """The saboteur trial exposes the regressed PASS_TO_PASS test by name."""
    report = parse_trial(_SABOTEUR_TRIAL)
    assert report.condition == "saboteur"
    assert report.resolved is False
    assert report.reward == pytest.approx(0.0)
    # The task's own FAIL_TO_PASS tests still pass...
    assert report.f2p_pass_count == 2
    assert report.f2p_fail_count == 0
    # ...but a hidden PASS_TO_PASS test regressed, captured at name granularity.
    assert report.p2p_failed == ("test_to_language (i18n.tests.TranslationTests)",)
    assert report.p2p_pass_count == 2


def test_parse_missing_result_raises(tmp_path: Path) -> None:
    """Parsing a directory without result.json raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        parse_trial(tmp_path)


def test_parse_paths_is_sorted() -> None:
    """parse_paths orders records by (task, condition)."""
    reports = parse_paths([_FIXTURE_JOB])
    assert [(r.task, r.condition) for r in reports] == [
        ("django__django-15098", "oracle"),
        ("django__django-15098", "saboteur"),
    ]


def test_reports_to_json_round_trips() -> None:
    """The JSON rendering is valid and preserves regression names."""
    reports = parse_paths([_FIXTURE_JOB])
    payload = json.loads(reports_to_json(reports))
    assert len(payload) == 2
    saboteur = next(record for record in payload if record["condition"] == "saboteur")
    assert saboteur["pass_to_pass"]["failed"] == [
        "test_to_language (i18n.tests.TranslationTests)",
    ]
    assert saboteur["resolved"] is False


def test_reports_to_markdown_lists_regression() -> None:
    """The Markdown table has a header row and shows the regressed test name."""
    markdown = reports_to_markdown(parse_paths([_FIXTURE_JOB]))
    lines = markdown.splitlines()
    assert lines[0].startswith("| Task | Condition |")
    assert "P2P regressions" in lines[0]
    assert "test_to_language (i18n.tests.TranslationTests)" in markdown
    # The oracle row reports no regressions.
    oracle_row = next(line for line in lines if "| oracle |" in line)
    assert oracle_row.endswith("| - |")


def test_live_trial_with_calls_is_valid() -> None:
    """A live arm with model calls and assistant turns is graded valid."""
    report = parse_trial(_LIVE_RESOLVED)
    assert report.is_model_arm is True
    assert report.api_calls == 5
    assert report.has_assistant_turn is True
    assert report.is_zero_call is False
    assert report.validity == "valid"


def test_live_trial_zero_call_is_flagged_invalid() -> None:
    """A live arm that made no assistant turn is flagged INVALID: zero-call.

    Mirrors the pilot's litellm-bug shape: mini-swe-agent still counts one
    ``api_calls`` for the query that raised, so the assistant-turn signal is the
    one that catches it -- exactly why both signals are kept.
    """
    report = parse_trial(_LIVE_ZERO_CALL)
    assert report.is_model_arm is True
    assert report.api_calls == 1
    assert report.has_assistant_turn is False
    assert report.is_zero_call is True
    assert report.validity == "INVALID: zero-call"


def test_deterministic_zero_call_is_not_flagged() -> None:
    """A deterministic arm makes no calls by design and must stay valid."""
    report = parse_trial(_DETERMINISTIC_NOP)
    assert report.is_model_arm is False
    assert report.api_calls is None
    assert report.is_zero_call is False
    assert report.validity == "valid"


def test_existing_deterministic_fixtures_are_valid() -> None:
    """The oracle/saboteur instruments (model_info null) are never zero-call."""
    for trial in (_ORACLE_TRIAL, _SABOTEUR_TRIAL):
        report = parse_trial(trial)
        assert report.is_model_arm is False
        assert report.validity == "valid"


def test_markdown_has_validity_column_and_flags_zero_call() -> None:
    """The Markdown table gains a Validity column that surfaces the verdict."""
    markdown = reports_to_markdown(parse_paths([_LIVE_JOB]))
    header = markdown.splitlines()[0]
    assert "| Validity |" in header
    zero_call_row = next(
        line for line in markdown.splitlines() if "demo-task-2" in line
    )
    assert "INVALID: zero-call" in zero_call_row
    resolved_row = next(line for line in markdown.splitlines() if "demo-task-1" in line)
    assert "| valid |" in resolved_row


def test_json_output_carries_validity_verdict() -> None:
    """The JSON records the verdict and the raw signals that produced it."""
    payload = json.loads(reports_to_json(parse_paths([_LIVE_JOB])))
    by_task = {record["task"]: record for record in payload}
    assert by_task["demo-task-2"]["validity"] == "INVALID: zero-call"
    assert by_task["demo-task-2"]["is_model_arm"] is True
    assert by_task["demo-task-2"]["api_calls"] == 1
    assert by_task["demo-task-2"]["has_assistant_turn"] is False
    assert by_task["demo-task-1"]["validity"] == "valid"


def test_untrimmed_trajectory_and_zero_api_calls(tmp_path: Path) -> None:
    """The untrimmed trajectory filename is read, and api_calls==0 flags too."""
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({
            "task_name": "demo",
            "agent_info": {
                "name": "mini-swe-agent-experiment",
                "version": "2.4.5",
                "model_info": {"name": "m", "provider": "p"},
            },
        }),
        encoding="utf-8",
    )
    # Full (untrimmed) filename, api_calls == 0 despite an assistant turn.
    (trial / "agent" / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "info": {"model_stats": {"api_calls": 0}},
            "messages": [{"role": "assistant", "content": "x"}],
        }),
        encoding="utf-8",
    )
    report = parse_trial(trial)
    assert report.api_calls == 0
    assert report.is_zero_call is True


def test_reward_falls_back_to_result_json(tmp_path: Path) -> None:
    """When reward.txt is absent, the reward comes from result.json."""
    trial = tmp_path / "trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({
            "task_name": "demo",
            "agent_info": {"name": "nop"},
            "verifier_result": {"rewards": {"reward": 0.0}},
        }),
        encoding="utf-8",
    )
    (trial / "verifier" / "report.json").write_text(
        json.dumps({
            "demo": {
                "resolved": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": [], "failure": ["t_a"]},
                    "PASS_TO_PASS": {"success": ["t_b"], "failure": []},
                },
            },
        }),
        encoding="utf-8",
    )
    report = parse_trial(trial)
    assert report.reward == pytest.approx(0.0)
    assert report.f2p_failed == ("t_a",)
    assert isinstance(report, TrialReport)
