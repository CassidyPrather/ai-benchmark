"""In-sandbox driver for the Experiment 001 implement->review->revise treatment.

This module is executed *inside the trial sandbox* by
:class:`ai_benchmark.live_agents.ExperimentReviewAgent`, under the
``mini-swe-agent`` tool venv's Python (which has ``minisweagent`` and
``litellm[proxy]`` installed). It drives ``mini-swe-agent`` **as a library**
rather than by re-invoking the ``mini`` CLI, because the CLI cannot continue a
conversation across phases: :meth:`DefaultAgent.run` resets ``self.messages = []``
on entry (``agents/default.py:91`` in v2.4.5) and there is no resume/seed-history
mechanism (no loader in ``utils/serialize.py``, no ``--resume`` flag in
``run/mini.py``). "Same context" (condition ``self_review``) is therefore only
achievable by keeping one live ``agent.messages`` object across phases, which is
exactly what :func:`continue_agent` does.

**Importability contract.** ``minisweagent`` is *not* a dependency of the
``ai_benchmark`` package -- it exists only in the sandbox. So every
``minisweagent`` import in this module lives *inside* a function (never at module
scope), and the orchestration core (:func:`run_review_experiment`,
:func:`continue_agent`, :func:`prepare_review_diff`, :func:`extract_review_text`,
:func:`fill_prompt`) is written as pure functions over injected seams so it can be
unit-tested in the repo venv with fakes and no ``minisweagent`` present.

**The three conditions** (they differ *only* in where the review happens, which is
the manipulated variable -- see ``experiments/001-adversarial-review/DESIGN.md``):

* ``control`` -- implement only.
* ``self_review`` -- after implementing, the SAME author conversation reviews its
  own patch in-context (:func:`continue_agent`), then revises.
* ``adversarial`` -- a FRESH context of the SAME model reviews the patch (blind to
  the author transcript, in an isolated ``/review`` copy), its review is delivered
  into the author's context, then the author revises.

All three run through the SAME orchestrator (:func:`run_review_experiment`) so
control and the treatment arms share machinery and differ only by the ``condition``
knob -- a validity requirement.
"""

from __future__ import annotations

import json
import shlex
import subprocess  # noqa: S404 -- only ever runs fixed internal argv (see _run_shell)
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

# --- Conditions -------------------------------------------------------------

#: Implement only; no review, no revise. The shared-machinery baseline.
CONTROL = "control"
#: Implement, then the author self-reviews in its own context, then revises.
SELF_REVIEW = "self_review"
#: Implement, then a fresh same-model reviewer reviews a blind copy, then the
#: author revises using that review.
ADVERSARIAL = "adversarial"
#: The full set of accepted conditions; anything else is rejected.
CONDITIONS = frozenset({CONTROL, SELF_REVIEW, ADVERSARIAL})

# --- Sandbox paths (fixed, non-configurable) --------------------------------

#: The graded working tree. **INVARIANT: no phase may mutate this via git
#: (``commit``/``reset``/``checkout .``) and reviewer prep must not touch its
#: working tree or index** -- it is what the SWE-bench verifier grades.
TESTBED = "/testbed"
#: An isolated full copy of :data:`TESTBED` the reviewer explores read-only. All
#: git operations for diffing/exploration target this copy, never :data:`TESTBED`.
REVIEW_CHECKOUT = "/review"
#: Scratch dir for the review file the author/reviewer submits (kept OUT of
#: :data:`TESTBED` so the graded tree stays clean of a stray ``REVIEW.md``).
REVIEW_WORKDIR = "/tmp/mswea-review"  # noqa: S108
#: The single review-submission file, shared by both review conditions (author
#: and reviewer run sequentially, never concurrently) so the submission footer is
#: byte-identical across conditions 2 and 3 -- a parity requirement.
REVIEW_FILE = f"{REVIEW_WORKDIR}/review.md"

