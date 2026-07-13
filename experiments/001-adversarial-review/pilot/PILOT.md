# Experiment 001 — Pilot report

Instrument validation for the adversarial-review experiment. Design and research
questions: [`../DESIGN.md`](../DESIGN.md) ("Pilot" section).

## Goals

The DESIGN's pilot asks two questions ("do FIRST, before building anything"):

- **Q1** — do SWE-bench(-style) tasks run cleanly under Harbor with the chosen
  agent?
- **Q2** — are regression results extractable at **PASS_TO_PASS granularity**,
  not flattened to a single resolved/unresolved bit? *"Item 2 is the whole
  experiment."*

This was run as a **no-model instrument validation**: no LLM API calls in any
condition. Three deterministic conditions stand in for the eventual review arms
and exercise the full Harbor → agent → verifier → report pipeline:

- **oracle** — Harbor's built-in agent; applies the task's gold patch. Expect
  `resolved=1`, all PASS_TO_PASS pass.
- **nop** — Harbor's built-in agent; changes nothing. Expect `resolved=0`,
  FAIL_TO_PASS fail, PASS_TO_PASS all pass.
- **saboteur** — a custom agent (this repo:
  [`ai_benchmark.pilot_agents.SaboteurAgent`](../../../src/ai_benchmark/pilot_agents.py))
  that applies the gold patch **plus one deterministic, documented source
  regression**. Expect `resolved=0` with **PASS_TO_PASS failure count > 0** — the
  "looks resolved but silently regresses" case that adversarial review must
  catch.

## Environment constraints encountered (blockers for a live run)

This sandbox routes egress through a policy proxy. The following are **hard
blockers** that a live run must resolve:

1. **No SWE-bench image is pullable.** Container-registry *manifests* resolve,
   but *blobs* are served from CDNs the proxy denies with `403 CONNECT`:
   - Docker Hub (`swebench/sweb.eval.x86_64.*`) → `production.cloudfront.docker.com` — denied.
   - ghcr Epoch mirror (`ghcr.io/epoch-research/swe-bench.eval.x86_64.*`) → `pkg-containers.githubusercontent.com` — denied.
   - AWS ECR Public, quay.io, registry.k8s.io, huggingface.co — also denied at the blob/CONNECT layer.

   `mcr.microsoft.com` **self-serves blobs** and is reachable (and mirrors the
   Docker Hub `python` images), so the pilot **reconstructs an equivalent
   testbed** on a `python:3.9` base instead of pulling the real image. Same
   public source repo (`django@<base_commit>`), same verifier, same grader — see
   [`reconstruction/`](reconstruction/). For a live run the fix is to
   **allowlist the image blob CDNs** (or host a self-serving mirror of the
   SWE-bench images).

2. **Other denied hosts:** `openrouter.ai` (the intended model provider — must be
   allowlisted for any model arm), `api.osv.dev`, and the container-blob CDNs
   above. `github.com` (git) and `pypi.org`/`files.pythonhosted.org` are
   reachable; container egress works through a **transparent** proxy once the
   proxy CA is trusted inside the image.

3. **No LLM API key** was available, hence the no-model design above.

## Reproduction

Prerequisites: Docker daemon; `uv`; this repo. Harbor and the custom agents are
pinned in `pyproject.toml` (`experiments` group, installed by `uv sync`).

```bash
uv sync                                        # installs harbor==0.18.0 + ai_benchmark
```

**1. Obtain a runnable task environment.** In an unrestricted environment you
would let Harbor pull the real base image. Here, build the reconstruction
(see [`reconstruction/README.md`](reconstruction/README.md)) and tag it
`local/sweb-<inst>:latest`, then point the harbor task's
`environment/Dockerfile` at it (`FROM local/sweb-<inst>:latest`).

**2. Run the three conditions** (jobs land in `./jobs`, which is git-ignored):

```bash
TASK=<path to the local task dir>            # task.toml + tests/ + solution/ + environment/
uv run harbor run -p "$TASK" -a oracle -e docker -n 1 -o jobs --yes
uv run harbor run -p "$TASK" -a nop    -e docker -n 1 -o jobs --yes
uv run harbor run -p "$TASK" -a ai_benchmark.pilot_agents:SaboteurAgent \
    --ak task_dir="$TASK" -e docker -n 1 -o jobs --yes
```

