"""Tests for :mod:`ai_benchmark.review_driver`'s pure orchestration.

These exercise the driver's phase sequencing, prompt filling, in-place
continuation, review extraction, and the ``/testbed`` non-mutation invariant with
fakes only -- ``minisweagent`` is never imported, mirroring the sandbox-only
dependency boundary the driver is written to respect.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from ai_benchmark.review_driver import (
    ADVERSARIAL,
    CONTROL,
    IMPLEMENT,
    REVIEW,
    REVISE,
    SELF_REVIEW,
    ExperimentResult,
    bash_wrap,
    continue_agent,
    extract_review_text,
    fill_prompt,
    prepare_review_diff,
    run_review_experiment,
)

_TASK = "Fix the {widget} so that a[b]{c} parses; see issue #{42}."
_CRITIQUE = "TASK:\n{task}\nDIFF:\n{diff}\nEnd."
_REVISE = "REVIEW:\n{review}\nProduce your final patch."
_FOOTER = "\n[submit review to file]\n"
_DIFF = "diff --git a/x b/x\n+brace {here}\n"
_REVIEW_TEXT = "Defect: off-by-one with a literal {brace}."


# --- fill_prompt ------------------------------------------------------------


def test_fill_prompt_replaces_each_placeholder() -> None:
    """Every named placeholder is replaced by its raw value."""
    filled = fill_prompt(_CRITIQUE, task="T", diff="D")
    assert filled == "TASK:\nT\nDIFF:\nD\nEnd."


def test_fill_prompt_is_literal_not_format() -> None:
    """Values containing braces survive verbatim (str.replace, not str.format)."""
    filled = fill_prompt(_CRITIQUE, task=_TASK, diff=_DIFF)
    assert _TASK in filled
    assert _DIFF in filled
    # No leftover placeholders for the keys we filled.
    assert "{task}" not in filled
    assert "{diff}" not in filled


def test_fill_prompt_review_placeholder() -> None:
    """The revise template's ``{review}`` is filled with the raw review text."""
    filled = fill_prompt(_REVISE, review=_REVIEW_TEXT)
    assert _REVIEW_TEXT in filled
    assert "{review}" not in filled


# --- bash_wrap: the interpreter fix -----------------------------------------


def test_bash_wrap_reexecs_into_bash() -> None:
    """Commands are wrapped so ``/bin/sh`` re-execs into non-interactive ``bash``.

    Without this the SWE-bench testbed conda env is never activated (``$BASH_ENV``
    is ignored by ``dash``), silently invalidating the measurement.
    """
    assert bash_wrap("pytest -q").startswith("exec bash -c ")


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "echo 'a b' && cat patch.txt",
        'grep -r "class {" src',
        "python - <<'EOF'\nprint(1)\nEOF",
        "",
    ],
)
def test_bash_wrap_round_trips_arbitrary_commands(command: str) -> None:
    """The original command is recoverable byte-for-byte -- quoting must be exact.

    ``/bin/sh -c "exec bash -c <quoted>"`` has to hand ``bash`` the original
    command intact however many quotes/newlines it contains, or the model's command
    would be corrupted before it runs.
    """
    assert shlex.split(bash_wrap(command)) == ["exec", "bash", "-c", command]


def test_bash_wrap_preserves_submit_sentinel() -> None:
    """The submit command survives the wrap, so the sentinel still fires on stdout."""
    submit = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat review.md"
    assert shlex.split(bash_wrap(submit))[-1] == submit


# --- run_review_experiment: phase sequencing --------------------------------


@dataclass
class _SeamRecorder:
    """Records seam invocations for :func:`run_review_experiment`."""

    review_text: str = _REVIEW_TEXT
    implement_calls: list[str] = field(default_factory=list)
    compute_diff_calls: int = 0
    review_in_context_calls: list[str] = field(default_factory=list)
    fresh_review_calls: list[str] = field(default_factory=list)
    revise_calls: list[str] = field(default_factory=list)

    def implement(self, task: str) -> None:
        """Record the implement instruction."""
        self.implement_calls.append(task)

    def compute_diff(self) -> str:
        """Return a fixed diff and count the call."""
        self.compute_diff_calls += 1
        return _DIFF

    def review_in_context(self, review_task: str) -> str:
        """Record the in-context review task and return the canned review."""
        self.review_in_context_calls.append(review_task)
        return self.review_text

    def fresh_review(self, review_task: str) -> str:
        """Record the fresh-reviewer task and return the canned review."""
        self.fresh_review_calls.append(review_task)
        return self.review_text

    def revise(self, revise_task: str) -> str | None:
        """Record the revise task and return a canned submission."""
        self.revise_calls.append(revise_task)
        return "FINAL-PATCH"