#: mini-swe-agent-specific termination footer appended to the (scaffold-agnostic)
#: registered ``critique.txt`` by the driver as connective. It routes the review
#: through mini-swe-agent's sentinel-submission protocol
#: (``LocalEnvironment._check_finished``, ``environments/local.py:45``) so the
#: review is captured deterministically in ``info.submission`` and the reviewing
#: agent terminates cleanly (the scaffold has no other clean stop -- every
#: assistant turn must carry a bash tool call, and the run loop only breaks on an
#: ``exit`` message, ``agents/default.py:120``). The submission is a *review*, not
#: a patch, and the reviewer must not mutate the checkout it is grading.
REVIEW_SUBMISSION_FOOTER = f"""

---
Operational notes (how to deliver this review in this environment):

- This is a REVIEW task. Do NOT modify, stage, commit, or reset any source file;
  only read files and run tests/reproductions to check your reasoning.
- When your review is complete, write the full review text to the file
  {REVIEW_FILE} (create parent directories if needed).
- Then, as a SEPARATE and FINAL command, submit it using EXACTLY:

  echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat {REVIEW_FILE}

  Do not combine the submit command with any other command. After you submit you
  cannot continue working.
"""

# --- Phase names (for the trajectory-snapshot filenames and records) --------

IMPLEMENT = "implement"
REVIEW = "review"
REVISE = "revise"
REVIEWER = "reviewer"


class _Agent(Protocol):
    """Structural view of the ``minisweagent`` agent seams this driver uses.

    Only the attributes/methods actually touched are declared, so the pure
    helpers can be typed and tested without importing ``minisweagent``.
    """

    messages: list[dict[str, Any]]
    n_calls: int
    cost: float
    config: Any
    model: Any

    def add_messages(self, *messages: dict[str, Any]) -> list[dict[str, Any]]:
        """Extend ``self.messages`` with *messages* and return them."""
        ...


class _ShellResult(Protocol):
    """Structural view of a completed shell command (``subprocess`` compatible)."""

    returncode: int
    stdout: str


@dataclass(frozen=True)
class PhaseRecord:
    """One executed phase and the exact prompt that drove it.

    :param name: The phase name (:data:`IMPLEMENT`, :data:`REVIEW`,
        :data:`REVISE`).
    :param prompt: The verbatim instruction handed to the agent for this phase
        (the task, or the filled critique+footer, or the filled revise prompt).
    """

    name: str
    prompt: str


@dataclass(frozen=True)
class ExperimentResult:
    """The outcome of one :func:`run_review_experiment` call.

    :param condition: The condition that was run.
    :param phases: The phases executed, in order (1 for ``control``, 3 otherwise).
    :param review_text: The review that was fed to the revise step, or ``None``
        for ``control``.
    :param final_submission: The author's final submission string (the post-revise
        patch text mini-swe-agent captured), or ``None`` for ``control`` / when no
        submission was produced.
    """

    condition: str
    phases: list[PhaseRecord] = field(default_factory=list)
    review_text: str | None = None
    final_submission: str | None = None


def fill_prompt(template: str, **substitutions: str) -> str:
    """Fill ``{key}`` placeholders in *template* by literal string replacement.

    Deliberately uses :meth:`str.replace` rather than :meth:`str.format`: the task
    statement and the unified diff routinely contain literal ``{``/``}`` (code,
    f-strings, JSON), which :meth:`str.format` would misparse. Each placeholder is
    replaced with the raw substitution value; the values are never re-interpreted.

    :param template: The prompt text containing ``{key}`` placeholders.
    :param substitutions: Mapping of placeholder name to replacement text (e.g.
        ``task=...``, ``diff=...``, ``review=...``).
    :returns: The template with every ``{key}`` replaced by its value.
    """
    result = template
    for key, value in substitutions.items():
        result = result.replace("{" + key + "}", value)
    return result


