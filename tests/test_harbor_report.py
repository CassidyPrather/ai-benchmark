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
