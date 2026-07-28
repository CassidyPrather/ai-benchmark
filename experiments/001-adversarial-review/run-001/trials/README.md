# Run 001 — trimmed reproducibility slice

This directory is the **committable evidence slice** for Experiment 001, Run 001.
It mirrors the Harbor job layout closely enough that
[`../analyze.py`](../analyze.py) re-runs on it **unchanged** and reproduces the
committed [`../run-001-results.json`](../run-001-results.json) exactly. The full,
untrimmed transcripts (~445 MB) are **not** in git — they are attached to the
GitHub Release (`run-001-full-transcripts.tar.gz`).

## Layout

```
trials/run-001-batch<N>-<condition>/<instance_id>__<hash>/
    verifier/report.json                    # VERBATIM — the graded outcome data
    agent/mini-swe-agent.trajectory.json    # TRIMMED (see rule below)
    result.json                             # redacted
    config.json                             # redacted
```

- `<condition>` ∈ `control` / `self_review` / `adversarial`; `<N>` ∈ `1..4`.
- 240 trial directories (4 batches × 20 tasks × 3 conditions).
- 219 trials have `verifier/report.json` (complete); 21 are incomplete
  (agent/verifier exception, no report) and are kept as dirs so the triplet-drop
  accounting matches. Incomplete trials still carry `result.json`/`config.json`
  (and a trajectory where one exists) so the directory survives a git checkout.

## Trimming rule (what analyze.py actually reads)

`analyze.py` only needs, per trial: the condition (from the parent dir name),
the instance id + `tests_status` (from `verifier/report.json`), and trial
*validity* (from the trajectory's `info.model_stats.api_calls` plus whether any
message is an assistant / `response` turn). So each
`agent/mini-swe-agent.trajectory.json` is reduced to:

- **`info` — kept VERBATIM** (holds `model_stats.api_calls`, `config`,
  `exit_status`, `submission`, `mini_version`).
- **`trajectory_format` — kept verbatim** (small metadata string).
- **`messages` — every element reduced to ONLY `role` and (if present)
  `object`.** All `content`, `tool_calls`, `extra`, `function_call`,
  observations, etc. are dropped. This is the minimum `_trajectory_valid` needs.

`verifier/report.json` is kept **verbatim** — it is the outcome data.
`result.json` and `config.json` are passed through a secret redactor (no literal
keys were present; Harbor stores only the `"${OPENROUTER_API_KEY}"` reference,
so 0 redactions were applied). `.txt` logs, `test-stdout.txt`, `trial.log`,
`job.log`, the per-phase `implement`/`revise`/`review`/`reviewer` trajectories,
and Harbor's `trajectory.json` copy are **excluded** as transcript bulk not read
by `analyze.py`. The actual review critiques are exported separately as readable
Markdown under [`../reviews/`](../reviews/).

Slice size: ~9.4 MB, 919 files.

## Reproduce the committed results

From the repo root:

```bash
uv run --with scipy --with numpy --with statsmodels --with pandas \
  python experiments/001-adversarial-review/run-001/analyze.py \
  'experiments/001-adversarial-review/run-001/trials/run-001-batch*-*/*' \
  --out experiments/001-adversarial-review/run-001
```

(Drop `--with statsmodels --with pandas` to skip only the optional mixed-effects
sensitivity model.) The output's `pool_fingerprint`, complete-triplet count
(65), per-condition regression/resolution counts, and every contrast's b/c/p and
rate-difference match `run-001-results.json` bit-for-bit. The only field that
differs run-to-run is the **optional** mixed-effects posterior means/sds, which
drift at the ~1e-6 level from the variational-Bayes optimizer and are
non-deterministic even on the original untrimmed source — they are a sensitivity
add-on, not a pre-registered primary/secondary result.
