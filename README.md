# ai-benchmark

Cassidy's AI research experiments. One repo, many experiments: shared Python tooling lives in `src/ai_benchmark/`, and each experiment keeps its design, prompts, configs, and (trimmed) run artifacts under `experiments/NNN-slug/`.

The point of this repo is showing work: every experiment should be reproducible from a fresh clone — pinned harness versions, pinned dataset snapshots, vendored prompts, committed seeds and raw outcome data. If a result can't be regenerated from what's committed here plus an API key, it doesn't get claimed.

## Experiments

| # | Experiment | Status |
|---|-----------|--------|
| [001](experiments/001-adversarial-review/DESIGN.md) | Adversarial LLM code review — does blinded adversarial review reduce regressions? | Pilot |
| [002](experiments/002-context-injection/NOTES.md) | Context-injection strategies beyond RAG (background curation, sequential single-tasking) | Open question |

## Development

Requires [uv](https://docs.astral.sh/uv/)

Setup: `uv sync --dev`

Lint: `uv run ruff check`

Format: `uv run ruff format`

Type-check: `uv run ty check`

Test: `uv run pytest`

Security audit: `uv audit --preview-features audit`
