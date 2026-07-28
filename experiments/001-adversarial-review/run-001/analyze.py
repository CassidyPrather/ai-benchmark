"""Pre-registered statistical analysis for Experiment 001, Run 001.

This implements *exactly* the analysis locked in ``PREREGISTRATION.md`` (see its
"Outcomes" and "Analysis" sections) and nothing else. It is run **once**, after
the final batch, over the Harbor job directories under ``jobs/``.

The primary outcome is the per-task, per-condition **PASS_TO_PASS regression
indicator** (``has_regression`` = at least one PASS_TO_PASS test failed in the
graded patch). The analysis is paired within task across the three conditions
``control`` / ``self_review`` / ``adversarial``. A task enters the paired
analysis only when it has a *valid, completed* trial in **all three** conditions
(a "complete triplet"); INVALID zero-call trials and trials with no verifier
report are excluded, and tasks missing any condition are dropped and reported.

Contrasts implemented (all pre-registered):

1. **Primary** -- ``adversarial`` vs ``self_review`` on ``has_regression``:
   McNemar exact test (two-sided binomial on discordant pairs) plus the paired
   difference in regression rate with a task-level bootstrap 95% CI.
2. **Secondary** -- ``adversarial`` vs ``control`` and ``self_review`` vs
   ``control`` (same method), with a Holm-adjusted family for joint claims.
3. **Counts sensitivity** -- Wilcoxon signed-rank on paired regression *counts*
   for the primary pair.
4. **Resolution guardrail (co-primary)** -- resolution and regression rates per
   condition over the complete-triplet tasks, reported side by side. The
   pre-registered guardrail metric is "all FAIL_TO_PASS pass"; the strict
   SWE-bench ``resolved`` (all FAIL_TO_PASS pass AND no PASS_TO_PASS regression)
   is co-reported.
5. **Optional** -- mixed-effects logistic ``regressed ~ C(condition) + (1|task)``
   via ``statsmodels`` if (and only if) it is installed; otherwise skipped.

``scipy``/``numpy`` are intentionally NOT repo dependencies (like ``datasets``),
so run this under an ephemeral overlay from the repo root::

    uv run --with scipy --with numpy python \
        experiments/001-adversarial-review/run-001/analyze.py \
        --out experiments/001-adversarial-review/run-001

Add ``--with statsmodels --with pandas`` to also fit the optional mixed model.
Validate the code without touching real data with ``--self-test`` (synthetic,
in-memory; never reads the filesystem)::

    uv run --with scipy --with numpy python .../analyze.py --self-test
"""

from __future__ import annotations

import argparse
import glob as _glob
import hashlib
import json
import logging
import operator
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np  # ty: ignore[unresolved-import]
from scipy import stats  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Sequence

# The optional mixed-model sensitivity analysis needs statsmodels + pandas. They
# are never required for the primary/secondary results, so their absence is not
# an error -- the analysis simply reports the model as skipped.
try:
    import pandas as pd  # ty: ignore[unresolved-import]
    from statsmodels.genmod.bayes_mixed_glm import (  # ty: ignore[unresolved-import]
        BinomialBayesMixedGLM,
    )
except ImportError:  # pragma: no cover -- optional dependency
    pd = None
    BinomialBayesMixedGLM = None

_HAS_STATSMODELS = pd is not None and BinomialBayesMixedGLM is not None

_logger = logging.getLogger(__name__)

CONTROL = "control"
SELF_REVIEW = "self_review"
ADVERSARIAL = "adversarial"
#: Conditions in a fixed, canonical order (matches the pre-registration).
CONDITIONS = (CONTROL, SELF_REVIEW, ADVERSARIAL)

#: Fixed bootstrap seed, recorded in every output for reproducibility. The value
#: is the pre-registration lock date (2026-07-21) as an integer; it is arbitrary
#: but frozen, so re-running the analysis reproduces the CIs bit-for-bit.
_BOOTSTRAP_SEED = 20260721
#: Bootstrap iterations for the paired rate-difference CIs (pre-reg: >= 10000).
_BOOTSTRAP_ITERS = 10000
#: Default trial-directory glob (relative to the repo root).
_DEFAULT_GLOB = "jobs/run-001-batch*-*/*"

_TESTS_STATUS = "tests_status"
_FAIL_TO_PASS = "FAIL_TO_PASS"  # noqa: S105 -- grading category, not a secret
_PASS_TO_PASS = "PASS_TO_PASS"  # noqa: S105 -- grading category, not a secret
_FAILURE = "failure"

#: mini-swe-agent trajectory filenames, in preference order (live run writes the
#: untrimmed file; committed evidence is trimmed for repo hygiene).
_TRAJECTORY_FILENAMES = (
    "mini-swe-agent.trajectory.json",
    "mini-swe-agent.trajectory.trimmed.json",
)

# Per-task, per-condition availability ranking used for triplet accounting.
_STATUS_OK = "ok"
_STATUS_INVALID = "invalid"
_STATUS_INCOMPLETE = "incomplete"
_STATUS_MISSING = "missing"
_STATUS_RANK = {_STATUS_INCOMPLETE: 0, _STATUS_INVALID: 1, _STATUS_OK: 2}


