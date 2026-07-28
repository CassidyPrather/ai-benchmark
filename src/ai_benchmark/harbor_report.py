"""Parse Harbor job output into per-trial regression records.

Harbor writes one directory per trial containing ``result.json`` (trial metadata,
including the agent/condition name and the reward) and ``verifier/report.json``
(the SWE-bench grading output, which carries *per-test* FAIL_TO_PASS and
PASS_TO_PASS success/failure lists). This module flattens a job directory (or any
tree of trial directories) into :class:`TrialReport` records and renders them as
JSON or a Markdown table.

The whole point of Experiment 001 is measuring regressions at PASS_TO_PASS
granularity rather than a single resolved/unresolved bit, so this parser
deliberately preserves the individual failing test *names*, not just counts.

It also emits a per-trial **validity verdict** (Fix 3 in ``HARNESS.md``). A live
pilot trial once made **zero** model calls yet was graded as a competent-harness
measurement of an incompetent model -- a silent, directionally biased artifact
(PILOT-LIVE.md, Hazard 1). To stop a ``nop``-shaped result being believed, model
arms with no model activity are flagged ``INVALID: zero-call``. The signals come
from the mini-swe-agent trajectory (``info.model_stats.api_calls`` and whether
any assistant turn exists); the arm *type* comes from the recorded
``agent_info.model_info`` -- present only when a model was configured -- so the
deterministic instruments (oracle/nop/saboteur), which legitimately make no
calls, are never false-flagged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

_RESULT_FILENAME = "result.json"
_REPORT_RELPATH = ("verifier", "report.json")
_REWARD_RELPATH = ("verifier", "reward.txt")
_AGENT_DIRNAME = "agent"
#: mini-swe-agent trajectory filenames, in preference order. The committed pilot
#: evidence is trimmed for repo hygiene; a live Harbor run writes the untrimmed
#: file. Both carry ``info.model_stats`` and the message list verbatim.
_TRAJECTORY_FILENAMES = (
    "mini-swe-agent.trajectory.json",
    "mini-swe-agent.trajectory.trimmed.json",
)

_TESTS_STATUS = "tests_status"
_FAIL_TO_PASS = "FAIL_TO_PASS"  # noqa: S105 -- grading category, not a password
_PASS_TO_PASS = "PASS_TO_PASS"  # noqa: S105 -- grading category, not a password
_SUCCESS = "success"
_FAILURE = "failure"

_UNKNOWN = "unknown"

_VALID = "valid"
_INVALID_ZERO_CALL = "INVALID: zero-call"


@dataclass(frozen=True)
class TrialReport:
    """A single Harbor trial flattened to the fields Experiment 001 cares about.

    :param task: The task (SWE-bench instance) name.
    :param condition: The agent/condition name (e.g. ``oracle``, ``nop``,
        ``saboteur``).
    :param resolved: Whether SWE-bench graded the trial as fully resolved.
    :param reward: The scalar reward Harbor recorded (``1.0``/``0.0``), if known.
    :param f2p_passed: Names of FAIL_TO_PASS tests that passed.
    :param f2p_failed: Names of FAIL_TO_PASS tests that failed.
    :param p2p_passed: Names of PASS_TO_PASS tests that passed.
    :param p2p_failed: Names of PASS_TO_PASS tests that failed (regressions).
    :param trial_dir: The source trial directory.
    :param is_model_arm: Whether a model was configured (``model_info`` present).
        Deterministic instruments (oracle/nop/saboteur) are ``False`` and are
        never flagged zero-call.
    :param api_calls: Model calls recorded in the trajectory, or ``None`` if no
        trajectory was found.
    :param has_assistant_turn: Whether the trajectory contains any assistant
        (model) turn.
    """

    task: str
    condition: str
    resolved: bool
    reward: float | None
    f2p_passed: tuple[str, ...]
    f2p_failed: tuple[str, ...]
    p2p_passed: tuple[str, ...]
    p2p_failed: tuple[str, ...]
    trial_dir: Path
    is_model_arm: bool = False
    api_calls: int | None = None
    has_assistant_turn: bool = False

    @property
    def is_zero_call(self) -> bool:
        """Whether this is a model arm that made no model calls.

        A model arm is zero-call if it produced no assistant turn *or* recorded
        ``api_calls == 0``. Both signals are kept because mini-swe-agent counts a
        query attempt (``api_calls == 1``) even when the request raised before
        any assistant reply -- the exact shape of the pilot's silent failure.
        Deterministic arms are never zero-call: they make no calls by design.
        """
        if not self.is_model_arm:
            return False
        return not self.has_assistant_turn or self.api_calls == 0

    @property
    def validity(self) -> str:
        """The per-trial validity verdict for the results table."""
        return _INVALID_ZERO_CALL if self.is_zero_call else _VALID

    @property
    def f2p_pass_count(self) -> int:
        """Number of FAIL_TO_PASS tests that passed."""
        return len(self.f2p_passed)

    @property
    def f2p_fail_count(self) -> int:
        """Number of FAIL_TO_PASS tests that failed."""
        return len(self.f2p_failed)

    @property
    def p2p_pass_count(self) -> int:
        """Number of PASS_TO_PASS tests that passed."""
        return len(self.p2p_passed)

    @property
    def p2p_fail_count(self) -> int:
        """Number of PASS_TO_PASS tests that failed (regressions)."""
        return len(self.p2p_failed)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of this record."""
        return {
            "task": self.task,
            "condition": self.condition,
            "validity": self.validity,
            "is_model_arm": self.is_model_arm,
            "api_calls": self.api_calls,
            "has_assistant_turn": self.has_assistant_turn,
            "resolved": self.resolved,
            "reward": self.reward,
            "fail_to_pass": {
                "passed": list(self.f2p_passed),
                "failed": list(self.f2p_failed),
            },
            "pass_to_pass": {
                "passed": list(self.p2p_passed),
                "failed": list(self.p2p_failed),
            },
            "trial_dir": str(self.trial_dir),
        }


