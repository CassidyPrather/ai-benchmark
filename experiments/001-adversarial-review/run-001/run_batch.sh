#!/usr/bin/env bash
# Experiment 001 — Run 001 batch runner.
#
# Usage: run_batch.sh <batch_number> [n_concurrent]
#
# Runs all three conditions (control / self_review / adversarial) over one batch
# of 20 tasks, drawn IN SEEDED ORDER from the frozen Django pool
# (task-pool-django.json). Batch N = seeded_rank in ((N-1)*20, N*20].
#
# Reproducible and self-contained: reads the committed pool, sources .env for
# the provider keys, and writes per-(batch,condition) jobs under ./jobs. The
# blind health check and the decision to run the next batch live OUTSIDE this
# script (see PREREGISTRATION.md § Mechanical continuation rule) — this script
# only executes one pre-registered batch.
set -uo pipefail

BATCH="${1:?usage: run_batch.sh <batch_number> [n_concurrent]}"
NCONC="${2:-4}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to repo root $REPO" >&2; exit 1; }

# Provider keys (OPENROUTER_API_KEY for --ae expansion; DAYTONA_API_KEY for the SDK).
if [ ! -f .env ]; then echo "FATAL: no .env at repo root" >&2; exit 1; fi
set -a; . ./.env; set +a

# Windows: force Python UTF-8 mode. Harbor reads task instruction files via
# Path.read_text() with no encoding, so Windows defaults to cp1252 and crashes on
# non-Latin-1 UTF-8 issue text (UnicodeDecodeError at task load). UTF-8 mode also
# stops the rich progress renderer choking on spinner glyphs.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

IDS="$(uv run python -c "
import json
b=int('$BATCH'); lo=(b-1)*20+1; hi=b*20
# Repo-relative path (the script cd's to the repo root first): Windows-native
# Python under 'uv run' cannot open Git-Bash '/c/...' absolute paths.
d=json.load(open('experiments/001-adversarial-review/run-001/task-pool-django.json', encoding='utf-8'))
print(' '.join(t['instance_id'] for t in d['pool'] if lo <= t['seeded_rank'] <= hi))
")"
[ -n "$IDS" ] || { echo "FATAL: no tasks resolved for batch $BATCH" >&2; exit 1; }

IFLAGS=""
for id in $IDS; do IFLAGS="$IFLAGS -i $id"; done

echo "=== Run 001 / batch $BATCH  (n_concurrent=$NCONC) ==="
echo "tasks ($(echo "$IDS" | wc -w)):"; echo "$IDS" | tr ' ' '\n' | sed 's/^/  /'

rc=0
for COND in control self_review adversarial; do
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ)  batch $BATCH / condition=$COND ==="
  # shellcheck disable=SC2086  # $IFLAGS must word-split into separate -i flags
  uv run harbor run -d swebench-verified@1.0 $IFLAGS \
    -a ai_benchmark.live_agents:ExperimentReviewAgent \
    -m openrouter/qwen/qwen3-coder -e daytona -n "$NCONC" -o jobs -q \
    --job-name "run-001-batch${BATCH}-${COND}" \
    --ae OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
    --environment-build-timeout 3.0 \
    --max-retries 1 --retry-include EnvironmentStartTimeoutError \
    --ak condition="$COND" --ak step_limit=100 --ak cost_limit=1.0 --yes
  ec=$?
  nrep=$(find "jobs/run-001-batch${BATCH}-${COND}" -name report.json 2>/dev/null | wc -l | tr -d ' ')
  echo "--- condition=$COND exit=$ec completed=$nrep ---"
  # Self-protect: if the FIRST condition yields zero completed trials, something
  # is systemically broken -- abort before spending on the other two conditions.
  if [ "$COND" = "control" ] && [ "${nrep:-0}" -eq 0 ]; then
    echo "ABORT batch $BATCH: control produced 0 completed trials." >&2
    exit 2
  fi
  [ "$ec" -ne 0 ] && rc="$ec"
done

echo "=== batch $BATCH complete; worst harbor exit=$rc ==="
exit 0
