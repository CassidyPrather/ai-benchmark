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

IDS="$(uv run python -c "
import json
b=int('$BATCH'); lo=(b-1)*20+1; hi=b*20
d=json.load(open(r'$HERE/task-pool-django.json', encoding='utf-8'))
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
    -m openrouter/qwen/qwen3-coder -e daytona -n "$NCONC" -o jobs \
    --job-name "run-001-batch${BATCH}-${COND}" \
    --ae OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
    --ak condition="$COND" --ak step_limit=100 --ak cost_limit=1.0 --yes
  ec=$?
  echo "--- condition=$COND exit=$ec ---"
  [ "$ec" -ne 0 ] && rc="$ec"
done

echo "=== batch $BATCH complete; worst harbor exit=$rc ==="
exit 0