def bash_wrap(command: str) -> str:
    """Wrap *command* so it runs under non-interactive ``bash`` (not ``/bin/sh``).

    ``minisweagent``'s :class:`LocalEnvironment` runs every command via
    ``subprocess.Popen(command, shell=True)`` -- i.e. ``/bin/sh -c`` (``dash`` on
    the SWE-bench Debian images) -- and has no interpreter setting. But the
    ``swebench`` config activates the testbed conda env through ``BASH_ENV``: it
    sets ``interpreter: ["bash", "-c"]`` and ``BASH_ENV=/root/.bashrc`` so a
    non-interactive ``bash`` sources ``~/.bashrc`` -> ``conda activate testbed``.
    ``$BASH_ENV`` is honoured *only* by ``bash`` invoked as ``bash``; under
    ``dash`` (or ``bash`` in ``sh`` mode) it is ignored, so the testbed env is
    never activated and commands run against the base interpreter -- silently
    invalidating the measurement.

    Re-exec into ``bash -c`` so ``$BASH_ENV`` is honoured, reproducing the upstream
    Docker ``bash -c`` path while still running locally in the sandbox. ``exec``
    preserves the pid/pgid so :class:`LocalEnvironment`'s process-group timeout
    kill still targets the right process; :func:`shlex.quote` keeps it safe for
    arbitrary model commands. stdout is unchanged, so the submit-sentinel check
    (``COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`` on the first output line) still fires.

    :param command: The raw command the model asked to run.
    :returns: ``exec bash -c <quoted command>``.
    """
    return "exec bash -c " + shlex.quote(command)


