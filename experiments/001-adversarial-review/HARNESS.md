# Experiment 001 — Harness controls

Three validity-critical controls added to the live-model harness after the cloud
pilot ([`pilot/live/PILOT-LIVE.md`](pilot/live/PILOT-LIVE.md)). They cover items
1–3 of that document's "What the real 4-condition experiment now needs" list.
Items 4–8 remain open; see [Still open](#still-open).

The live agent is [`ExperimentMiniSweAgent`](../../src/ai_benchmark/live_agents.py)
(Harbor agent name `mini-swe-agent-experiment`). It extends
`MiniSweAgentLitellmProxy` — the pilot's litellm-packaging fix (Hazard 1) — so the
experiment always gets both. The unmodified `MiniSweAgentLitellmProxy` is kept
importable for the PILOT-LIVE.md reproduction commands.

## Control 1 — bounded steps and cost

Stock Harbor forces `--cost-limit 0` (unlimited) and leaves mini-swe-agent's
`step_limit` at `0` (also unlimited), so the only bound is wall-clock. With an
unbounded step budget the effective number of steps is a function of provider
latency, which makes arms and models incomparable — and it let the pilot's 15098
agent wander to step ~51 and self-kill.

`ExperimentMiniSweAgent` drops Harbor's forced flag (`CLI_FLAGS = []`) and sets
both bounds from constructor kwargs, defaulting to the pinned constants below. A
`step_limit` (or `cost_limit`) of `0`/unlimited, or a negative/non-numeric value,
is **rejected at construction** — unlimited is exactly the invalid configuration
this control exists to prevent.

The **value** matters less than it being explicit, constant across every arm, and
recorded. The resolved bounds are written into the per-trial mini-swe-agent
config (`agent.step_limit`, `agent.cost_limit`), which mini-swe-agent serializes
into its trajectory's `info.config.agent` — the authoritative per-trial record of
the values actually in force (verified against the pilot trajectories). Passing
them as `--ak step_limit=100 --ak cost_limit=1.0` additionally records them
verbatim in Harbor's `config.json` (`agent.kwargs`); the real run should pass them
there so both records agree. `cost_limit` is only a secondary guard: it is
enforced against mini-swe-agent's own cost estimate, which under-reported real
billing ~8× in the pilot, so provider-side limits remain authoritative (item 4).

## Control 2 — task text out of argv

Harbor passes the whole issue statement as `--task='<text>'`. Any agent command
that runs `pkill -f` / `pgrep -f` on a word appearing in that statement then
matches — and kills — the agent's own process (15098's issue text contained
`runserver`; the agent SIGTERM'd itself at exit 143).

**Invariant:** the constructed agent command line must not contain the task text.
It is delivered instead through mini-swe-agent 2.4.5's native `run.task` config
key: `ExperimentMiniSweAgent.build_run_commands()` writes a per-trial config file
(`/tmp/mswea-experiment/trial.yaml`, JSON — a subset of YAML — so the task string
round-trips exactly) and invokes `mini-swe-agent … -c mini -c <file>` with **no**
`--task`. mini-swe-agent's `mini` CLI reads the task from `run.task` when `--task`
is absent (`run/mini.py:60,94`); the CLI `--task` default is `UNSET`, so omitting
it leaves the file's value intact (`utils/serialize.py`). The write itself
base64-encodes the payload, keeping the plaintext off argv there too (a naïve
`--task "$(cat f)"` would still land the text in the child's argv, so that is not
used). The invariant is asserted directly on the generated command in
[`tests/test_live_agents.py`](../../tests/test_live_agents.py).

## Control 3 — zero-call validity guard

The pilot's litellm bug produced a silent zero-model-call trial that looked like
model incompetence (a `nop` in disguise) — directionally biased measurement error
(Hazard 1). [`harbor-report`](../../src/ai_benchmark/harbor_report.py) now emits a
per-trial **validity verdict** in both the Markdown table (a `Validity` column)
and the JSON output.

A trial is flagged `INVALID: zero-call` when it is a **model arm** that made no
model calls: `api_calls == 0` **or** no assistant turn exists. Both signals are
kept because mini-swe-agent counts one `api_calls` for a query that raised before
any reply (the pilot's failed trial recorded `api_calls: 1`, no assistant turn),
so the assistant-turn signal is the load-bearing one there. The signals come from
the trajectory (`agent/mini-swe-agent.trajectory[.trimmed].json`:
`info.model_stats.api_calls`, and message roles). Arm type is read from the
recorded `agent_info.model_info` — present only when a model was configured — so
the deterministic instruments (`oracle`/`nop`/`saboteur`), which make no calls by
design, are never false-flagged. A model arm with no trajectory at all is treated
as zero-call, since there is no evidence any call was made.

**Wrapper-level guard: none.** Harbor 0.18.0 exposes no clean agent-lifecycle
hook for failing a trial on a post-run condition. Its only post-run entry point,
`populate_context_post_run`, is documented best-effort (the base implementation
swallows every exception) and runs in the agent phase's `finally`, so raising
there would skip verification and surface as an unrelated error type. We do not
monkey-patch Harbor; the report layer is the guard.

## Chosen constants

| Constant | Value | Notes |
| --- | --- | --- |
| `DEFAULT_STEP_LIMIT` | `100` | Max model calls; hold constant across arms. |
| `DEFAULT_COST_LIMIT` | `1.0` USD | Secondary guard (see Control 1). Matches the pilot. |

## Still open

Not addressed here; tracked in PILOT-LIVE.md's needs-list:

- **4** — enforce budget provider-side (OpenRouter key limit); `cost_limit`
  under-reports ~8×.
- **5** — bake the agent into the snapshot (per-trial install overhead).
- **6** — pin images by digest, not `:latest`.
- **7** — repeat the reconstruction-fidelity check on 16315/16429.
- **8** — `swebench-live` still absent from Harbor's registry; mutation-based
  task filtering remains future work.