@dataclass(frozen=True)
class Trial:
    """A single Harbor trial reduced to the fields the pre-reg analysis needs.

    The stats layer operates purely on sequences of these records, so the
    synthetic self-test can construct them directly and never touch the disk.

    :param task: The true SWE-bench instance id (the pairing key).
    :param condition: One of ``control`` / ``self_review`` / ``adversarial``.
    :param valid: Model arm with ``api_calls > 0`` and an assistant turn.
    :param complete: A ``verifier/report.json`` was present for the trial.
    :param has_regression: At least one PASS_TO_PASS test failed.
    :param n_regressions: Number of failed PASS_TO_PASS tests.
    :param resolved: Strict SWE-bench resolution -- all FAIL_TO_PASS pass AND no
        PASS_TO_PASS regression.
    :param f2p_resolved: Pre-reg guardrail resolution -- all FAIL_TO_PASS pass
        (independent of the regression outcome).
    :param trial_dir: Source directory, for provenance (``None`` for synthetic).
    """

    task: str
    condition: str
    valid: bool
    complete: bool
    has_regression: bool
    n_regressions: int
    resolved: bool
    f2p_resolved: bool
    trial_dir: str | None = None


# --------------------------------------------------------------------------- #
# Artifact parsing (mirrors scratchpad/agg_b1.py + harbor_report validity).
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> object | None:
    """Return parsed JSON at *path*, or ``None`` if unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _as_str_dict(obj: object) -> dict[str, Any]:
    """Coerce a JSON object into a ``dict[str, Any]`` (``{}`` if not a mapping)."""
    if isinstance(obj, dict):
        return {str(key): value for key, value in obj.items()}
    return {}


def _report_block(report: object) -> tuple[str | None, dict[str, Any]]:
    """Return ``(instance_id, tests_status)`` from a ``report.json`` payload.

    ``report.json`` is keyed by the true instance id -> ``{tests_status: ...}``;
    that key is the pairing id. A defensive branch also accepts a payload that is
    already the inner block.
    """
    if not isinstance(report, dict):
        return None, {}
    if _TESTS_STATUS in report:
        return None, _as_str_dict(report.get(_TESTS_STATUS))
    for key, value in report.items():
        if isinstance(value, dict) and _TESTS_STATUS in value:
            return str(key), _as_str_dict(value.get(_TESTS_STATUS))
    return None, {}


def _n_failures(status: dict[str, Any], category: str) -> int:
    """Count failing tests for one grading *category* (e.g. PASS_TO_PASS)."""
    side = status.get(category)
    if not isinstance(side, dict):
        return 0
    failures = side.get(_FAILURE)
    return len(failures) if isinstance(failures, list) else 0


def _is_assistant_turn(message: object) -> bool:
    """Whether a trajectory message is a model (assistant) turn.

    Covers both mini-swe-agent wire shapes: chat-completions assistant messages
    and the Responses API (a raw ``response`` object with no ``role``).
    """
    return isinstance(message, dict) and (
        message.get("role") == "assistant" or message.get("object") == "response"
    )


def _trajectory_valid(trial_dir: Path) -> bool:
    """Whether the trial is a valid model arm (``api_calls > 0`` and an turn).

    A missing/unreadable trajectory is treated as INVALID (zero-call): there is
    no evidence any model call was made, exactly the pilot's silent-failure
    shape (HARNESS.md Fix 3).
    """
    agent_dir = trial_dir / "agent"
    for filename in _TRAJECTORY_FILENAMES:
        trajectory = _load_json(agent_dir / filename)
        if not isinstance(trajectory, dict):
            continue
        info = trajectory.get("info")
        model_stats = info.get("model_stats") if isinstance(info, dict) else None
        api_calls = (
            model_stats.get("api_calls") if isinstance(model_stats, dict) else None
        )
        messages = trajectory.get("messages")
        if not isinstance(messages, list):
            messages = []
        has_assistant = any(_is_assistant_turn(message) for message in messages)
        return bool(api_calls) and has_assistant
    return False


def _condition_of(parent_name: str) -> str | None:
    """Extract the condition from a ``run-001-batch<N>-<condition>`` dir name."""
    for condition in CONDITIONS:
        if parent_name.endswith(condition):
            return condition
    return None


def parse_trial_dir(trial_dir: Path) -> Trial | None:
    """Parse one ``<instance_id>__<hash>`` trial directory into a :class:`Trial`.

    Returns ``None`` when the parent directory is not a recognised condition
    directory. Incomplete (no ``report.json``) and INVALID (zero-call) trials are
    still returned -- with ``complete``/``valid`` set accordingly -- so triplet
    completeness can account for them.
    """
    condition = _condition_of(trial_dir.parent.name)
    if condition is None:
        return None

    # The dir is ``<instance_id>__<hash>``; instance ids themselves contain
    # ``__`` (e.g. ``django__django-11149``), so split off only the last field.
    fallback_task = trial_dir.name.rsplit("__", 1)[0]
    report = _load_json(trial_dir / "verifier" / "report.json")
    if report is None:
        return Trial(
            task=fallback_task,
            condition=condition,
            valid=False,
            complete=False,
            has_regression=False,
            n_regressions=0,
            resolved=False,
            f2p_resolved=False,
            trial_dir=str(trial_dir),
        )

    instance_id, status = _report_block(report)
    task = instance_id or fallback_task
    p2p_fail = _n_failures(status, _PASS_TO_PASS)
    f2p_fail = _n_failures(status, _FAIL_TO_PASS)
    return Trial(
        task=task,
        condition=condition,
        valid=_trajectory_valid(trial_dir),
        complete=True,
        has_regression=p2p_fail > 0,
        n_regressions=p2p_fail,
        resolved=f2p_fail == 0 and p2p_fail == 0,
        f2p_resolved=f2p_fail == 0,
        trial_dir=str(trial_dir),
    )


def load_trials(pattern: str) -> list[Trial]:
    """Load every trial directory matched by *pattern* into :class:`Trial`s."""
    matches = _glob.glob(pattern)  # noqa: PTH207 -- arbitrary CLI glob string
    trials: list[Trial] = []
    for match in sorted(matches):
        path = Path(match)
        if not path.is_dir():
            continue
        trial = parse_trial_dir(path)
        if trial is not None:
            trials.append(trial)
    return trials


# --------------------------------------------------------------------------- #
# Core statistics (operate on Trial sequences -- dependency-injectable).
# --------------------------------------------------------------------------- #


def mcnemar_pvalue(
    treat_flags: Sequence[bool],
    base_flags: Sequence[bool],
) -> tuple[int, int, float]:
    """Return ``(b, c, p)`` for the paired McNemar exact test.

    ``b`` = tasks where the *baseline* regressed but the *treatment* did not;
    ``c`` = the reverse. ``p`` is the two-sided exact binomial test on the
    discordant pairs (``binomtest(min(b, c), b + c, 0.5)``); when there are no
    discordant pairs the test is undefined and ``p`` is reported as ``1.0``.
    """
    pairs = list(zip(treat_flags, base_flags, strict=True))
    b = sum(1 for treat, base in pairs if base and not treat)
    c = sum(1 for treat, base in pairs if treat and not base)
    n_discordant = b + c
    if n_discordant == 0:
        return b, c, 1.0
    p = stats.binomtest(min(b, c), n_discordant, 0.5, alternative="two-sided").pvalue
    return b, c, float(p)


def bootstrap_diff_ci(
    treat_flags: Sequence[bool],
    base_flags: Sequence[bool],
    *,
    n_boot: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    """Task-level bootstrap 95% percentile CI for ``mean(treat) - mean(base)``.

    Resamples the paired tasks (rows) with replacement so each draw keeps a
    task's treatment and baseline indicators together, recomputes the paired
    rate difference, and returns the (2.5, 97.5) percentiles.
    """
    treat = np.asarray(treat_flags, dtype=float)
    base = np.asarray(base_flags, dtype=float)
    n = treat.size
    if n == 0:
        return (float("nan"), float("nan"))
    idx = rng.integers(0, n, size=(n_boot, n))
    diffs = treat[idx].mean(axis=1) - base[idx].mean(axis=1)
    low, high = np.percentile(diffs, [2.5, 97.5])
    return (float(low), float(high))


def wilcoxon_counts(
    treat_counts: Sequence[int],
    base_counts: Sequence[int],
) -> dict[str, Any]:
    """Wilcoxon signed-rank on paired regression *counts* (primary pair)."""
    treat = np.asarray(treat_counts, dtype=float)
    base = np.asarray(base_counts, dtype=float)
    diffs = treat - base
    if diffs.size == 0 or bool(np.all(diffs == 0)):
        return {
            "statistic": None,
            "p_value": 1.0,
            "note": "no non-zero paired differences; test undefined",
        }
    try:
        result = stats.wilcoxon(treat, base)
    except ValueError as exc:  # e.g. all-zero after dropping ties
        return {"statistic": None, "p_value": 1.0, "note": str(exc)}
    return {"statistic": float(result.statistic), "p_value": float(result.pvalue)}


def holm_adjust(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values for a family of contrasts."""
    ordered = sorted(pvalues.items(), key=operator.itemgetter(1))
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * p))
        adjusted[name] = running
    return adjusted


