# Experiment 002 — Context-injection strategies beyond RAG

**Status: open question. No design yet — this file is the seed.**

## Terminology (to get ahead of the debate)

- **Agent:** an LLM in a loop that uses tools.
- **Retrieval Augmented Generation (RAG):** adding tokens to the LLM's context
  automatically to (theoretically) improve outputs.

Ignore the weeds: e.g. "subagents", when spawned by a tool of an agent
(i.e. recursion), are just part of the agentic workflow and don't need a
call-out. That is different from, say, the current hype of injecting RAG
before the agent loop starts
(<https://agentskills.io/specification#description-field>).

## The question

You can have an agent that doesn't use RAG. RAG is an unusually *specific*
strategy. If we posit that RAG bolted onto agents improves them, and RAG is an
unusually specific strategy… are there **other** strategies that improve
quality?

Candidates on the radar:

- **Background context curation** —
  [Familiar-Connect](https://github.com/CorVous/Familiar-Connect) uses
  (theoretically) cheap, purposeful models in the *background* which curate
  optimized chunks of context to inject into the KV cache when needed; a
  system modeled after the human subconscious. No established name or
  researched precedent yet. Most interesting axes: **cost** and **speed** —
  which unfortunately requires addressing the cache-management elephant in
  the room.
- **Sequential single-tasking** — the
  [re-cast](https://github.com/closuretxt/recast-post-processing) strategy of
  making a cheap LLM do **one thing at a time** in sequence, since LLMs (and
  especially cheap ones) are bad at multi-tasking. A heavier-handed version of
  the task lists that were popular in Copilot and other harnesses.

## Caveat

Whether the AI-wrangling strategy is invoked by the agentic workflow or is
forcibly injected isn't the phenomenon under study. If a strategy is good and
available, the agent's RL should ensure it gets selected when relevant.