Notes:
- The dataset registry is resolved from a local `registry.json` via
  `--registry-path` (the default registry backend host is not reachable here);
  task defs are fetched from `github.com/laude-institute/harbor-datasets`. If the
  session injects `GIT_CONFIG_*` env vars that scope git to a private repo, run
  harbor with them removed (`env -u GIT_CONFIG_KEY_0 -u GIT_CONFIG_KEY_1 -u
  GIT_CONFIG_KEY_2 -u GIT_CONFIG_COUNT uv run harbor ...`).
- Harbor only auto-injects `task_dir` for its built-in `oracle`; a custom
  import-path agent must be given it with `--ak task_dir=...`.

**3. Summarise at PASS_TO_PASS granularity:**

```bash
uv run ai-benchmark harbor-report jobs --json results.json --markdown results.md
```

## Results (conditions × tasks)

Three SWE-bench Verified django instances (chosen for shared image layers,
Python-3.9 spec reuse, and dep-light test modules — `i18n`, `utils_tests`,
`bulk_create`), each under all three conditions. Full table:
[`results.md`](results.md) / [`results.json`](results.json); per-trial raw
grading under [`trials/`](trials/).

| Task | Condition | Resolved | Reward | F2P pass/fail | P2P pass/fail | P2P regressions |
| --- | --- | --- | --- | --- | --- | --- |
| django__django-15098 | nop | no | 0 | 0/2 | 88/0 | – |
| django__django-15098 | oracle | yes | 1 | 2/0 | 88/0 | – |
| django__django-15098 | saboteur | no | 0 | 2/0 | **87/1** | `test_to_language (i18n.tests.TranslationTests)` |
| django__django-16315 | nop | no | 0 | 0/1 | 42/0 | – |
| django__django-16315 | oracle | yes | 1 | 1/0 | 42/0 | – |
| django__django-16315 | saboteur | no | 0 | 1/0 | **39/3** | 3 `bulk_create.tests.BulkCreateTests` PK-write-back tests |
| django__django-16429 | nop | no | 0 | 1/3 | 21/0 | – |
| django__django-16429 | oracle | yes | 1 | 4/0 | 21/0 | – |
| django__django-16429 | saboteur | no | 0 | 3/1 | **8/13** | 13 `utils_tests.test_timesince` tests |

## Verdicts

**Q1 — tasks run cleanly under Harbor: YES.** All 9 trials completed with 0
Harbor exceptions. Environment build (`FROM local/sweb-<inst>`), agent phase
(oracle uploads `solution/` and runs `solve.sh`; the custom saboteur applies
patches via `environment.exec`), verifier isolation (the container-side
`tests/test.sh` runs the repo test subset and `swebench==4.0.3` grading), reward
emission, and host-side log download all worked. `oracle` resolves every task;
`nop` fails FAIL_TO_PASS with PASS_TO_PASS intact — exactly as designed.

**Q2 — PASS_TO_PASS granularity is extractable: YES.** The task's own verifier
writes `/logs/verifier/report.json` with `tests_status.{FAIL_TO_PASS,
PASS_TO_PASS}.{success,failure}` **lists of individual test names** — it is *not*
flattened to a resolved bit. Harbor downloads the whole `verifier/` dir to the
host per trial. Evidence (committed):
`trials/<task>/<condition>/report.json` (e.g.
[`trials/django__django-15098/saboteur/report.json`](trials/django__django-15098/saboteur/report.json)).
`src/ai_benchmark/harbor_report.py` parses these into per-trial records carrying
the failing test **names**, not just counts.

Harbor's SWE-bench adapter therefore does **not** need a custom verifier for the
regression outcome — the primary risk the DESIGN flagged for "week one" is
cleared.

**Instrument validation — regressions surface as named PASS_TO_PASS failures:
YES.** In every `saboteur` trial the injected regression appears as one or more
**named** PASS_TO_PASS failures, distinct from the FAIL_TO_PASS signal:

- **15098** (partial, 1 test): gold patch applied so FAIL_TO_PASS still passes
  (2/0) — the task *looks* resolved — yet `test_to_language` regresses. A single
  resolved bit would hide this; per-test granularity names it.
- **16315** (partial, 3 tests): FAIL_TO_PASS still passes (1/0); 3 bulk_create
  PK-write-back tests regress.
- **16429** (broad, 13 tests): 13 timesince tests regress (this one also trips 1
  FAIL_TO_PASS).

The exact sabotage edit per task is documented and version-controlled in the
`REGRESSIONS` table in
[`src/ai_benchmark/pilot_agents.py`](../../../src/ai_benchmark/pilot_agents.py):

| Task | File | Edit | Effect |
| --- | --- | --- | --- |
| 15098 | `django/utils/translation/__init__.py` | `to_language` language subtag `.lower()` → `.upper()` | `to_language('en_US')` → `'EN-us'`; regresses `test_to_language` |
| 16315 | `django/db/models/query.py` | drop `setattr(obj_without_pk, field.attname, result)` in `bulk_create` | generated PKs not written back; 3 tests regress |
| 16429 | `django/utils/timesince.py` | zero-duration fallback `time_strings["minute"]` → `["hour"]` | "0 minutes" → "0 hours"; 13 tests regress |

## What a live-model pilot needs

- **Allowlist `openrouter.ai`** (the DESIGN's model provider) and set an API
  key. The review-arm agents (author/reviewer wrappers) then slot in as
  additional subclasses of `_TestbedPatchAgent`'s sibling in `pilot_agents.py`.
- **Allowlist the image blob CDNs** (`production.cloudfront.docker.com` and/or
  `pkg-containers.githubusercontent.com`) or host a self-serving mirror, so the
  real SWE-bench images pull and per-task reconstruction is unnecessary.
- **`swebench-live` is not in Harbor's registry.** The DESIGN prefers a
  post-cutoff task source (SWE-bench-Live / SWE-rebench) for contamination
  resistance. Harbor ships `swebench-verified@1.0` (used here) but no
  swebench-live dataset; wiring a swebench-live adapter (or a Harbor dataset
  built from its monthly snapshots) is **future work**.
- Mutation-testing-based task filtering (DESIGN "suite strength") is likewise
  future work.

## Pinned versions & digests

| Component | Value |
| --- | --- |
| Harbor | `0.18.0` (pinned in `pyproject.toml`, `experiments` group) |
| Grader | `swebench==4.0.3` (+ `datasets==2.16.1`, `fastcore<1.11`), run via `uv` in-container |
| Dataset | `swebench-verified@1.0` (500 tasks) |
| harbor-datasets commit | `86723674f04e4209ac479d0fb75d9d9f44b4377e` |
| Tasks | `django__django-15098` (v4.1), `django__django-16315` (v4.2), `django__django-16429` (v4.2) |
| Reconstruction base | `mcr.microsoft.com/mirror/docker/library/python:3.9-bookworm` @ `sha256:ff12c273af1e1814efcd8dfdefe16a70a4d901afe94dce721ffed8a34176f285` |
| Grading interpreter | `python:3.11-bookworm` @ `sha256:e39286476f84ffedf7c3564b0b74e32c9e1193ec9ca32ee8a11f8c09dbf6aafe` |

Reference (would-be) base images for an unrestricted run — ghcr Epoch mirror
`latest` manifest digests (reachable manifest, **denied blobs** here):

| Task | `ghcr.io/epoch-research/swe-bench.eval.x86_64.<inst>` digest |
| --- | --- |
| django__django-15098 | `sha256:db9f2a872180b3aef9cc811565ed4ddbcc4156b12d989f2776e7b79cf2b0ad89` |
| django__django-16429 | `sha256:46414756e1a999d929fd972248bc31b3148dad6b0784a3acc1b470aeb1c0e3d2` |
| django__django-16315 | `sha256:d91d6f14b6ea99d2f0fb33245c92a352ab558586e90b17e447e2f054c7de3797` |