def _mean(flags: Sequence[bool]) -> float:
    """Mean of a boolean sequence (0.0 for an empty sequence)."""
    return sum(flags) / len(flags) if flags else 0.0


def _contrast(
    by_task: dict[str, dict[str, Trial]],
    triplet_tasks: Sequence[str],
    treatment: str,
    baseline: str,
    *,
    n_boot: int,
    seed: int,
    stream: int,
    with_counts: bool,
) -> dict[str, Any]:
    """Compute one paired contrast (McNemar + bootstrap [+ Wilcoxon])."""
    treat_reg = [by_task[task][treatment].has_regression for task in triplet_tasks]
    base_reg = [by_task[task][baseline].has_regression for task in triplet_tasks]

    pairs = list(zip(treat_reg, base_reg, strict=True))
    both = sum(1 for t, b in pairs if t and b)
    treat_only = sum(1 for t, b in pairs if t and not b)
    base_only = sum(1 for t, b in pairs if b and not t)
    neither = sum(1 for t, b in pairs if not t and not b)

    b, c, p = mcnemar_pvalue(treat_reg, base_reg)
    treat_rate = _mean(treat_reg)
    base_rate = _mean(base_reg)
    rng = np.random.default_rng([seed, stream])
    low, high = bootstrap_diff_ci(treat_reg, base_reg, n_boot=n_boot, rng=rng)

    contrast: dict[str, Any] = {
        "treatment": treatment,
        "baseline": baseline,
        "n_tasks": len(triplet_tasks),
        "table": {
            "both_regress": both,
            "treatment_only_regress": treat_only,
            "baseline_only_regress": base_only,
            "neither_regress": neither,
        },
        "mcnemar": {
            "b_baseline_only": b,
            "c_treatment_only": c,
            "n_discordant": b + c,
            "p_value": p,
        },
        "rate_diff": {
            "treatment_rate": treat_rate,
            "baseline_rate": base_rate,
            "estimate": treat_rate - base_rate,
            "ci_lower": low,
            "ci_upper": high,
            "ci_level": 0.95,
        },
        "bootstrap_seed_stream": [seed, stream],
    }
    if with_counts:
        treat_counts = [
            by_task[task][treatment].n_regressions for task in triplet_tasks
        ]
        base_counts = [by_task[task][baseline].n_regressions for task in triplet_tasks]
        contrast["wilcoxon_counts"] = wilcoxon_counts(treat_counts, base_counts)
    return contrast


