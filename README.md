# AI Benchmark

Hosting my experiments on version control so other people can replicate them later if needed.

Generally you have to control *so many things*, I'll keep trying to pin the knobs in place with superglue and hopefully that'll compound later.
Or not. But hopefully.

## Experiments

| #                                                   | Experiment                                                            | Status |
| --------------------------------------------------- | --------------------------------------------------------------------- | ------ |
| [001](experiments/001-adversarial-review/DESIGN.md) | Is blinded adversarial review the optimal way to prevent regressions? | TBD    |
| [002](experiments/002-context-injection/NOTES.md)   | Do context curating parallel agents actually help?                    | TBD    |

## Development

Requires [uv](https://docs.astral.sh/uv/)

Setup: `uv sync --dev`

Lint: `uv run ruff check`

Format: `uv run ruff format`

Type-check: `uv run ty check`

Test: `uv run pytest`

Security audit: `uv audit --preview-features audit`