def extract_review_text(result: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Extract the review text a reviewing phase produced.

    Prefers the deterministic sentinel submission (``info.submission``, populated
    when the agent runs the review-file submission from
    :data:`REVIEW_SUBMISSION_FOOTER`). Falls back to the reviewing agent's last
    assistant message if no submission was captured (e.g. the phase hit its step
    budget before submitting), so a degenerate phase still yields whatever review
    prose exists rather than an empty string.

    :param result: The exit ``extra`` dict returned by the agent run/continuation
        (carries ``submission``).
    :param messages: The reviewing agent's message history, newest last.
    :returns: The review text (possibly empty if nothing usable was produced).
    """
    submission = str((result or {}).get("submission") or "")
    if submission.strip():
        return submission
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return submission


def prepare_review_diff(
    shell: Callable[[list[str]], _ShellResult],
    *,
    testbed: str = TESTBED,
    review: str = REVIEW_CHECKOUT,
) -> str:
    """Copy the testbed to an isolated checkout and return the author's diff.

    **Provably non-mutating to** *testbed*: every command either reads *testbed*
    (the ``cp`` source) or targets *review* (the throwaway copy). No git command
    touches *testbed*'s working tree or index, so the SWE-bench verifier still
    grades exactly the state the author produced. ``cp -a`` preserves the whole
    tree including ``.git``, so the reviewer can diff and run tests in the copy.

    The diff is computed in the copy with ``git add -A -N`` (intent-to-add, so
    newly created files show up) followed by ``git diff HEAD``.

    :param shell: Injected command runner (a ``subprocess.run``-shaped callable);
        injected so the non-mutation invariant is unit-testable with a fake.
    :param testbed: The graded working tree to copy from (never mutated).
    :param review: The isolated destination copy to diff in.
    :returns: The unified diff of the author's change (stdout of ``git diff HEAD``).
    """
    shell(["rm", "-rf", review])
    shell(["cp", "-a", testbed, review])
    shell(["git", "-C", review, "add", "-A", "-N"])
    diff = shell(["git", "-C", review, "diff", "HEAD"])
    return diff.stdout


def continue_agent(
    agent: _Agent,
    content: str,
    *,
    step_limit: int,
    cost_limit: float,
    run_loop: Callable[[_Agent], dict[str, Any]],
) -> dict[str, Any]:
    """Continue *agent*'s conversation with a new user turn, WITHOUT resetting it.

    This is the load-bearing continuation primitive that :meth:`DefaultAgent.run`
    cannot provide (it would ``self.messages = []``). It mirrors the seam between
    ``agents/default.py:91`` and the run loop at ``:96-122``:

    1. Pop the trailing synthetic ``{"role": "exit"}`` turn left by the previous
       phase. (After a submission via :class:`InteractiveAgent`, the history is
       already API-valid -- ``execute_actions``' ``finally`` padded a ``role:
       "tool"`` observation for the submit tool call, ``interactive.py:124-139``
       + ``models/utils/actions_toolcall.py:79`` -- so once the ``exit`` turn is
       removed the history ends on a valid observation.)
    2. Inject the new instruction as a ``user`` message via
       ``model.format_message`` + ``add_messages`` (extends, never resets).
    3. Raise the step and cost budgets so THIS phase gets a full, constant budget:
       ``n_calls``/``cost`` accumulate across continuations (they are never reset),
       so the ceilings are lifted relative to the running totals. Each author
       phase therefore gets its own ``step_limit`` calls / ``cost_limit`` dollars.
    4. Run the same loop :meth:`DefaultAgent.run` runs, via the injected
       *run_loop*, and return its exit ``extra`` dict.

    :param agent: The live agent whose ``messages`` are continued in place.
    :param content: The user instruction to inject (filled critique or revise).
    :param step_limit: Additional model calls to grant this phase.
    :param cost_limit: Additional dollars to grant this phase.
    :param run_loop: The step loop to run (real loop in the sandbox; a fake in
        tests), injected so this function's message manipulation is testable.
    :returns: The exit ``extra`` dict (carries ``exit_status`` / ``submission``).
    """
    if agent.messages and agent.messages[-1].get("role") == "exit":
        agent.messages.pop()
    agent.add_messages(agent.model.format_message(role="user", content=content))
    agent.config.step_limit = agent.n_calls + step_limit
    agent.config.cost_limit = agent.cost + cost_limit
    return run_loop(agent)


def run_review_experiment(
    *,
    condition: str,
    task: str,
    critique_template: str,
    revise_template: str,
    review_footer: str,
    implement: Callable[[str], None],
    compute_diff: Callable[[], str],
    review_in_context: Callable[[str], str],
    fresh_review: Callable[[str], str],
    revise: Callable[[str], str | None],
) -> ExperimentResult:
    """Orchestrate the implement->review->revise phases for one *condition*.

    The single entry point all three conditions share (``control`` is
    implement-only), so the arms differ only by the ``condition`` knob and the
    review's *source* -- never by the machinery. Conditions ``self_review`` and
    ``adversarial`` build the review instruction identically (same filled
    ``critique`` + same *review_footer*) and revise identically (same filled
    ``revise`` over the resulting review text); only which seam produces the
    review differs (*review_in_context* vs *fresh_review*).

    All ``minisweagent`` interaction is behind the injected seams, so this
    function is pure and unit-testable with fakes.

    :param condition: One of :data:`CONDITIONS`.
    :param task: The task statement (fills ``{task}`` in *critique_template* and is
        the implement instruction).
    :param critique_template: The registered ``critique.txt`` (has ``{task}`` /
        ``{diff}``).
    :param revise_template: The registered ``revise.txt`` (has ``{review}``).
    :param review_footer: The driver-owned submission footer appended to the filled
        critique (identical for both review conditions).
    :param implement: Seam that runs the implement phase (``author.run(task)``).
    :param compute_diff: Seam returning the author's unified diff (fills ``{diff}``).
    :param review_in_context: Seam (``self_review``) that continues the author's own
        conversation with the review instruction and returns the review text.
    :param fresh_review: Seam (``adversarial``) that runs a fresh reviewer over a
        blind copy and returns the review text.
    :param revise: Seam that continues the author with the filled revise prompt and
        returns the final submission text.
    :returns: The :class:`ExperimentResult` describing the phases that ran.
    :raises ValueError: If *condition* is not one of :data:`CONDITIONS`.
    """
    if condition not in CONDITIONS:
        message = (
            f"Unknown condition {condition!r}; expected one of {sorted(CONDITIONS)}"
        )
        raise ValueError(message)

    phases: list[PhaseRecord] = []

    # Phase 1 -- implement (every condition, control included).
    implement(task)
    phases.append(PhaseRecord(IMPLEMENT, task))
    if condition == CONTROL:
        return ExperimentResult(condition=condition, phases=phases)

    # Build the review instruction. Identical construction for both review
    # conditions so conditions 2 and 3 are comparable: same critique fill, same
    # footer; only the *source* of the review differs below.
    diff = compute_diff()
    critique_filled = fill_prompt(critique_template, task=task, diff=diff)
    review_task = critique_filled + review_footer

    if condition == SELF_REVIEW:
        review_text = review_in_context(review_task)
    else:  # ADVERSARIAL
        review_text = fresh_review(review_task)
    phases.append(PhaseRecord(REVIEW, review_task))

    # Revise -- identical across conditions 2 and 3 (same revise prompt, same
    # injection path); only ``review_text`` above came from a different source.
    revise_filled = fill_prompt(revise_template, review=review_text)
    final_submission = revise(revise_filled)
    phases.append(PhaseRecord(REVISE, revise_filled))

    return ExperimentResult(
        condition=condition,
        phases=phases,
        review_text=review_text,
        final_submission=final_submission,
    )


# ---------------------------------------------------------------------------
# In-sandbox wiring below. Everything past this point imports ``minisweagent``
# (inside the functions) and is exercised only by ``main()`` in the sandbox.
# ---------------------------------------------------------------------------


def _run_shell(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a fixed internal command and capture its output.

    Used only for the review-checkout prep (``rm``/``cp``/``git`` on constant
    paths). ``shell=False`` and the argument vectors are fully constant, so there
    is no untrusted-input execution path here.

    :param args: The argument vector to execute.
    :returns: The completed process (``returncode`` and ``stdout``).
    """
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, no untrusted input
        args,
        capture_output=True,
        text=True,
        check=False,
    )