def _triplet_accounting(
    trials: Sequence[Trial],
) -> tuple[
    dict[str, dict[str, Trial]], list[str], dict[str, dict[str, str]], list[list[str]]
]:
    """Group valid+complete trials by task and resolve triplet completeness.

    :returns: ``(by_task, triplet_tasks, dropped, collisions)`` where ``by_task``
        maps task -> condition -> the valid+complete :class:`Trial`;
        ``triplet_tasks`` is the sorted list of complete triplets; ``dropped``
        maps each incomplete-triplet task to its per-condition status; and
        ``collisions`` lists any duplicate ``(task, condition)`` pairs seen.
    """
    by_task: dict[str, dict[str, Trial]] = {}
    collisions: list[list[str]] = []
    status: dict[str, dict[str, str]] = {}

    for trial in trials:
        if trial.complete and trial.valid:
            new = _STATUS_OK
        elif trial.complete:
            new = _STATUS_INVALID
        else:
            new = _STATUS_INCOMPLETE
        current = status.setdefault(trial.task, {}).get(trial.condition)
        if current is None or _STATUS_RANK[new] > _STATUS_RANK[current]:
            status[trial.task][trial.condition] = new
        if new == _STATUS_OK:
            conditions = by_task.setdefault(trial.task, {})
            if trial.condition in conditions:
                collisions.append([trial.task, trial.condition])
            conditions[trial.condition] = trial

    triplet_tasks = sorted(
        task
        for task, conds in status.items()
        if all(conds.get(condition) == _STATUS_OK for condition in CONDITIONS)
    )
    triplet_set = set(triplet_tasks)
    dropped = {
        task: {
            condition: conds.get(condition, _STATUS_MISSING) for condition in CONDITIONS
        }
        for task, conds in sorted(status.items())
        if task not in triplet_set
    }
    return by_task, triplet_tasks, dropped, collisions


def _pool_fingerprint(
    by_task: dict[str, dict[str, Trial]],
    triplet_tasks: Sequence[str],
) -> str:
    """SHA-256 over the analysed triplet data (task, condition, outcomes)."""
    rows = [
        [
            task,
            condition,
            by_task[task][condition].has_regression,
            by_task[task][condition].n_regressions,
            by_task[task][condition].resolved,
            by_task[task][condition].f2p_resolved,
        ]
        for task in triplet_tasks
        for condition in CONDITIONS
    ]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mixed_model(
    by_task: dict[str, dict[str, Trial]],
    triplet_tasks: Sequence[str],
) -> dict[str, Any]:
    """Fit an optional mixed logistic ``regressed ~ C(condition) + (1|task)``.

    Best-effort and non-blocking: skipped (not failed) when statsmodels/pandas
    are absent or the fit raises.
    """
    if not _HAS_STATSMODELS or pd is None or BinomialBayesMixedGLM is None:
        return {"available": False, "reason": "statsmodels/pandas not installed"}
    if not triplet_tasks:
        return {"available": False, "reason": "no complete triplets"}
    rows = [
        {
            "task": task,
            "condition": condition,
            "regressed": int(by_task[task][condition].has_regression),
        }
        for task in triplet_tasks
        for condition in CONDITIONS
    ]
    formula = "regressed ~ C(condition, Treatment(reference='control'))"
    try:
        model = BinomialBayesMixedGLM.from_formula(
            formula,
            {"task": "0 + C(task)"},
            pd.DataFrame(rows),
        )
        result = model.fit_vb()
        names = list(model.exog_names)
        coefficients = {
            name: {"posterior_mean": float(mean), "posterior_sd": float(sd)}
            for name, mean, sd in zip(names, result.fe_mean, result.fe_sd, strict=False)
        }
    except Exception as exc:  # noqa: BLE001 -- optional, must never block
        return {"available": False, "reason": f"fit failed: {exc}"}
    return {
        "available": True,
        "method": "BinomialBayesMixedGLM.fit_vb (variational Bayes)",
        "reference_condition": CONTROL,
        "fixed_effects": coefficients,
    }


