# Experiment 001 — Adversarial LLM Code Review

**Status: design settled; instrument validated on the cloud path (see [`pilot/live/PILOT-LIVE.md`](pilot/live/PILOT-LIVE.md)); treatment (the review-loop wrapper) in build.**

Companion to the blog post *"Prove It"* (a critique of the epistemics of
[bun.com/blog/bun-in-rust](https://bun.com/blog/bun-in-rust)'s adversarial-review
claims). The blog post lives outside this repo; this directory is the experiment.

## Research question

Does blinded adversarial LLM review measurably reduce regressions versus
alternatives at equal budget? The composite claim is untested in the
literature; components have partial support (see Prior literature).

Original hypotheses:

1. **H1** — Blinded adversarial review produces fewer regressions than
   non-blinded review.
2. **H2** — Blinded adversarial review is more cost-effective than equal spend
   on an anti-regression suite.
3. **H3** — Blinded-reviewed code is cheaper to extend for future requirements.

**Scope: v1 tests H1 only.** H3 (maintainability via "cost of next issue") is
deferred: it doubles the run count, adds a second noisy outcome, and drags in
LLM-as-judge methodology. No cheap proxies for maintainability (diff size,
complexity deltas) — that's hunch-laundering. H2 is likewise deferred (needs a
cost-matched anti-regression-suite arm).

## Design

### Conditions (the only manipulated variable)

**v1 holds the model fixed** — one author/reviewer model across all arms. The
manipulated variable is *where the review happens*, not *who reviews*:

1. **No review** (control)
2. **Self-review, same context** — the author model reviews its own patch in
   the same conversation (non-blinded)
3. **Adversarial review, fresh context, same model** — a fresh context window
   of the same model reviews the patch, blind to the author's transcript

Conditions 2 vs. 3 are the contribution, and they *are* H1: does splitting the
context — a fresh window versus the author's own conversation — reduce
regressions at all? Perplexity-based self-preference (Wataoka et al. 2024)
predicts a fresh context of the *same* model may not fully escape
self-preference bias, so whether condition 3 beats condition 2 is an open
empirical question rather than the assumption the bun post treats it as.

