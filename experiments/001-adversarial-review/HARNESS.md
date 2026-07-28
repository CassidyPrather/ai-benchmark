# Experiment 001 — Harness controls

Three validity-critical controls added to the live-model harness after the cloud
pilot ([`pilot/live/PILOT-LIVE.md`](pilot/live/PILOT-LIVE.md)). They cover items
1–3 of that document's needs-list for the real (now **3-condition**) experiment.
Items 4–8 remain open; see [Still open](#still-open). The review-loop treatment
those controls exist to serve is documented under
[The review-loop treatment](#the-review-loop-treatment).

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

## The review-loop treatment

The three conditions (`control`, `self_review`, `adversarial`) are the actual
experiment; they differ **only** in *where the review happens* (DESIGN § Conditions).
They are delivered by [`ExperimentReviewAgent`](../../src/ai_benchmark/live_agents.py)
(Harbor agent name `mini-swe-agent-review`, distinct so results tables never
conflate it with the implement-only `mini-swe-agent-experiment`), which extends
`ExperimentMiniSweAgent` and therefore keeps every control above — litellm proxy
extra, bounded/validated step & cost, task off `argv`. Its whole job is to ship an
in-sandbox driver ([`review_driver.py`](../../src/ai_benchmark/review_driver.py))
plus the registered prompts and a JSON config, then run the driver under the
mini-swe-agent tool venv's Python.

### Library driver, not the `mini` CLI

`self_review` requires the *same* conversation to implement, then review, then
revise. The `mini` CLI cannot do this: `DefaultAgent.run()` sets
`self.messages = []` on entry (`agents/default.py:91` in v2.4.5) and there is no
resume/seed-history mechanism (no loader in `utils/serialize.py`, no `--resume`
flag in `run/mini.py`), so a second CLI invocation always starts a fresh
conversation. The driver instead keeps one live `agent.messages` object across
phases and continues it: pop the trailing synthetic `{"role":"exit"}` turn, inject
the next instruction as a `user` message via `add_messages`, and re-run the loop
body from `agents/default.py:96‑122` **without** the reset. All three conditions
run through this one driver (control = implement-only), so control and the review
arms share machinery and differ only by the `condition` knob — a validity
requirement.

**Author = `InteractiveAgent`.** The author is an `InteractiveAgent`
(`mode: yolo`, `confirm_exit: false`), matching the upstream SWE-bench runner
(`run/benchmarks/swebench_single.py` also uses `default_type="interactive"`). This
is load-bearing for continuation: after a submission, `DefaultAgent.execute_actions`
raises before appending the tool result, leaving a dangling assistant `tool_calls`
turn with no matching `role:"tool"` message (the next API call would 400).
`InteractiveAgent.execute_actions` (`agents/interactive.py:124‑139`) instead pads
an observation in a `finally` (`models/utils/actions_toolcall.py:79‑113`), so the
post-submit history is already API-valid once the `exit` turn is popped.
`confirm_exit: false` makes the submit path re-raise cleanly instead of prompting
(a `/dev/null` stdin would otherwise `EOFError`).

### Config base = `swebench.yaml` (the approved swap from the pilot's `-c mini`)

The author config is `recursive_merge(get_config_from_spec("swebench"), overrides)`,
where the spec `"swebench"` resolves to the packaged
`config/benchmarks/swebench.yaml` (`config/__init__.py` `get_config_path`). Overrides
set `model_name`, the bounds, `cwd=/testbed`, and the unattended switches; a
`LocalEnvironment` is built directly (the config's `environment_class: docker` is
ignored — we are already inside the sandbox). The base is **pinned** to the v2.4.5
release commit `e187bcb2ff5825d85761a6f9c1f98c9fa6cfbc79` (file blob
`106decd160e72e5164e29d15d23da354c29c309d`); the exact file is vendored at
[`prompts/swebench.yaml`](prompts/swebench.yaml) as the audit trail. `get_config_from_spec`
returns those bytes **iff the sandbox install is pinned to v2.4.5** — the run must
pin the version for the loaded config to match the vendored SHA. Switching the base
from the pilot's `-c mini` to `swebench` is a deliberate, documented change (it is
the defensible off-the-shelf implementer and makes `info.submission` carry the diff).

### Interpreter — commands must run under `bash`, not `/bin/sh`

`swebench.yaml` sets `interpreter: ["bash", "-c"]` with `BASH_ENV=/root/.bashrc`
precisely so a non-interactive `bash` sources `~/.bashrc` and runs
`conda activate testbed` before each command — otherwise commands hit the base
interpreter, not the testbed env. But mini-swe-agent's `LocalEnvironment` has **no**
`interpreter` field and runs every command via `subprocess.Popen(command,
shell=True)` = `/bin/sh -c` (`dash` on the SWE-bench images), which ignores
`$BASH_ENV`. Passing `interpreter=` to `LocalEnvironment` is a silent no-op (pydantic
drops the unknown field), so the driver instead wraps every command in
`exec bash -c <shlex-quoted>` ([`bash_wrap`](../../src/ai_benchmark/review_driver.py)):
`/bin/sh` re-execs into non-interactive `bash`, which *does* read `$BASH_ENV`,
reproducing the upstream Docker `bash -c` path while staying local. Without this the
author silently runs against the base Python and **every measurement is invalid** —
this was caught by the pre-flight verification pass against the real 2.4.5 package,
not by the fakes-based unit tests (which never touch the real `LocalEnvironment`).
`exec` preserves the pid/pgid so the process-group timeout kill still works, and
stdout is unchanged so the submit sentinel still fires.

### Blind reviewer checkout — non-mutation of `/testbed`

The graded artifact is the `/testbed` working tree the SWE-bench verifier reads
(the task's own `test.sh`/grader from `harbor-datasets`, source `swebench-verified`;
Harbor itself extracts no patch). **INVARIANT: no phase runs `git commit`/`reset`/
`checkout .` in `/testbed`, and reviewer prep never mutates `/testbed`'s working
tree or index.** The driver satisfies this by construction: it copies the whole tree
once (`cp -a /testbed /review`, preserving `.git`) and computes the author's diff
**in the copy** (`git -C /review add -A -N && git -C /review diff HEAD`). Every git
command targets `/review`; `/testbed` appears only as the read-only `cp` source. The
fresh reviewer runs with `cwd=/review` and its **own** `messages` (it can never see
the author transcript), and its `instance_template` is a bare `{{task}}` passthrough
so the registered `critique.txt` is not wrapped in the implement-and-submit-a-patch
framing.

### Budget accumulation

`n_calls`/`cost` are **not** reset across continuation, so a single `step_limit`
would bound implement+review+revise combined. The driver gives each author phase a
full, constant budget: before each continuation it raises `step_limit` to
`n_calls + STEP_LIMIT` (and `cost_limit` to `cost + COST_LIMIT`). So implement,
in-context review (condition 2), and revise each get a full `STEP_LIMIT` calls; the
fresh reviewer (condition 3) is a separate agent with its own budget. Per DESIGN,
equal compute across arms is explicitly **not** a v1 constraint — the review arms
may spend more (that is H2, deferred).

### Review extraction & termination

`critique.txt`/`revise.txt` stay scaffold-agnostic; the mini-swe-agent-specific
submission footer is appended by the driver and is byte-identical across conditions
2 and 3 (shared review-file path). The reviewing agent writes its review to a file
and submits it with the sentinel `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat
<file>` (`environments/local.py:45`), the only clean termination in a scaffold that
requires a tool call every turn and stops only on an `exit` turn
(`agents/default.py:120`). The review is then read from `info.submission`, falling
back to the last assistant message if the step budget was hit first. The author's
**final** (post-revise) trajectory is written to the canonical
`agent/mini-swe-agent.trajectory.json` that `harbor-report` and the zero-call guard
read (the loop saves after every step, so the canonical file always holds the
latest author state); per-phase snapshots (`implement`/`review`/`revise`/`reviewer`)
are written beside it as sibling evidence.

## Still open

Not addressed here; tracked in PILOT-LIVE.md's needs-list:

- **4** — enforce budget provider-side (OpenRouter key limit); `cost_limit`
  under-reports ~8×.
- **5** — bake the agent into the snapshot (per-trial install overhead).
- **6** — pin images by digest, not `:latest`.
- **7** — repeat the reconstruction-fidelity check on 16315/16429.
- **8** — `swebench-live` still absent from Harbor's registry; mutation-based
  task filtering remains future work.
