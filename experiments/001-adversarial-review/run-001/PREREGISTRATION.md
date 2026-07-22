# Experiment 001 — Run 001 pre-registration

**Locked 2026-07-21, before any treatment trial.** Committed prior to the first
`ExperimentReviewAgent` run so the task set, outcomes, analysis, continuation
rule, and budget are fixed in advance and cannot be reverse-engineered from
results. Any change after data collection begins is an **amendment** — dated,
justified, appended below, never a silent edit.

This run is an **internal pilot with sample-size re-estimation**: batch 1
estimates the *nuisance* parameter (the regression base rate) to size a
conclusive run; the treatment *contrast* is never used to decide whether or how
far to continue. See [Continuation rule](#mechanical-continuation-rule--the-insulation).

## Hypothesis

- **H1 (primary).** Blinded adversarial review (condition `adversarial`) produces
  fewer PASS_TO_PASS regressions than non-blinded self-review (condition
  `self_review`).
- **Secondary.** Each review arm vs. `control` (does any review reduce regressions).

## Conditions — the only manipulated variable

`control` / `self_review` / `adversarial`, selected via
`ExperimentReviewAgent --ak condition=<name>`. Everything else is held constant.
Definitions and machinery: [`../DESIGN.md`](../DESIGN.md) § Conditions,
[`../HARNESS.md`](../HARNESS.md).

## Fixed harness (frozen for the whole run)

| Component | Value |
| --- | --- |
| Harbor | `0.18.0` |
| Agent | `ai_benchmark.live_agents:ExperimentReviewAgent` (mini-swe-agent 2.4.5 + `litellm[proxy]`) |
| Author base config | `swebench.yaml` pinned to mini-swe-agent v2.4.5 commit `e187bcb2ff5825d85761a6f9c1f98c9fa6cfbc79` (blob `106decd1…`) |
| Model | `openrouter/qwen/qwen3-coder` (author and reviewer; same-model design) |
| `step_limit` / `cost_limit` | `100` / `1.0` (per phase) |
| Prompts | committed [`../prompts/critique.txt`](../prompts/critique.txt), [`../prompts/revise.txt`](../prompts/revise.txt) (see PROVENANCE) |
| Environment | Daytona, `-n 4` concurrent |
| Dataset | `swebench-verified@1.0`; instance-id fingerprint `fad0fdea4fc2315e9b78cdf80882a32e32393297052e502e0e63c79ad648fb85` |

## Task pool (frozen)

[`task-pool-django.json`](task-pool-django.json) — **80 Django tasks**, chosen
Django-only to de-risk the unattended run against Daytona build failures
(generalizability to other repos is deferred to follow-up research, per DESIGN §
Task source). Inclusion filter: `n_PASS_TO_PASS >= 1` AND not trivial
(`files == 1 AND total_changed_lines < 5`). **Canonical order is a fixed seeded
permutation** (`sha256(f"{instance_id}:seed42")`, ascending), so every prefix
batch is a homogeneous random sample of the enriched pool — no ordering bias by
regression-proneness. Batches are contiguous slices of 20 in seeded order:
**batch N = seeded_rank in `((N-1)*20, N*20]`**.

## Outcomes (pre-specified)

- **Primary:** per-task, per-condition **P2P-regression indicator** = (number of
  PASS_TO_PASS test *failures* > 0) in the final graded patch, taken from
  `verifier/report.json` via `ai-benchmark harbor-report` (per-test name lists,
  not a flattened bit).
- **Guardrail (co-reported, load-bearing):** per-task **resolution** = all
  FAIL_TO_PASS pass. A do-nothing or over-conservative patch trivially avoids
  regressions, so **H1 is interpreted only alongside comparable resolution
  rates**; a review arm that regresses less *by solving less* is not evidence for
  H1. A secondary analysis conditions on "final patch changed ≥ 1 tracked file."
- **Secondary:** regression *count* (not just indicator); resolution rate;
  per-trial validity verdict (zero-call guard).

## Analysis (pre-specified; run ONCE, after the run stops)

- **Primary contrast:** `adversarial` vs `self_review`, paired within task —
  **McNemar exact test** on the regression indicator over discordant pairs, plus
  the paired difference in regression rate with a **task-level bootstrap 95% CI**.
- **Secondary contrasts:** `adversarial` vs `control`; `self_review` vs `control`
  (same method). Family multiplicity handled with Holm if a joint claim is made;
  the primary is the single `adversarial`-vs-`self_review` contrast.
- **Model-based sensitivity:** mixed-effects logistic regression
  `regressed ~ condition + (1 | task)`.
- **Counts sensitivity:** Wilcoxon signed-rank on paired regression counts.
- Resolution rate per condition is reported next to every regression result.

## Mechanical continuation rule — the insulation

Runs in **batches of 20** (seeded order). After each batch, the orchestrator
computes **only treatment-blind signals**:

- operational health, and
- the **pooled** regression base rate (across all conditions together — blind to
  the between-condition contrast; this is the nuisance parameter we are
  estimating).

**Continue** to the next batch iff **all** hold:
1. cumulative *actual* spend `< $90` (budget soft cap; tracked via the OpenRouter
   key-usage endpoint between batches — provider-side, accurate),
2. **health OK** := in the batch, `(harbor exceptions + INVALID zero-call) / trials
   < 20%` AND per-trial actual cost `< ~2.5×` the smoke baseline AND reviewers are
   not systematically emitting empty reviews,
3. the pool is not exhausted.

Otherwise **ABORT-and-report**: halt and notify the human (do not continue) — also
on hitting the credit/key wall.

**Forbidden during the run:** using the between-condition regression *contrast*
to decide whether or how far to continue. That is optional stopping, and it is
the exact failure mode this project critiques. The pooled base rate is permitted
because it is independent of the contrast under the null; this is what keeps the
internal-pilot design free of optional-stopping bias.

## Budget

Authorized **$100**. Autonomous **soft cap $90** actual. Hard provider backstop:
OpenRouter key limit **$102** (enforced server-side). Actual spend tracked via
the key-usage endpoint at each batch boundary, not the agent's self-estimate
(which under-reports ~1.8×).

## Reproduction

```bash
# one batch (all 3 conditions over its 20 tasks):
bash experiments/001-adversarial-review/run-001/run_batch.sh <N>
# aggregate at PASS_TO_PASS granularity:
uv run ai-benchmark harbor-report jobs --markdown run-001-results.md --json run-001-results.json
# regenerate the pool (datasets is NOT in the repo venv — ephemeral overlay):
cd <scratchpad> && uv run --with datasets python build_pool.py
```

## Amendments

- **2026-07-21 (pre-data, no trials had run).** Fixed a Windows path bug in
  `run_batch.sh`: it built the pool path from Git-Bash `pwd` (`/c/...`), which
  Windows-native Python under `uv run` cannot open, so batch 1 aborted at setup
  before any Harbor trial started (zero spend). Switched to a repo-relative path.
  **No change** to the task pool, conditions, outcomes, analysis, or the
  continuation/stopping rule — runner mechanics only.
- **2026-07-22 (pre-data, batch 1 attempt).** Batch 1 failed on all three
  conditions: Harbor on Windows reads task instruction files via
  `Path.read_text()` with no encoding, defaulting to cp1252, which crashes on
  non-Latin-1 UTF-8 issue text (`UnicodeDecodeError` at task load) and cancels the
  batch; a `rich` spinner glyph adds a secondary cp1252 crash. ~$4.19 was spent on
  partially-run trials before cancellation. **No valid batch was collected — all
  batch-1 job dirs discarded and the batch re-run.** Fix (runner mechanics only):
  export `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` and add `-q`, plus a guard that
  aborts the batch if `control` yields zero completed trials. No change to the
  pool, conditions, outcomes, analysis, or stopping rule.