def _run(condition: str, recorder: _SeamRecorder) -> ExperimentResult:
    """Run the orchestrator against *recorder*'s seams for *condition*."""
    return run_review_experiment(
        condition=condition,
        task=_TASK,
        critique_template=_CRITIQUE,
        revise_template=_REVISE,
        review_footer=_FOOTER,
        implement=recorder.implement,
        compute_diff=recorder.compute_diff,
        review_in_context=recorder.review_in_context,
        fresh_review=recorder.fresh_review,
        revise=recorder.revise,
    )


def test_control_runs_implement_only() -> None:
    """``control`` runs exactly one phase and touches no review machinery."""
    recorder = _SeamRecorder()
    result = _run(CONTROL, recorder)
    assert [phase.name for phase in result.phases] == [IMPLEMENT]
    assert recorder.implement_calls == [_TASK]
    assert recorder.compute_diff_calls == 0
    assert recorder.review_in_context_calls == []
    assert recorder.fresh_review_calls == []
    assert recorder.revise_calls == []
    assert result.review_text is None


def test_self_review_runs_three_phases_in_context() -> None:
    """``self_review`` implements, reviews in-context, then revises."""
    recorder = _SeamRecorder()
    result = _run(SELF_REVIEW, recorder)
    assert [phase.name for phase in result.phases] == [IMPLEMENT, REVIEW, REVISE]
    # The review came from the author's own context, not a fresh reviewer.
    assert len(recorder.review_in_context_calls) == 1
    assert recorder.fresh_review_calls == []
    assert result.review_text == _REVIEW_TEXT


def test_adversarial_runs_three_phases_fresh_reviewer() -> None:
    """``adversarial`` implements, reviews in a FRESH context, then revises."""
    recorder = _SeamRecorder()
    result = _run(ADVERSARIAL, recorder)
    assert [phase.name for phase in result.phases] == [IMPLEMENT, REVIEW, REVISE]
    # The review came from the fresh reviewer, not the author's own context.
    assert len(recorder.fresh_review_calls) == 1
    assert recorder.review_in_context_calls == []
    assert result.review_text == _REVIEW_TEXT


def test_unknown_condition_is_rejected() -> None:
    """An unknown condition is refused before any phase runs."""
    recorder = _SeamRecorder()
    with pytest.raises(ValueError, match="Unknown condition"):
        _run("different_model", recorder)
    assert recorder.implement_calls == []


# --- conditions 2 & 3 parity ------------------------------------------------


def test_review_conditions_share_revise_and_review_prompts() -> None:
    """Conditions 2 and 3 build byte-identical review and revise prompts.

    Only the SOURCE of the review differs (in-context vs fresh reviewer); given the
    same diff and the same resulting review text, the critique+footer instruction
    and the filled revise prompt must be identical -- the validity requirement.
    """
    self_recorder = _SeamRecorder()
    adv_recorder = _SeamRecorder()
    self_result = _run(SELF_REVIEW, self_recorder)
    adv_result = _run(ADVERSARIAL, adv_recorder)

    # The review instruction (critique + footer) is identical across arms...
    assert self_recorder.review_in_context_calls == adv_recorder.fresh_review_calls
    # ...and so is the filled revise prompt (same revise.txt, same review text).
    assert self_recorder.revise_calls == adv_recorder.revise_calls
    assert len(self_recorder.revise_calls) == 1

    # The revise prompt is the revise template filled with the review text only.
    expected_revise = fill_prompt(_REVISE, review=_REVIEW_TEXT)
    assert self_recorder.revise_calls[0] == expected_revise

    # The review phase prompt is the critique filled with task+diff, plus footer.
    expected_review = fill_prompt(_CRITIQUE, task=_TASK, diff=_DIFF) + _FOOTER
    review_phase = next(p for p in self_result.phases if p.name == REVIEW)
    assert review_phase.prompt == expected_review
    adv_review_phase = next(p for p in adv_result.phases if p.name == REVIEW)
    assert adv_review_phase.prompt == expected_review


# --- continue_agent: in-place continuation ----------------------------------


class _FakeAgent:
    """A minimal stand-in for a live mini-swe-agent (no ``minisweagent``)."""

    def __init__(self) -> None:
        """Seed a submitted-and-exited history like a completed phase leaves."""
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "did it", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "obs"},
            {"role": "exit", "content": "patch", "extra": {"submission": "patch"}},
        ]
        self.n_calls = 7
        self.cost = 0.25
        self.config = SimpleNamespace(step_limit=100, cost_limit=1.0)
        self.model = SimpleNamespace(
            format_message=lambda **kwargs: dict(kwargs),
        )

    def add_messages(self, *messages: dict[str, Any]) -> list[dict[str, Any]]:
        """Extend the history in place, exactly like the real agent."""
        self.messages.extend(messages)
        return list(messages)


