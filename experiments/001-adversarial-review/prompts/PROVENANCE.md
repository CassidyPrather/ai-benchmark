# Experiment 001 — Prompt provenance

Every prompt used in this experiment, its source, and exactly how much of it is
original to us. This file exists for two reasons the project cares about:

1. **Pre-registration integrity.** The review protocol was operationalized
   *before* any runs ([`../DESIGN.md`](../DESIGN.md) § Review protocol). These
   prompts are the operative form of that protocol; they are fixed here before
   the experiment so results cannot be reverse-engineered from prompt tuning.
2. **Reproducibility contract.** "Vendor exact prompts as files" (DESIGN §
   Reproducibility contract). The operative strings live beside this file as
   plain text; this document is the audit trail.

The honest summary is a table with one uncomfortable row:

| Role | Prompt file | Source | Exact text published? | License | Invention required |
| --- | --- | --- | --- | --- | --- |
| Implementer | [`swebench.yaml`](swebench.yaml) (vendored at pinned SHA) | mini-swe-agent `swebench.yaml` | **Yes, verbatim** | MIT | **None** |
| Reviewer | [`critique.txt`](critique.txt) | *No published prompt fits our setting* | **No** (CriticGPT withheld) | authored by us; cited composition | **Unavoidable** |
| Reviser | [`revise.txt`](revise.txt) | Self-Refine REFINE step | Yes (off-task domain) | Apache-2.0 | Minimal (one clause) |

We state the asymmetry openly because it is exactly the kind of thing the blog
post accuses others of hiding: **the adversarial-reviewer role has no
off-the-shelf prompt, and the most-cited prior art is precisely the case where
the prompt was not released.** Anyone is free to swap our critique instruction
and re-run; that is why the manipulated variable is isolated and the rest is
held constant.

## Implementer — off-the-shelf, verbatim

The author agent uses mini-swe-agent's packaged SWE-bench config
`src/minisweagent/config/benchmarks/swebench.yaml` **unmodified** — the same
config behind the project's reported SWE-bench Verified numbers. It already
emits a unified diff via the standard submission protocol, so nothing about the
task framing is ours.

- Repo: <https://github.com/SWE-agent/mini-swe-agent> — License: **MIT**.
- File: `src/minisweagent/config/benchmarks/swebench.yaml`, pinned to the
  **v2.4.5** release commit `e187bcb2ff5825d85761a6f9c1f98c9fa6cfbc79` (a floating
  `main` reference is non-reproducible because the v1→v2 templates changed). The
  exact file content is git blob `106decd160e72e5164e29d15d23da354c29c309d`.
- We override **numeric / structural config only** — `model_name`, `step_limit`,
  `cost_limit`, `cwd`, and the unattended-run switches (`mode: yolo`,
  `confirm_exit: false`) that the upstream SWE-bench runner sets the same way
  (`run/benchmarks/swebench_single.py`) — plus building a `LocalEnvironment`
  instead of the config's `environment_class: docker` (we are already inside the
  sandbox). These are experiment parameters documented in
  [`../HARNESS.md`](../HARNESS.md), not prompt edits. The `system_template` and
  `instance_template` (the actual prompt) are used byte-for-byte.

Because it is used verbatim and pinned, the implementer prompt is referenced
rather than copied; the exact `swebench.yaml` at that SHA is vendored beside this
file as [`swebench.yaml`](swebench.yaml) (blob `106decd1…`), so this directory is
self-contained. At run time the driver loads the base via
`get_config_from_spec("swebench")` from the sandbox-installed `mini-swe-agent`,
which equals the vendored bytes **when the install is pinned to v2.4.5** (the
version pin is what makes the loaded config reproducible; the vendored copy is the
audit trail proving what those bytes are).

> **Open reconciliation (for the driver build):** the cloud pilot ran the author
> on the `-c mini` base config, whose submission step is empty (the graded
> artifact is the `/testbed` working tree, not the submission). `swebench.yaml`
> is the more defensible off-the-shelf *implementer* and additionally makes
> `info.submission` carry the diff. Switching the base config from `mini` to
> `swebench` is a deliberate, documented change from the pilot, not silent drift.

## Reviewer — `critique.txt` (authored; cited composition)

No published prompt matches our setting: an agent that adversarially reviews a
unified diff while exploring a read-only checkout, one round, correctness only.
The candidates and why none is directly reusable:

- **CriticGPT** — McAleese et al., *LLM Critics Help Catch LLM Bugs*, arXiv
  [2407.00215](https://arxiv.org/abs/2407.00215) (OpenAI, 2024). The critic is
  **RLHF-trained**, not prompted; §7.3 states the prompt was hand-tweaked for
  formatting and **not released**. §7.4 publishes only the human-evaluation
  *dimensions* — comprehensiveness, real-bug inclusion, no hallucinated bugs, no
  nitpicks. We lift that **rubric** (what a good critique optimizes for), not any
  text, because no text exists.
- **Reflexion** — Shinn et al., arXiv [2303.11366](https://arxiv.org/abs/2303.11366);
  repo <https://github.com/noahshinn/reflexion> (**MIT**). Its released
  self-critique string — *"explain why your implementation is wrong…"* — is
  test-failure-driven and single-function, not diff-level review. We reuse its
  **framing** ("explain why this is wrong"), not its trigger.
- **Self-Refine** — Madaan et al., arXiv [2303.17651](https://arxiv.org/abs/2303.17651);
  repo <https://github.com/madaan/self-refine> (**Apache-2.0**). Its released
  code-feedback prompts target *readability/speed*, not correctness. We reuse the
  **feedback-step role** in the loop, not its text.

`critique.txt` is therefore an **original composition** whose every design
choice traces to (a) CriticGPT's rubric §7.4, (b) Reflexion's "why is it wrong"
framing, (c) Self-Refine's feedback role, and (d) DESIGN's adversarial,
one-round, correctness-only protocol. The scaffold around it — how the reviewer
reads files and runs bash — is mini-swe-agent's MIT scaffold used verbatim; only
this instruction is ours.

The **same** `critique.txt` is used in both review conditions, to keep them
comparable:
- **Condition 3 (adversarial, fresh context):** it is the fresh reviewer agent's
  task instruction, with the task statement and unified diff filled in and a
  read-only `/review` checkout to explore. The reviewer never sees the author's
  transcript.
- **Condition 2 (self-review, same context):** the identical instruction is
  injected into the author's own conversation as the next turn, so the author
  reviews its own patch in-context (non-blinded).

## Reviser — `revise.txt` (Self-Refine + one clause)

The revise step adopts Self-Refine's canonical REFINE structure — *"here is the
artifact, here is feedback, now produce the improved artifact"* (`PROMPT_FIX`,
Apache-2.0, arXiv 2303.17651). The one thing the literature does **not** provide
is an *accept-or-reject* obligation; Self-Refine assumes feedback is accepted. We
add a single clause requiring the author to address each point **or explicitly
reject it with a reason**, which is DESIGN's stated author obligation. Reflexion's
reviser instruction ("given your prior implementation and feedback, write your
full implementation") is the secondary citation for the "prior output + feedback
→ final artifact" shape.

`revise.txt` is **identical across conditions 2 and 3** — this is required for
validity. The only thing that differs between those conditions is the *source* of
the review it acts on (the author's own in-context self-review vs. a fresh
same-model reviewer). Same revise prompt, different review origin.

## Delivery mechanics (finalized in the driver, noted here for the record)

- **Condition 1 (control):** implement only. No critique, no revise.
- **Condition 2:** implement → inject `critique.txt` (author self-reviews
  in-context) → inject `revise.txt` → author produces final patch, all in one
  live `agent.messages`.
- **Condition 3:** implement → a fresh same-model reviewer runs `critique.txt`
  against a read-only `/review` worktree → its review is delivered verbatim into
  the author's context → inject `revise.txt` → author produces final patch.

Conditions 2 and 3 use the **same injection path** and the **same** `revise.txt`;
only the review's origin differs. This parity is the whole point of the design
and is what CLI orchestration could not have provided.

### Scaffold-agnostic prompts + a driver-owned submission footer

`critique.txt` and `revise.txt` are kept **scaffold-agnostic** — they name no
mini-swe-agent mechanics — because they are the registered, pre-registered
prompts. The one mini-swe-agent-specific bit, *how a review is submitted and how
the reviewing agent terminates*, lives in the **driver** as connective, not in the
prompt: the driver appends a fixed submission footer to the filled `critique.txt`.
The footer is **byte-identical** for conditions 2 and 3 (both use a single shared
review-file path, since the author-as-reviewer and the fresh reviewer never run
concurrently), so the parity above is preserved.

The footer instructs the reviewing agent to write its review to a file and submit
it with mini-swe-agent's sentinel command
(`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat <file>`). This is the only
clean way to *terminate* a mini-swe-agent review turn: the scaffold requires every
assistant turn to carry a bash tool call and the run loop only stops on an `exit`
turn (`agents/default.py:120`), so a review with no submission would loop to the
step budget. Routing through the sentinel (`environments/local.py:45`) makes the
review land deterministically in `info.submission`, from which the driver extracts
it (falling back to the last assistant message if the budget was hit before
submission). The same extraction path is used for both review conditions.

Prompt **filling** is literal `str.replace` of `{task}`/`{diff}`/`{review}`, never
`str.format`, because the task statement and unified diff routinely contain literal
`{`/`}` (code, JSON) that `str.format` would misparse.

## Citations

- McAleese et al., *LLM Critics Help Catch LLM Bugs*, arXiv 2407.00215, 2024.
- Shinn et al., *Reflexion*, arXiv 2303.11366, 2023; repo noahshinn/reflexion (MIT).
- Madaan et al., *Self-Refine*, arXiv 2303.17651, 2023; repo madaan/self-refine (Apache-2.0).
- mini-swe-agent, github.com/SWE-agent/mini-swe-agent (MIT), `config/benchmarks/swebench.yaml`.