def analyze(
    trials: Sequence[Trial],
    *,
    n_boot: int = _BOOTSTRAP_ITERS,
    seed: int = _BOOTSTRAP_SEED,
    source: str = "unknown",
    with_mixed_model: bool = True,
) -> dict[str, Any]:
    """Run the full pre-registered analysis over a sequence of trials.

    This is the dependency-injected entry point: it takes :class:`Trial` records
    (from disk or synthetic) and returns a JSON-serialisable result dict.
    """
    trials = list(trials)
    n_incomplete = sum(1 for trial in trials if not trial.complete)
    n_invalid = sum(1 for trial in trials if trial.complete and not trial.valid)
    by_task, triplet_tasks, dropped, collisions = _triplet_accounting(trials)
    n_pool = sum(1 for trial in trials if trial.complete and trial.valid)

    per_condition: dict[str, Any] = {}
    for condition in CONDITIONS:
        reg = [by_task[task][condition].has_regression for task in triplet_tasks]
        f2p = [by_task[task][condition].f2p_resolved for task in triplet_tasks]
        strict = [by_task[task][condition].resolved for task in triplet_tasks]
        per_condition[condition] = {
            "n": len(triplet_tasks),
            "n_regressed": int(sum(reg)),
            "regression_rate": _mean(reg),
            "n_f2p_resolved": int(sum(f2p)),
            "f2p_resolution_rate": _mean(f2p),
            "n_resolved_strict": int(sum(strict)),
            "strict_resolution_rate": _mean(strict),
        }

    primary = _contrast(
        by_task,
        triplet_tasks,
        ADVERSARIAL,
        SELF_REVIEW,
        n_boot=n_boot,
        seed=seed,
        stream=0,
        with_counts=True,
    )
    secondary = {
        "adversarial_vs_control": _contrast(
            by_task,
            triplet_tasks,
            ADVERSARIAL,
            CONTROL,
            n_boot=n_boot,
            seed=seed,
            stream=1,
            with_counts=False,
        ),
        "self_review_vs_control": _contrast(
            by_task,
            triplet_tasks,
            SELF_REVIEW,
            CONTROL,
            n_boot=n_boot,
            seed=seed,
            stream=2,
            with_counts=False,
        ),
    }
    family_raw = {
        "adversarial_vs_self_review": primary["mcnemar"]["p_value"],
        "adversarial_vs_control": secondary["adversarial_vs_control"]["mcnemar"][
            "p_value"
        ],
        "self_review_vs_control": secondary["self_review_vs_control"]["mcnemar"][
            "p_value"
        ],
    }
    family_holm = holm_adjust(family_raw)
    holm_family = {
        name: {"p_raw": family_raw[name], "p_holm": family_holm[name]}
        for name in family_raw
    }

    mixed = (
        _mixed_model(by_task, triplet_tasks)
        if with_mixed_model
        else {"available": False, "reason": "disabled"}
    )

    return {
        "provenance": {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": source,
            "pool_fingerprint": _pool_fingerprint(by_task, triplet_tasks),
            "bootstrap_seed": seed,
            "bootstrap_iterations": n_boot,
            "conditions": list(CONDITIONS),
            "statsmodels_available": _HAS_STATSMODELS,
            "n_trials_total": len(trials),
            "n_trials_incomplete": n_incomplete,
            "n_trials_invalid": n_invalid,
            "n_trials_pool": n_pool,
            "n_tasks_complete_triplet": len(triplet_tasks),
            "n_dropped": len(dropped),
            "dropped_tasks": dropped,
            "collisions": collisions,
        },
        "per_condition": per_condition,
        "primary": primary,
        "secondary": secondary,
        "holm_family": holm_family,
        "mixed_model": mixed,
    }


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _fmt_p(value: float | None) -> str:
    """Format a p-value for the report."""
    return "n/a" if value is None else f"{value:.4g}"


def _fmt_rate(rate: float, count: int, total: int) -> str:
    """Format a rate as ``0.250 (5/20)``."""
    return f"{rate:.3f} ({count}/{total})"


def _render_contrast(title: str, contrast: dict[str, Any]) -> list[str]:
    """Render one contrast to Markdown lines."""
    table = contrast["table"]
    mcnemar = contrast["mcnemar"]
    diff = contrast["rate_diff"]
    treat = contrast["treatment"]
    base = contrast["baseline"]
    ci = f"[{diff['ci_lower']:+.3f}, {diff['ci_upper']:+.3f}]"
    lines = [
        f"### {title}",
        "",
        (
            f"Treatment = `{treat}`, baseline = `{base}`, paired over "
            f"{contrast['n_tasks']} complete-triplet tasks."
        ),
        "",
        f"| | {base} regressed | {base} clean |",
        "| --- | --- | --- |",
        (
            f"| **{treat} regressed** | {table['both_regress']} "
            f"| {table['treatment_only_regress']} |"
        ),
        (
            f"| **{treat} clean** | {table['baseline_only_regress']} "
            f"| {table['neither_regress']} |"
        ),
        "",
        (
            f"- McNemar exact (two-sided binomial on discordant pairs): "
            f"b (baseline-only) = {mcnemar['b_baseline_only']}, "
            f"c (treatment-only) = {mcnemar['c_treatment_only']}, "
            f"discordant = {mcnemar['n_discordant']}, "
            f"**p = {_fmt_p(mcnemar['p_value'])}**."
        ),
        (
            f"- Regression rate: {treat} = {diff['treatment_rate']:.3f}, "
            f"{base} = {diff['baseline_rate']:.3f}."
        ),
        (
            f"- Paired difference ({treat} - {base}) = "
            f"**{diff['estimate']:+.3f}** (bootstrap 95% CI {ci})."
        ),
    ]
    if "wilcoxon_counts" in contrast:
        wilcoxon = contrast["wilcoxon_counts"]
        note = f" ({wilcoxon['note']})" if wilcoxon.get("note") else ""
        lines.append(
            f"- Wilcoxon signed-rank on paired regression counts: "
            f"statistic = {wilcoxon['statistic']}, "
            f"p = {_fmt_p(wilcoxon['p_value'])}{note}.",
        )
    lines.append("")
    return lines