def _names(status: dict[str, Any], category: str, outcome: str) -> tuple[str, ...]:
    """Extract a tuple of test names for one grading category/outcome."""
    bucket = status.get(category)
    if not isinstance(bucket, dict):
        return ()
    values = bucket.get(outcome, [])
    if not isinstance(values, list):
        return ()
    return tuple(str(name) for name in values)


def _read_call_stats(trial_dir: Path) -> tuple[int | None, bool]:
    """Return ``(api_calls, has_assistant_turn)`` from the trial's trajectory.

    Reads the mini-swe-agent trajectory (trimmed or full) if present. Returns
    ``(None, False)`` when no trajectory is found -- for a model arm that is
    treated as zero-call, since there is no evidence any call was made.
    """
    agent_dir = trial_dir / _AGENT_DIRNAME
    for filename in _TRAJECTORY_FILENAMES:
        path = agent_dir / filename
        if not path.is_file():
            continue
        try:
            trajectory: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, False
        model_stats = (trajectory.get("info") or {}).get("model_stats") or {}
        raw_calls = model_stats.get("api_calls")
        api_calls = raw_calls if isinstance(raw_calls, int) else None
        messages = trajectory.get("messages") or []
        has_assistant_turn = any(_is_assistant_turn(message) for message in messages)
        return api_calls, has_assistant_turn
    return None, False


def _is_assistant_turn(message: object) -> bool:
    """Whether a trajectory message is a model (assistant) turn.

    Covers both mini-swe-agent wire shapes: chat-completions assistant messages
    (``role == "assistant"``) and the Responses API, where a turn is the raw
    response object (``object == "response"``, no ``role``).
    """
    return isinstance(message, dict) and (
        message.get("role") == "assistant" or message.get("object") == "response"
    )


def _read_reward(trial_dir: Path, result: dict[str, Any]) -> float | None:
    """Resolve the trial reward from ``reward.txt`` or ``result.json``."""
    reward_path = trial_dir.joinpath(*_REWARD_RELPATH)
    if reward_path.is_file():
        text = reward_path.read_text(encoding="utf-8").strip()
        if text:
            try:
                return float(text)
            except ValueError:
                return None
    rewards = (result.get("verifier_result") or {}).get("rewards")
    if isinstance(rewards, dict) and isinstance(rewards.get("reward"), (int, float)):
        return float(rewards["reward"])
    return None