def run_agent_loop(agent: _Agent) -> dict[str, Any]:
    """Run mini-swe-agent's step loop over *agent* without re-seeding messages.

    A faithful transcription of :meth:`DefaultAgent.run`'s loop body
    (``agents/default.py:96-122`` in v2.4.5): step, reset the consecutive
    format-error counter on a clean step, translate the mini-swe-agent control
    exceptions into appended messages, always persist the trajectory in a
    ``finally``, and break once the last message is an ``exit`` turn. Used for the
    continuation phases, where :meth:`DefaultAgent.run` itself cannot be called
    (it resets ``self.messages``).

    :param agent: The live agent to drive.
    :returns: The exit ``extra`` dict of the final message.
    """
    from minisweagent.exceptions import (  # ty: ignore[unresolved-import]
        FormatError,
        InterruptAgentFlow,
    )

    while True:
        try:
            agent.step()  # ty: ignore[unresolved-attribute]
            agent.n_consecutive_format_errors = 0  # ty: ignore[unresolved-attribute]
        except FormatError as exc:
            agent.n_consecutive_format_errors += 1  # ty: ignore[unresolved-attribute]
            limit = agent.config.max_consecutive_format_errors
            if 0 < limit <= agent.n_consecutive_format_errors:  # ty: ignore[unresolved-attribute]
                agent.add_messages(
                    *exc.messages,
                    {
                        "role": "exit",
                        "content": "RepeatedFormatError",
                        "extra": {
                            "exit_status": "RepeatedFormatError",
                            "submission": "",
                        },
                    },
                )
            else:
                agent.add_messages(*exc.messages)
        except InterruptAgentFlow as exc:
            agent.add_messages(*exc.messages)
        except Exception as exc:
            agent.handle_uncaught_exception(exc)  # ty: ignore[unresolved-attribute]
            raise
        finally:
            agent.save(agent.config.output_path)  # ty: ignore[unresolved-attribute]
        if agent.messages[-1].get("role") == "exit":
            break
    return agent.messages[-1].get("extra", {})


