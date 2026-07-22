# Experiment 003 — Cross-model adversarial review (seed)

**Status: not started. Spun out of Experiment 001's scope on 2026-07-20.**

Experiment 001 v1 holds the model fixed and manipulates only *where* review
happens (no review / self-review same-context / adversarial fresh-context, all
same model). It deliberately drops the original fourth condition — adversarial
review by a **different** model — to this separate experiment rather than
answering it halfway.

## The question

Does review by a *different* model reduce regressions **beyond** what a
same-model fresh context achieves — i.e. beyond the sum of the two models'
individual capabilities?

## Why it's separate, not a fourth arm in 001

- **Cross-model confounds** that don't exist in the fixed-model design: the
  capability gap between author and reviewer, cost asymmetry, and the choice of
  which model reviews which are all new variables.
- It roughly restores the run count that fixing the model removes from 001.
- The clean version needs a **capability-matched control** (a same-strength
  different model) to separate "different weights" from "just a stronger
  reviewer" — its own design problem.

## Prior signal

Perplexity-based self-preference (Wataoka et al. 2024) predicts a same-model
fresh context may not escape self-preference bias. If Experiment 001 finds
condition 3 barely beats condition 2, that is the motivation to test whether
*different weights* are what's actually required. See
[`../001-adversarial-review/DESIGN.md`](../001-adversarial-review/DESIGN.md).
