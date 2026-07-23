# Experiment 001 - Run 001 results

Pre-registered analysis (`PREREGISTRATION.md`), run once over the final data. Primary outcome: PASS_TO_PASS regression indicator, paired within task. Read every regression result alongside the resolution guardrail -- an arm that regresses less by solving less is not evidence for H1.

## Provenance

- Source glob / label: `jobs/run-001-batch*-*/*`
- Pool fingerprint (sha256): `3b18b128bc1ba8e7d9d3048b7bf28c57090ae31d05d3a03ce1321e0bcc4a83a3`
- Bootstrap seed: `20260721` (10000 iterations, percentile CIs)
- statsmodels available: True
- Generated: 2026-07-23T10:32:24.444696+00:00

## Triplet completeness

- Trial dirs parsed: 240
- Incomplete (no verifier report, excluded): 21
- INVALID zero-call (excluded): 0
- Valid + complete trials in pool: 219
- **Complete triplets (all 3 conditions): 65**
- Tasks dropped (missing >=1 condition): 15

Dropped tasks (per-condition status):

| Task | control | self_review | adversarial |
| --- | --- | --- | --- |
| django__django-11265 | ok | ok | incomplete |
| django__django-11333 | incomplete | incomplete | incomplete |
| django__django-11400 | incomplete | ok | ok |
| django__django-12708 | incomplete | ok | ok |
| django__django-12713 | ok | ok | incomplete |
| django__django-13028 | ok | incomplete | ok |
| django__django-13195 | ok | ok | incomplete |
| django__django-13344 | ok | incomplete | incomplete |
| django__django-13449 | ok | ok | incomplete |
| django__django-13837 | ok | incomplete | ok |
| django__django-15629 | ok | incomplete | incomplete |
| django__django-15732 | incomplete | incomplete | incomplete |
| django__django-15957 | ok | ok | incomplete |
| django__django-16256 | ok | ok | incomplete |
| django__django-16938 | incomplete | ok | ok |

## Per-condition rates (guardrail, co-primary)

Over the complete-triplet tasks. `regression rate` is the primary outcome; `resolution (F2P)` is the pre-registered guardrail (all FAIL_TO_PASS pass); `resolved (strict)` also requires no regression.

| Condition | n | Regression rate | Resolution (F2P) | Resolved (strict) |
| --- | --- | --- | --- | --- |
| control | 65 | 0.246 (16/65) | 0.569 (37/65) | 0.477 (31/65) |
| self_review | 65 | 0.138 (9/65) | 0.585 (38/65) | 0.554 (36/65) |
| adversarial | 65 | 0.200 (13/65) | 0.585 (38/65) | 0.492 (32/65) |

## Primary contrast

### adversarial vs self_review (primary, H1)

Treatment = `adversarial`, baseline = `self_review`, paired over 65 complete-triplet tasks.

| | self_review regressed | self_review clean |
| --- | --- | --- |
| **adversarial regressed** | 5 | 8 |
| **adversarial clean** | 4 | 48 |

- McNemar exact (two-sided binomial on discordant pairs): b (baseline-only) = 4, c (treatment-only) = 8, discordant = 12, **p = 0.3877**.
- Regression rate: adversarial = 0.200, self_review = 0.138.
- Paired difference (adversarial - self_review) = **+0.062** (bootstrap 95% CI [-0.046, +0.169]).
- Wilcoxon signed-rank on paired regression counts: statistic = 42.5, p = 0.5299.

## Secondary contrasts

### adversarial vs control

Treatment = `adversarial`, baseline = `control`, paired over 65 complete-triplet tasks.

| | control regressed | control clean |
| --- | --- | --- |
| **adversarial regressed** | 7 | 6 |
| **adversarial clean** | 9 | 43 |

- McNemar exact (two-sided binomial on discordant pairs): b (baseline-only) = 9, c (treatment-only) = 6, discordant = 15, **p = 0.6072**.
- Regression rate: adversarial = 0.200, control = 0.246.
- Paired difference (adversarial - control) = **-0.046** (bootstrap 95% CI [-0.169, +0.062]).

### self_review vs control

Treatment = `self_review`, baseline = `control`, paired over 65 complete-triplet tasks.

| | control regressed | control clean |
| --- | --- | --- |
| **self_review regressed** | 7 | 2 |
| **self_review clean** | 9 | 47 |

- McNemar exact (two-sided binomial on discordant pairs): b (baseline-only) = 9, c (treatment-only) = 2, discordant = 11, **p = 0.06543**.
- Regression rate: self_review = 0.138, control = 0.246.
- Paired difference (self_review - control) = **-0.108** (bootstrap 95% CI [-0.200, -0.015]).

## Family multiplicity (Holm), for joint claims

| Contrast | McNemar p (raw) | Holm-adjusted |
| --- | --- | --- |
| adversarial_vs_self_review | 0.3877 | 0.7754 |
| adversarial_vs_control | 0.6072 | 0.7754 |
| self_review_vs_control | 0.06543 | 0.1963 |

## Mixed-effects logistic (optional sensitivity)

`regressed ~ C(condition) + (1|task)` via BinomialBayesMixedGLM.fit_vb (variational Bayes).

| Fixed effect | Posterior mean | Posterior sd |
| --- | --- | --- |
| Intercept | -1.7631 | 0.2271 |
| C(condition, Treatment(reference='control'))[T.adversarial] | -0.4430 | 0.3843 |
| C(condition, Treatment(reference='control'))[T.self_review] | -1.1086 | 0.4299 |