def _build_local_environment(
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
) -> Any:  # noqa: ANN401 -- returns a sandbox-only LocalEnvironment subclass
    """Build a ``LocalEnvironment`` that runs each command under ``bash -c``.

    See :func:`bash_wrap` for *why*: stock ``LocalEnvironment`` uses ``/bin/sh``,
    which ignores ``$BASH_ENV`` and so never runs ``conda activate testbed``. This
    subclass wraps every command via :func:`bash_wrap` so the testbed env is
    activated exactly as the upstream Docker ``bash -c`` path does.

    :param cwd: Working directory for bash actions (``/testbed`` author,
        ``/review`` reviewer).
    :param env: Environment overlay for the process (carries ``BASH_ENV``).
    :param timeout: Per-command timeout in seconds.
    :returns: A ``LocalEnvironment`` instance whose ``execute`` runs under ``bash``.
    """
    from minisweagent.environments.local import (  # ty: ignore[unresolved-import]
        LocalEnvironment,
    )

    class _BashLocalEnvironment(LocalEnvironment):
        """``LocalEnvironment`` that re-execs each command into ``bash -c``."""

        def execute(
            self,
            action: dict[str, Any],
            cwd: str = "",
            *,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            """Run ``action['command']`` under ``bash -c`` (see :func:`bash_wrap`)."""
            wrapped = {**action, "command": bash_wrap(action.get("command", ""))}
            return super().execute(wrapped, cwd, timeout=timeout)

    return _BashLocalEnvironment(cwd=cwd, env=env, timeout=timeout)


def _build_agent(
    *,
    model_name: str,
    cwd: str,
    step_limit: int,
    cost_limit: float,
    output_path: str,
    instance_template: str | None = None,
) -> Any:  # noqa: ANN401 -- returns a minisweagent InteractiveAgent (sandbox-only type)
    """Construct an unattended :class:`InteractiveAgent` on the ``swebench`` base.

    Mirrors the upstream SWE-bench wiring (``run/benchmarks/swebench_single.py``,
    which also uses ``default_type="interactive"``): the config base is
    ``get_config_from_spec("swebench")`` recursively merged with the experiment
    overrides. :class:`InteractiveAgent` is chosen over :class:`DefaultAgent`
    because its ``execute_actions`` pads a tool observation on submit, leaving the
    post-submit history API-valid for continuation.

    ``mode="yolo"`` + ``confirm_exit=False`` make it fully unattended: on submit,
    ``_check_for_new_task_or_submit`` re-raises cleanly instead of prompting (which
    would ``EOFError`` on a ``/dev/null`` stdin). The environment is built by
    :func:`_build_local_environment` (a ``bash -c`` ``LocalEnvironment``, ignoring
    the config's ``environment_class: docker`` -- we are already inside the sandbox,
    but still need ``bash`` so ``$BASH_ENV`` activates the testbed conda env).

    :param model_name: The ``provider/model`` name (author and reviewer share it).
    :param cwd: The working directory for bash actions (``/testbed`` for the author,
        ``/review`` for the reviewer).
    :param step_limit: The per-phase model-call budget.
    :param cost_limit: The per-phase cost budget in USD.
    :param output_path: Where mini-swe-agent continuously saves this agent's
        trajectory.
    :param instance_template: If given, replaces the swebench instance template
        (used for the reviewer: a bare ``{{task}}`` passthrough so the critique is
        not wrapped in the implement-and-submit-a-patch framing).
    :returns: The constructed :class:`InteractiveAgent`.
    """
    from minisweagent.agents.interactive import (  # ty: ignore[unresolved-import]
        InteractiveAgent,
    )
    from minisweagent.config import (  # ty: ignore[unresolved-import]
        get_config_from_spec,
    )
    from minisweagent.models import get_model  # ty: ignore[unresolved-import]
    from minisweagent.utils.serialize import (  # ty: ignore[unresolved-import]
        recursive_merge,
    )

    base = get_config_from_spec("swebench")
    agent_overrides: dict[str, Any] = {
        "step_limit": step_limit,
        "cost_limit": cost_limit,
        "mode": "yolo",
        "confirm_exit": False,
        "output_path": output_path,
    }
    if instance_template is not None:
        agent_overrides["instance_template"] = instance_template
    agent_config = recursive_merge(base.get("agent", {}), agent_overrides)

    model = get_model(
        config=recursive_merge(base.get("model", {}), {"model_name": model_name}),
    )

    env_config = base.get("environment", {})
    env = _build_local_environment(
        cwd=cwd,
        env=env_config.get("env", {}),
        timeout=env_config.get("timeout", 30),
    )
    return InteractiveAgent(model, env, **agent_config)


def _sibling_trajectory(author_trajectory: str, phase: str) -> str:
    """Return a per-phase snapshot path beside the canonical author trajectory.

    :param author_trajectory: The canonical trajectory path (basename
        ``mini-swe-agent.trajectory.json``, read by ``harbor_report``).
    :param phase: The phase name (:data:`IMPLEMENT` / :data:`REVIEW` /
        :data:`REVISE` / :data:`REVIEWER`).
    :returns: ``<dir>/<phase>.trajectory.json``.
    """
    return str(Path(author_trajectory).with_name(f"{phase}.trajectory.json"))


def main(config_path: str) -> ExperimentResult:
    """Wire real ``minisweagent`` objects and run the phases from a config file.

    Reads its inputs from the JSON config the wrapper wrote into the sandbox (task,
    condition, model, bounds, the two registered prompt texts, and the canonical
    author-trajectory path -- everything off argv except the config *path* itself,
    preserving the Fix-2 no-task-in-argv invariant). It constructs the author,
    defines the phase seams, and runs :func:`run_review_experiment`.

    The author's ``output_path`` is the canonical trajectory path, and the loop
    saves after every step, so the canonical file always holds the author's LATEST
    state -- for ``control`` that is the implement result, for the review arms the
    post-revise result -- which is exactly what ``harbor_report`` and the zero-call
    guard read. Per-phase snapshots are written beside it as sibling evidence.

    :param config_path: Path to the JSON config written by the wrapper.
    :returns: The :class:`ExperimentResult` (also useful for smoke assertions).
    """
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    task: str = config["task"]
    condition: str = config["condition"]
    model_name: str = config["model_name"]
    step_limit: int = int(config["step_limit"])
    cost_limit: float = float(config["cost_limit"])
    critique_template: str = config["critique"]
    revise_template: str = config["revise"]
    author_trajectory: str = config["author_trajectory"]

    Path(REVIEW_WORKDIR).mkdir(parents=True, exist_ok=True)

    author = _build_agent(
        model_name=model_name,
        cwd=TESTBED,
        step_limit=step_limit,
        cost_limit=cost_limit,
        output_path=author_trajectory,
    )

    def implement(task_statement: str) -> None:
        author.run(task_statement)
        author.save(Path(_sibling_trajectory(author_trajectory, IMPLEMENT)))

    def compute_diff() -> str:
        return prepare_review_diff(_run_shell)

    def review_in_context(review_task: str) -> str:
        result = continue_agent(
            author,
            review_task,
            step_limit=step_limit,
            cost_limit=cost_limit,
            run_loop=run_agent_loop,
        )
        author.save(Path(_sibling_trajectory(author_trajectory, REVIEW)))
        return extract_review_text(result, author.messages)

    def fresh_review(review_task: str) -> str:
        reviewer_trajectory = _sibling_trajectory(author_trajectory, REVIEWER)
        reviewer = _build_agent(
            model_name=model_name,
            cwd=REVIEW_CHECKOUT,
            step_limit=step_limit,
            cost_limit=cost_limit,
            output_path=reviewer_trajectory,
            instance_template="{{task}}\n",
        )
        result = reviewer.run(review_task)
        reviewer.save(Path(reviewer_trajectory))
        return extract_review_text(result, reviewer.messages)

    def revise(revise_task: str) -> str | None:
        result = continue_agent(
            author,
            revise_task,
            step_limit=step_limit,
            cost_limit=cost_limit,
            run_loop=run_agent_loop,
        )
        author.save(Path(_sibling_trajectory(author_trajectory, REVISE)))
        submission = result.get("submission")
        return str(submission) if submission is not None else None

    return run_review_experiment(
        condition=condition,
        task=task,
        critique_template=critique_template,
        revise_template=revise_template,
        review_footer=REVIEW_SUBMISSION_FOOTER,
        implement=implement,
        compute_diff=compute_diff,
        review_in_context=review_in_context,
        fresh_review=fresh_review,
        revise=revise,
    )


if __name__ == "__main__":
    import sys

    main(sys.argv[1])