def _render_header(prov: dict[str, Any]) -> list[str]:
    """Render the title, provenance, and triplet-completeness sections."""
    intro = (
        "Pre-registered analysis (`PREREGISTRATION.md`), run once over the final "
        "data. Primary outcome: PASS_TO_PASS regression indicator, paired within "
        "task. Read every regression result alongside the resolution guardrail -- "
        "an arm that regresses less by solving less is not evidence for H1."
    )
    seed_line = (
        f"- Bootstrap seed: `{prov['bootstrap_seed']}` "
        f"({prov['bootstrap_iterations']} iterations, percentile CIs)"
    )
    n_triplet = prov["n_tasks_complete_triplet"]
    lines = [
        "# Experiment 001 - Run 001 results",
        "",
        intro,
        "",
        "## Provenance",
        "",
        f"- Source glob / label: `{prov['source']}`",
        f"- Pool fingerprint (sha256): `{prov['pool_fingerprint']}`",
        seed_line,
        f"- statsmodels available: {prov['statsmodels_available']}",
        f"- Generated: {prov['generated_at']}",
        "",
        "## Triplet completeness",
        "",
        f"- Trial dirs parsed: {prov['n_trials_total']}",
        f"- Incomplete (no verifier report, excluded): {prov['n_trials_incomplete']}",
        f"- INVALID zero-call (excluded): {prov['n_trials_invalid']}",
        f"- Valid + complete trials in pool: {prov['n_trials_pool']}",
        f"- **Complete triplets (all 3 conditions): {n_triplet}**",
        f"- Tasks dropped (missing >=1 condition): {prov['n_dropped']}",
        "",
    ]
    if prov["collisions"]:
        lines.extend([
            f"- WARNING: duplicate (task, condition) pairs: {prov['collisions']}",
            "",
        ])
    if prov["dropped_tasks"]:
        lines.extend([
            "Dropped tasks (per-condition status):",
            "",
            "| Task | control | self_review | adversarial |",
            "| --- | --- | --- | --- |",
        ])
        for task, status in prov["dropped_tasks"].items():
            cells = f"{status[CONTROL]} | {status[SELF_REVIEW]} | {status[ADVERSARIAL]}"
            lines.append(f"| {task} | {cells} |")
        lines.append("")
    return lines