**Deferred to a future experiment: the different-model arm.** The original
design carried a fourth condition — adversarial review by a *different* model.
Whether different weights beat a same-model fresh context (i.e. whether the
benefit exceeds the sum of the two models' parts) is a separate question that
drags in cross-model confounds — capability gaps, cost asymmetry, which model
reviews which — and roughly restores the run count this cut removes. It is spun
out rather than answered halfway. Seed captured in
[`../003-cross-model-review/NOTES.md`](../003-cross-model-review/NOTES.md).

### Outcome

- **Primary:** regression count via hidden test suite at PASS_TO_PASS
  granularity (SWE-bench style).
- **Secondary:** task success via FAIL_TO_PASS.

The suite need not be complete — it must be **identical across arms**. An
incomplete suite underestimates the absolute regression rate, but the relative
comparison stays valid (measurement error uncorrelated with treatment).
Completeness is a *power* problem, not a *validity* problem: a weak suite
means both arms measure ≈0 and there is no signal. Mitigation: filter tasks by
suite strength via mutation testing (mutmut / cosmic-ray).

### Review protocol (operationalized before any runs — forking-paths guard)

Settled for v1, chosen to minimize compounding variables; held constant across
all review arms:

- **Reviewer input:** task statement + unified diff + read-only checkout of
  the patched repo. Never the author transcript.
- **Rounds:** exactly one (review → single author revision → final patch).
- **Reviewer prompt:** purely adversarial framing — the only job is to find
  bugs and reasons the change does not work; no style feedback solicited.
- **Author obligation:** receives the review verbatim; must address or
  explicitly reject each finding; produces the final patch. The same revision
  prompt is used in all review arms.
- Condition 2 uses the identical review + revision prompts, delivered inside
  the author's own context window.

Known confound, accepted for v1: review arms consume more compute/turns than
control. H1 is a claim about regression counts, not equal-cost outcomes; the
equal-budget question is H2 and is deferred.

### Task source & contamination

A memorized golden patch compresses all conditions toward identical output and
kills effect size (a power problem, not a bias problem). Use post-cutoff task
sources: **SWE-bench-Live** ([arXiv:2505.23419](https://arxiv.org/abs/2505.23419),
monthly updates, RepoLaunch automated env setup) or **SWE-rebench** (Nebius —
steal its fixed-ReAct-scaffold methodology). Pin the task set to a specific
monthly snapshot and record it here. Live sources trade contamination
resistance for week-to-week variance; the paired within-task design mostly
neutralizes this.

**v1 decision (2026-07-21): run on `swebench-verified@1.0` first.** The "simple"
experiment ships on SWE-bench Verified despite its contamination risk (the model
has likely seen these gold patches). Rationale: it works end to end today (the
pilot proved the cloud path on it), whereas SWE-bench-Live is still absent from
Harbor's registry (PILOT-LIVE.md needs-list item 8). Contamination attacks
*effect size*, not validity, and the paired within-task design is unaffected — if
review's effect is washed out we will know to move to a post-cutoff source.
Characterizing contamination itself — and whether it has a genuinely unique
confounding interaction with review — is deferred, not assumed away.
SWE-bench-Live / SWE-rebench remain the target for a contamination-resistant
follow-up.

Task selection: prefer tasks with larger gold patches (one-line fixes rarely
regress — wasted samples); filter by test-suite strength via mutation testing.

### Statistics

Paired design: every task runs under every condition, multiple seeds per cell;
bootstrap the paired difference. Budget sketch: 40 tasks × 3 conditions × 3
seeds = 360 runs. Mid-tier model via OpenRouter — cheaper AND a higher base
error rate means more regressions available for review to catch, i.e. more
power.

## Stack

**Harbor** ([harborframework.com](https://harborframework.com), from the
Terminal-Bench team) is the switchboard:

- runs arbitrary agents (Claude Code, OpenHands, Codex CLI, …) as black boxes
- `harbor run --dataset X --agent Y --model Z --n-concurrent N --env <provider>`
- local Docker or cloud via a flag swap (Daytona, Modal, LangSmith, Blaxel,
  Novita, E2B, Runloop, Tensorlake)
- verifier isolation built in: the test script runs in a sandbox and writes a
  reward; the agent never touches the verifier
- the treatment lives in a custom agent wrapper via `--agent-import-path` —
  three conditions = one wrapper class + a config knob orchestrating the
  implementer → reviewer → revise loop; everything else held constant

Provider: local Docker for pilots; Modal (recurring free credits) or Daytona
(Terminal-Bench team's own choice) for full runs.

Rejected alternatives: Inspect AI (wants the agent inside its solver
abstractions; we want real harnesses as black boxes), SWE-ReX / raw SWE-bench
harness (lower level; would re-write Harbor's glue).

## Reproducibility contract

Pin the Harbor version and dataset snapshot; vendor exact prompts as files;
commit per-run job configs, seeds, and raw reward outputs (trimmed of bulk
logs). Target: clone → set API key → `harbor run` → reproduce Table 1.

## Pilot (do FIRST, before building anything)

Five tasks, end to end. Verify:

1. SWE-bench-Live-style tasks run cleanly under Harbor with the chosen agent.
2. Regression results are extractable at PASS_TO_PASS granularity — not
   flattened to a single resolved/unresolved bit.

Item 2 is the whole experiment. If Harbor's SWE-bench adapter flattens it, a
small custom verifier is needed — find that out in week one, not week six.

## Prior literature

Self-bias (supports the "writer wants merge" premise):

- Panickssery, Bowman & Feng, *LLM Evaluators Recognize and Favor Their Own
  Generations* (NeurIPS 2024). <https://arxiv.org/abs/2404.13076> —
  self-preference bias is real and causally linked to self-recognition.
- Wataoka et al. 2024 follow-up: self-preference is largely a perplexity
  effect — judges favor low-perplexity text regardless of authorship. Key
  implication: a fresh context window of the SAME model may not escape bias.
- Huang et al., *Large Language Models Cannot Self-Correct Reasoning Yet*
  (ICLR 2024). <https://arxiv.org/abs/2310.01798> — intrinsic self-correction
  fails, sometimes degrades performance.

Critic/reviewer effectiveness:

- McAleese et al. (OpenAI), *LLM Critics Help Catch LLM Bugs*.
  <https://arxiv.org/abs/2407.00215> — CriticGPT critiques preferred over
  human 63%; catches more inserted bugs than paid human reviewers. Caveat:
  hallucinated bugs; human+model teams hallucinate less. Methodology note:
  inserted bugs with known ground truth ≈ the design needed for H1.
- Du et al., *Improving Factuality and Reasoning through Multiagent Debate*
  (ICML 2024). <https://arxiv.org/abs/2305.14325> — canonical split-instances
  result. Skeptical follow-up: *Should We Be Going MAD?*
  <https://arxiv.org/abs/2311.17371> (gains inconsistent).

Field studies:

- Cihan et al., *Automated Code Review In Practice*.
  <https://arxiv.org/abs/2412.18531> — Beko, 4,335 PRs: 73.8% of automated
  comments resolved; PR closure time rose 5h52m → 8h20m. Best existing data
  for H2 (benefit exists, not free).
- WirelessCar workflow study: <https://arxiv.org/abs/2505.16339>
- Early multi-agent review: <https://arxiv.org/abs/2404.18496>

Perception vs. measurement:

- METR RCT, July 2025: devs 19% slower with AI while believing they were 20%
  faster.
  <https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/>
  (A Feb 2026 follow-up exists — cite both to avoid cherry-picking
  accusations.)