def parse_trial(trial_dir: Path) -> TrialReport:
    """Parse a single Harbor trial directory into a :class:`TrialReport`.

    :param trial_dir: A directory containing ``result.json`` and, when the
        verifier ran, ``verifier/report.json``.
    :returns: The flattened trial record.
    :raises FileNotFoundError: If ``result.json`` is absent.
    """
    result_path = trial_dir / _RESULT_FILENAME
    if not result_path.is_file():
        message = f"No {_RESULT_FILENAME} in {trial_dir}"
        raise FileNotFoundError(message)
    result: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))

    task = str(result.get("task_name") or _UNKNOWN)
    agent_info = result.get("agent_info") or {}
    condition = str(agent_info.get("name") or _UNKNOWN)
    # A model was configured iff ``model_info`` is recorded; the deterministic
    # instruments (oracle/nop/saboteur) leave it null, so this cleanly separates
    # "should have made calls" arms without hardcoding agent names.
    is_model_arm = agent_info.get("model_info") is not None
    api_calls, has_assistant_turn = _read_call_stats(trial_dir)
    reward = _read_reward(trial_dir, result)

    report_path = trial_dir.joinpath(*_REPORT_RELPATH)
    resolved = False
    status: dict[str, Any] = {}
    if report_path.is_file():
        report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        entry = report.get(task)
        if entry is None and report:
            entry = next(iter(report.values()))
        if isinstance(entry, dict):
            resolved = bool(entry.get("resolved", False))
            raw_status = entry.get(_TESTS_STATUS)
            if isinstance(raw_status, dict):
                status = raw_status
    elif reward is not None:
        resolved = reward >= 1.0

    return TrialReport(
        task=task,
        condition=condition,
        resolved=resolved,
        reward=reward,
        f2p_passed=_names(status, _FAIL_TO_PASS, _SUCCESS),
        f2p_failed=_names(status, _FAIL_TO_PASS, _FAILURE),
        p2p_passed=_names(status, _PASS_TO_PASS, _SUCCESS),
        p2p_failed=_names(status, _PASS_TO_PASS, _FAILURE),
        trial_dir=trial_dir,
        is_model_arm=is_model_arm,
        api_calls=api_calls,
        has_assistant_turn=has_assistant_turn,
    )


def _is_trial_result(result_path: Path) -> bool:
    """Return whether *result_path* is a per-trial (not job-aggregate) result.

    Harbor writes a job-level ``result.json`` (aggregate stats, no ``task_name``)
    alongside per-trial ``result.json`` files; only the latter are trials.
    """
    if not result_path.is_file():
        return False
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("task_name"))


def find_trial_dirs(root: Path) -> list[Path]:
    """Return every trial directory at or beneath *root*, sorted by path.

    A trial directory is one that directly contains a per-trial ``result.json``
    (one carrying a ``task_name``); Harbor's job-aggregate ``result.json`` is
    skipped.

    :param root: A trial directory, a job directory, or any ancestor tree.
    :return: Trial directories in sorted path order.
    """
    if _is_trial_result(root / _RESULT_FILENAME):
        return [root]
    return sorted(
        {p.parent for p in root.rglob(_RESULT_FILENAME) if _is_trial_result(p)},
    )


def parse_paths(paths: Iterable[Path]) -> list[TrialReport]:
    """Parse every trial found under each of *paths* into report records.

    Results are ordered by task then condition for stable table output.
    """
    reports: list[TrialReport] = []
    for path in paths:
        reports.extend(parse_trial(trial) for trial in find_trial_dirs(path))
    reports.sort(key=lambda record: (record.task, record.condition))
    return reports


def reports_to_json(reports: Sequence[TrialReport]) -> str:
    """Render *reports* as a pretty-printed JSON array."""
    return json.dumps([record.to_dict() for record in reports], indent=2)


def _format_reward(reward: float | None) -> str:
    """Render a reward value for the Markdown table."""
    if reward is None:
        return "-"
    return f"{reward:g}"


def reports_to_markdown(reports: Sequence[TrialReport]) -> str:
    """Render *reports* as a GitHub-flavoured Markdown table.

    The ``Validity`` column carries the zero-call verdict; the final column lists
    the names of any regressed PASS_TO_PASS tests, which is the signal
    Experiment 001 exists to measure.
    """
    header = (
        "| Task | Condition | Validity | Resolved | Reward | F2P pass/fail | "
        "P2P pass/fail | P2P regressions |"
    )
    divider = "| --- | --- | --- | --- | --- | --- | --- | --- |"
    rows = [header, divider]
    for record in reports:
        regressions = ", ".join(record.p2p_failed) if record.p2p_failed else "-"
        rows.append(
            f"| {record.task} | {record.condition} "
            f"| {record.validity} "
            f"| {'yes' if record.resolved else 'no'} "
            f"| {_format_reward(record.reward)} "
            f"| {record.f2p_pass_count}/{record.f2p_fail_count} "
            f"| {record.p2p_pass_count}/{record.p2p_fail_count} "
            f"| {regressions} |",
        )
    return "\n".join(rows)