def _render_per_condition(per_condition: dict[str, Any]) -> list[str]:
    """Render the per-condition regression/resolution rate table."""
    note = (
        "Over the complete-triplet tasks. `regression rate` is the primary "
        "outcome; `resolution (F2P)` is the pre-registered guardrail (all "
        "FAIL_TO_PASS pass); `resolved (strict)` also requires no regression."
    )
    lines = [
        "## Per-condition rates (guardrail, co-primary)",
        "",
        note,
        "",
        "| Condition | n | Regression rate | Resolution (F2P) | Resolved (strict) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for condition in CONDITIONS:
        row = per_condition[condition]
        total = row["n"]
        reg = _fmt_rate(row["regression_rate"], row["n_regressed"], total)
        f2p = _fmt_rate(row["f2p_resolution_rate"], row["n_f2p_resolved"], total)
        strict = _fmt_rate(
            row["strict_resolution_rate"], row["n_resolved_strict"], total
        )
        lines.append(f"| {condition} | {total} | {reg} | {f2p} | {strict} |")
    lines.append("")
    return lines


def _render_mixed(mixed: dict[str, Any]) -> list[str]:
    """Render the optional mixed-model section."""
    lines = ["## Mixed-effects logistic (optional sensitivity)", ""]
    if not mixed.get("available"):
        lines.extend([f"Skipped: {mixed.get('reason', 'unavailable')}.", ""])
        return lines
    lines.extend([
        f"`regressed ~ C(condition) + (1|task)` via {mixed['method']}.",
        "",
        "| Fixed effect | Posterior mean | Posterior sd |",
        "| --- | --- | --- |",
    ])
    for name, coef in mixed["fixed_effects"].items():
        lines.append(
            f"| {name} | {coef['posterior_mean']:+.4f} | {coef['posterior_sd']:.4f} |",
        )
    lines.append("")
    return lines


def render_markdown(result: dict[str, Any]) -> str:
    """Render the full analysis result as a Markdown report."""
    secondary = result["secondary"]
    lines = _render_header(result["provenance"])
    lines.extend(_render_per_condition(result["per_condition"]))

    lines.extend(["## Primary contrast", ""])
    lines.extend(
        _render_contrast("adversarial vs self_review (primary, H1)", result["primary"]),
    )

    lines.extend(["## Secondary contrasts", ""])
    lines.extend(
        _render_contrast("adversarial vs control", secondary["adversarial_vs_control"]),
    )
    lines.extend(
        _render_contrast("self_review vs control", secondary["self_review_vs_control"]),
    )

    lines.extend([
        "## Family multiplicity (Holm), for joint claims",
        "",
        "| Contrast | McNemar p (raw) | Holm-adjusted |",
        "| --- | --- | --- |",
    ])
    for name, entry in result["holm_family"].items():
        lines.append(
            f"| {name} | {_fmt_p(entry['p_raw'])} | {_fmt_p(entry['p_holm'])} |"
        )
    lines.append("")

    lines.extend(_render_mixed(result["mixed_model"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def _run_analysis(pattern: str, out_dir: Path | None, n_boot: int) -> int:
    """Load, analyse, render, and optionally write the results."""
    trials = load_trials(pattern)
    if not trials:
        _logger.error("No trial directories matched: %s", pattern)
        return 1
    _logger.info("Parsed %d trial directory(ies) from %s", len(trials), pattern)
    result = analyze(trials, n_boot=n_boot, source=pattern)
    markdown = render_markdown(result)
    payload = json.dumps(result, indent=2)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run-001-results.md").write_text(markdown + "\n", encoding="utf-8")
        (out_dir / "run-001-results.json").write_text(payload + "\n", encoding="utf-8")
        _logger.info("Wrote run-001-results.{md,json} to %s", out_dir)
    print(markdown)  # noqa: T201 -- primary CLI output
    return 0


# --------------------------------------------------------------------------- #
# Synthetic self-test (no filesystem access).
# --------------------------------------------------------------------------- #


def _require(*, ok: bool, detail: str) -> None:
    """Raise :class:`AssertionError` with *detail* when *ok* is false."""
    if not ok:
        raise AssertionError(detail)


def _synthetic_trials(
    n_tasks: int,
    regression_rates: dict[str, float],
    resolution_rates: dict[str, float],
    *,
    seed: int,
    rho: float = 0.4,
) -> list[Trial]:
    """Build synthetic paired trials with planted per-condition regression rates.

    A shared per-task latent (Gaussian, correlation ``rho`` across conditions)
    plus Gaussian thresholds reproduces the marginal regression rates exactly in
    expectation while inducing realistic *discordance* between conditions.
    """
    rng = np.random.default_rng(seed)
    thresholds = {
        c: float(stats.norm.ppf(rate)) for c, rate in regression_rates.items()
    }
    trials: list[Trial] = []
    for i in range(n_tasks):
        task = f"synthetic-task-{i:04d}"
        z_task = float(rng.standard_normal())
        for condition in CONDITIONS:
            z = (rho**0.5) * z_task + ((1.0 - rho) ** 0.5) * float(
                rng.standard_normal()
            )
            regressed = z < thresholds[condition]
            n_reg = int(1 + rng.poisson(1.0)) if regressed else 0
            f2p_ok = bool(rng.random() < resolution_rates[condition])
            trials.append(
                Trial(
                    task=task,
                    condition=condition,
                    valid=True,
                    complete=True,
                    has_regression=regressed,
                    n_regressions=n_reg,
                    resolved=f2p_ok and not regressed,
                    f2p_resolved=f2p_ok,
                ),
            )
    return trials


def _self_test() -> int:
    """Validate the stats on synthetic data with known planted effects."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s: %(message)s", force=True
    )
    resolution = {CONTROL: 0.55, SELF_REVIEW: 0.50, ADVERSARIAL: 0.42}

    # 1. Large planted effect: adversarial (0.12) < self_review (0.30) < control (0.35).
    planted = {CONTROL: 0.35, SELF_REVIEW: 0.30, ADVERSARIAL: 0.12}
    trials = _synthetic_trials(400, planted, resolution, seed=1)
    result = analyze(
        trials, n_boot=10000, seed=_BOOTSTRAP_SEED, source="synthetic:effect"
    )
    per = result["per_condition"]
    prov = result["provenance"]
    primary = result["primary"]

    print("[self-test] recovered regression rates (planted in parens):")  # noqa: T201
    for condition in CONDITIONS:
        print(  # noqa: T201
            f"    {condition:12s} {per[condition]['regression_rate']:.3f} "
            f"(planted {planted[condition]:.2f})",
        )
    for condition in CONDITIONS:
        _require(
            ok=abs(per[condition]["regression_rate"] - planted[condition]) < 0.06,
            detail=f"{condition} rate {per[condition]['regression_rate']} off planted",
        )
    _require(
        ok=prov["n_tasks_complete_triplet"] == 400 and prov["n_dropped"] == 0,
        detail="expected 400 complete triplets, 0 dropped",
    )
    _require(ok=prov["n_trials_pool"] == 1200, detail="expected 1200 pooled trials")

    diff = primary["rate_diff"]
    mcnemar = primary["mcnemar"]
    print(  # noqa: T201
        f"[self-test] primary adv-vs-sr: diff={diff['estimate']:+.3f} "
        f"CI[{diff['ci_lower']:+.3f},{diff['ci_upper']:+.3f}] "
        f"McNemar p={mcnemar['p_value']:.3g} "
        f"(b={mcnemar['b_baseline_only']}, c={mcnemar['c_treatment_only']})",
    )
    _require(ok=diff["estimate"] < 0, detail="adversarial should regress less (diff<0)")
    _require(
        ok=diff["ci_upper"] < 0, detail="bootstrap CI should exclude 0 for large effect"
    )
    _require(
        ok=mcnemar["p_value"] < 0.01,
        detail="McNemar p should be small for large effect",
    )

    wilcoxon = primary["wilcoxon_counts"]
    print(f"[self-test] primary Wilcoxon counts p={wilcoxon['p_value']:.3g}")  # noqa: T201
    _require(
        ok=wilcoxon["p_value"] < 0.01,
        detail="Wilcoxon p should be small for large effect",
    )

    adv_control = result["secondary"]["adversarial_vs_control"]
    _require(
        ok=adv_control["rate_diff"]["estimate"] < 0
        and adv_control["rate_diff"]["ci_upper"] < 0,
        detail="adversarial should regress less than control",
    )
    _require(
        ok=result["secondary"]["self_review_vs_control"]["rate_diff"]["estimate"] < 0,
        detail="self_review should trend below control",
    )

    # Guardrail sanity: resolution rates recovered near planted, adversarial lower.
    print(  # noqa: T201
        "[self-test] resolution (F2P) rates: "
        + ", ".join(f"{c}={per[c]['f2p_resolution_rate']:.3f}" for c in CONDITIONS),
    )
    _require(
        ok=abs(per[CONTROL]["f2p_resolution_rate"] - resolution[CONTROL]) < 0.08,
        detail="control resolution rate off planted",
    )

    # 2. Dropped-triplet accounting: inject a broken task.
    broken = [
        Trial(
            "broken-task",
            CONTROL,
            valid=True,
            complete=True,
            has_regression=False,
            n_regressions=0,
            resolved=True,
            f2p_resolved=True,
        ),
        Trial(
            "broken-task",
            SELF_REVIEW,
            valid=False,
            complete=True,
            has_regression=True,
            n_regressions=2,
            resolved=False,
            f2p_resolved=False,
        ),  # INVALID
        Trial(
            "broken-task",
            ADVERSARIAL,
            valid=True,
            complete=False,
            has_regression=False,
            n_regressions=0,
            resolved=False,
            f2p_resolved=False,
        ),  # incomplete
    ]
    dropped_result = analyze(
        trials[:30] + broken, n_boot=1000, source="synthetic:dropped"
    )
    dprov = dropped_result["provenance"]
    _require(ok=dprov["n_dropped"] == 1, detail="broken task should be dropped")
    _require(
        ok="broken-task" in dprov["dropped_tasks"],
        detail="dropped task not recorded in provenance",
    )
    reasons = dprov["dropped_tasks"]["broken-task"]
    _require(
        ok=reasons[SELF_REVIEW] == _STATUS_INVALID
        and reasons[ADVERSARIAL] == _STATUS_INCOMPLETE,
        detail=f"unexpected drop reasons: {reasons}",
    )
    _require(
        ok=dprov["n_tasks_complete_triplet"] == 10,
        detail="expected 10 triplets after slice",
    )
    print(f"[self-test] dropped-task accounting OK (reasons={reasons})")  # noqa: T201

    # 3. Null effect: p-values should be ~uniform (not concentrated near 0).
    null_rates = {CONTROL: 0.30, SELF_REVIEW: 0.30, ADVERSARIAL: 0.30}
    n_reps = 300
    pvalues = np.empty(n_reps)
    for rep in range(n_reps):
        null_trials = _synthetic_trials(120, null_rates, resolution, seed=1000 + rep)
        by_task, triplet, _drop, _coll = _triplet_accounting(null_trials)
        treat = [by_task[t][ADVERSARIAL].has_regression for t in triplet]
        base = [by_task[t][SELF_REVIEW].has_regression for t in triplet]
        _b, _c, pvalues[rep] = mcnemar_pvalue(treat, base)
    frac_sig = float(np.mean(pvalues < 0.05))
    mean_p = float(np.mean(pvalues))
    print(  # noqa: T201
        f"[self-test] null: mean p={mean_p:.3f}, fraction p<0.05={frac_sig:.3f} "
        f"over {n_reps} replicates",
    )
    _require(ok=mean_p > 0.35, detail=f"null mean p unexpectedly low ({mean_p})")
    _require(
        ok=frac_sig < 0.12, detail=f"null false-positive rate too high ({frac_sig})"
    )

    print(f"[self-test] pool fingerprint (effect dataset): {prov['pool_fingerprint']}")  # noqa: T201
    print("[self-test] ALL CHECKS PASSED")  # noqa: T201
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Pre-registered analysis for Experiment 001, Run 001.",
    )
    parser.add_argument(
        "glob",
        nargs="?",
        default=_DEFAULT_GLOB,
        help=f"Trial-directory glob (default: {_DEFAULT_GLOB})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory to write run-001-results.{md,json}",
    )
    parser.add_argument(
        "--bootstrap-iters",
        type=int,
        default=_BOOTSTRAP_ITERS,
        help=f"Bootstrap iterations (default: {_BOOTSTRAP_ITERS})",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the synthetic self-test (no filesystem access) and exit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args(argv)
    if args.self_test:
        return _self_test()
    return _run_analysis(args.glob, args.out, args.bootstrap_iters)


if __name__ == "__main__":
    sys.exit(main())