def test_continue_agent_injects_without_reset() -> None:
    """Continuation pops the exit turn and appends a user turn -- no reset."""
    agent = _FakeAgent()
    original_prefix = agent.messages[:4]  # everything except the trailing exit
    loop_calls: list[object] = []

    def fake_loop(passed: object) -> dict[str, Any]:
        loop_calls.append(passed)
        return {"submission": "revised", "exit_status": "Submitted"}

    result = continue_agent(
        agent,
        "please revise",
        step_limit=40,
        cost_limit=0.5,
        run_loop=fake_loop,
    )

    # History was continued, not reset: the original turns are still present...
    assert agent.messages[:4] == original_prefix
    # ...the trailing exit turn was popped...
    assert all(m.get("role") != "exit" for m in agent.messages)
    # ...and the injected instruction is the new final turn.
    assert agent.messages[-1] == {"role": "user", "content": "please revise"}
    # Budgets were lifted relative to the running totals (per-phase full budget).
    assert agent.config.step_limit == 7 + 40
    assert agent.config.cost_limit == pytest.approx(0.25 + 0.5)
    assert loop_calls == [agent]
    assert result == {"submission": "revised", "exit_status": "Submitted"}


def test_continue_agent_without_trailing_exit() -> None:
    """If the last turn is not an ``exit``, nothing is popped before injecting."""
    agent = _FakeAgent()
    agent.messages = [{"role": "assistant", "content": "hi"}]

    result = continue_agent(
        agent,
        "next",
        step_limit=10,
        cost_limit=0.1,
        run_loop=lambda _agent: {"submission": ""},
    )
    assert agent.messages == [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "next"},
    ]
    assert result == {"submission": ""}


# --- prepare_review_diff: /testbed non-mutation -----------------------------


@dataclass
class _FakeShell:
    """Records the argument vectors :func:`prepare_review_diff` runs."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str]) -> SimpleNamespace:
        """Record *args* and return a canned diff."""
        self.calls.append(args)
        return SimpleNamespace(returncode=0, stdout="THE-DIFF")


def test_prepare_review_diff_returns_copy_diff() -> None:
    """The diff is computed in the isolated copy and returned."""
    shell = _FakeShell()
    diff = prepare_review_diff(shell)
    assert diff == "THE-DIFF"
    assert shell.calls == [
        ["rm", "-rf", "/review"],
        ["cp", "-a", "/testbed", "/review"],
        ["git", "-C", "/review", "add", "-A", "-N"],
        ["git", "-C", "/review", "diff", "HEAD"],
    ]


def test_prepare_review_diff_never_mutates_testbed() -> None:
    """No command mutates ``/testbed``: git only targets the ``/review`` copy.

    ``/testbed`` may appear only as the read-only ``cp`` source; it must never be
    the target of a git command or any mutating verb -- that would corrupt what the
    SWE-bench verifier grades.
    """
    shell = _FakeShell()
    prepare_review_diff(shell)
    mutating_verbs = {"commit", "reset", "checkout", "clean", "restore", "stash", "add"}
    for args in shell.calls:
        # git operations only ever run against the /review copy.
        if args and args[0] == "git":
            assert "-C" in args
            assert args[args.index("-C") + 1] == "/review"
            assert "/testbed" not in args
        # No command applies a mutating git verb to /testbed.
        if "/testbed" in args:
            assert args[0] == "cp"  # /testbed is only ever a copy *source*
            assert not (mutating_verbs & set(args))


# --- extract_review_text ----------------------------------------------------


def test_extract_review_prefers_submission() -> None:
    """A sentinel submission is used verbatim when present."""
    review = extract_review_text({"submission": "the review"}, [])
    assert review == "the review"


def test_extract_review_falls_back_to_last_assistant() -> None:
    """With no submission, the last assistant message is used."""
    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": "early"},
        {"role": "tool", "content": "obs"},
        {"role": "assistant", "content": "final review prose"},
    ]
    review = extract_review_text({"submission": ""}, messages)
    assert review == "final review prose"


def test_extract_review_empty_when_nothing_usable() -> None:
    """No submission and no assistant prose yields an empty string, not an error."""
    review = extract_review_text({}, [{"role": "user", "content": "hi"}])
    assert not review
