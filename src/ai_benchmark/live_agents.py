"""Harbor agent shims for the Experiment 001 live-model arm.

Unlike :mod:`ai_benchmark.pilot_agents` (deterministic, no-LLM instruments), the
agents here drive a real model through Harbor.

Two classes live here:

* :class:`MiniSweAgentLitellmProxy` works around an upstream packaging bug that
  otherwise silently destroys the measurement (Hazard 1 in
  ``experiments/001-adversarial-review/pilot/live/PILOT-LIVE.md``). It is the
  agent named in that document's reproduction commands, so its behaviour is kept
  frozen.
* :class:`ExperimentMiniSweAgent` extends that fix with the two harness controls
  the real (3-condition) run needs: explicit, constant step/cost bounds (Fix 1)
  and task text delivered out of ``argv`` (Fix 2). See
  ``experiments/001-adversarial-review/HARNESS.md``.

**On the zero-call validity guard (Fix 3), wrapper layer.** Harbor 0.18.0 exposes
no clean agent-lifecycle hook for failing a trial *after* the agent runs on a
post-run condition. The only post-run entry point, ``populate_context_post_run``,
is documented as best-effort context backfill (the base implementation swallows
every exception) and is invoked from the ``finally`` block of the agent phase, so
raising there would skip verification and surface as an unrelated error type. We
therefore do **not** attempt a wrapper-level zero-call abort and do **not**
monkey-patch Harbor; the zero-call verdict is emitted by the report layer
(:mod:`ai_benchmark.harbor_report`) from the recorded trajectory instead.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar, override

from harbor.agents.installed.base import CliFlag, with_prompt_template
from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.agents.utils import get_api_key_var_names_from_model_name

from ai_benchmark.review_driver import CONDITIONS as _REVIEW_CONDITIONS

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

#: Extra requirement injected into the mini-swe-agent tool environment.
#:
#: ``fastapi`` alone is not enough -- the offending import chain also reaches
#: ``orjson`` (and further proxy-only modules), so the packaging *extra* is
#: pinned rather than an ad-hoc list of individual modules.
LITELLM_PROXY_EXTRA = "litellm[proxy]"

#: Pinned default step budget (Fix 1). The *value* matters less than it being
#: explicit, constant across every arm, and recorded: with an unbounded step
#: budget the effective number of steps is a function of provider latency, which
#: makes arms and models incomparable. Overridable per run via
#: ``--ak step_limit=<n>`` (which also records it in ``config.json``); 0 is
#: rejected because unlimited is exactly the invalid configuration.
DEFAULT_STEP_LIMIT = 100

#: Pinned default cost budget in USD (Fix 1). A secondary guard only: enforced
#: against mini-swe-agent's *own* cost estimate, which under-reports real billing
#: (~8x in the pilot), so provider-side limits remain the authoritative bound
#: (PILOT-LIVE.md needs-list item 4, out of scope here). Still set explicitly and
#: constant rather than left at Harbor's forced ``0`` (unlimited).
DEFAULT_COST_LIMIT = 1.0

#: Sandbox path of the per-trial mini-swe-agent config carrying the task text and
#: the step/cost bounds. Delivering the task here (rather than ``--task`` in
#: argv) is Fix 2: it keeps the issue text out of every process's ``/proc``
#: cmdline, so an agent command like ``pkill -f <word-in-issue-text>`` can no
#: longer match -- and kill -- the agent's own process.
_TRIAL_CONFIG_PATH = "/tmp/mswea-experiment/trial.yaml"  # noqa: S108

#: PATH shim prepended to every in-sandbox command, mirroring upstream Harbor so
#: ``uv``-installed tools resolve regardless of shell init.
_PATH_SHIM = (
    'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
    'else export PATH="$HOME/.local/bin:$PATH"; fi; '
)

#: Sandbox directory holding the shipped review driver and its JSON config.
_REVIEW_SANDBOX_DIR = "/tmp/mswea-review"  # noqa: S108
#: Sandbox path of the shipped ``review_driver.py`` source.
_REVIEW_DRIVER_PATH = f"{_REVIEW_SANDBOX_DIR}/driver.py"
#: Sandbox path of the shipped driver config. Like the pilot's ``trial.yaml`` it
#: carries the task text; the write base64-encodes it, so the task stays off argv
#: (Fix 2). The driver reads its *path* from argv -- never the task text itself.
_REVIEW_CONFIG_PATH = f"{_REVIEW_SANDBOX_DIR}/config.json"

#: Repo-side sources shipped verbatim into the sandbox at build time. The prompts
#: are the registered ``critique.txt`` / ``revise.txt`` (see
#: ``experiments/001-adversarial-review/prompts/PROVENANCE.md``); the driver is the
#: pure-plus-wiring orchestrator in :mod:`ai_benchmark.review_driver`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_DIR = _REPO_ROOT / "experiments" / "001-adversarial-review" / "prompts"
_CRITIQUE_PROMPT_PATH = _PROMPTS_DIR / "critique.txt"
_REVISE_PROMPT_PATH = _PROMPTS_DIR / "revise.txt"
_REVIEW_DRIVER_SOURCE_PATH = Path(__file__).resolve().parent / "review_driver.py"


class MiniSweAgentLitellmProxy(MiniSweAgent):
    """``mini-swe-agent`` with litellm's ``proxy`` extra installed.

    **Why this exists.** ``mini-swe-agent`` v2 issues every model request with a
    ``tools=[BASH_TOOL]`` argument. In ``litellm`` 1.92.0, ``completion()``
    eagerly imports the MCP gateway handler whenever ``tools`` is truthy::

        skip_mcp_handler = kwargs.pop("_skip_mcp_handler", False)
        if not skip_mcp_handler and tools:
            from litellm.responses.mcp.chat_completions_handler import (
                acompletion_with_mcp,
            )
            ...
            if LiteLLM_Proxy_MCP_Handler._should_use_litellm_mcp_gateway(...):

    That import transitively pulls in ``litellm.proxy``, which requires
    ``fastapi``/``orjson`` -- modules shipped only in litellm's ``proxy`` extra
    and therefore absent from a plain ``uv tool install mini-swe-agent``. The
    import is unconditional even though the gateway itself is only *used* when
    MCP tools are present (they never are here), so **every** model query raises
    ``ModuleNotFoundError``.

    ``mini-swe-agent`` retries the failure with exponential backoff, so the agent
    burns its entire wall-clock timeout while making **zero** model calls and
    then exits non-zero. Harbor still runs the verifier, which grades an
    unmodified testbed. The trial therefore looks exactly like a competent
    harness measuring an incompetent model -- an evil measurement artifact -- when
    in fact no model was ever contacted.

    Installing the ``proxy`` extra satisfies the import and leaves the request
    path otherwise untouched: ``_should_use_litellm_mcp_gateway`` remains false
    for a plain bash tool, so the ordinary chat-completions path is taken.
    """

    @staticmethod
    @override
    def name() -> str:
        # Deliberately distinct from the built-in ``mini-swe-agent`` so a results
        # table cannot silently conflate the patched and unpatched agents.
        return "mini-swe-agent-litellm-proxy"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install ``mini-swe-agent``, then add the ``litellm`` proxy extra.

        Delegates to the upstream installer first (build tools, ``uv``, the tool
        itself) and then force-reinstalls the tool with the extra, so the
        resolver picks a ``litellm`` version consistent with whatever
        ``mini-swe-agent`` pins rather than upgrading it independently.
        """
        await super().install(environment)
        version_spec = f"=={self._version}" if self._version else ""
        await self.exec_as_agent(
            environment,
            command=(
                f"{_PATH_SHIM}"
                "set -euo pipefail; "
                f"uv tool install --force mini-swe-agent{version_spec} "
                f'--with "{LITELLM_PROXY_EXTRA}" && '
                # Fail loudly at install time rather than degrading into a
                # retry-until-timeout loop during the agent phase.
                'python_bin="$(uv tool dir)/mini-swe-agent/bin/python" && '
                '"$python_bin" -c "import fastapi, orjson"'
            ),
        )


def _coerce_positive_int(value: object, name: str) -> int:
    """Coerce *value* to an ``int`` and reject anything ``<= 0``.

    :param value: The raw kwarg value (``--ak`` parses ``100`` to ``int``, but a
        string is accepted defensively).
    :param name: The kwarg name, for error messages.
    :returns: The validated positive integer.
    :raises TypeError: If *value* is not an integer, float or numeric string.
    :raises ValueError: If *value* is not a whole number greater than zero.
    """
    # ``bool`` is an ``int`` subclass; a flag value is never a valid bound.
    if isinstance(value, bool):
        message = f"{name} must be a positive integer, not a bool"
        raise TypeError(message)
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if not value.is_integer():
            message = f"{name} must be a whole number, got {value!r}"
            raise ValueError(message)
        coerced = int(value)
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError as exc:
            message = f"{name} must be an integer, got {value!r}"
            raise ValueError(message) from exc
    else:
        message = f"{name} must be an integer, got {type(value).__name__}"
        raise TypeError(message)
    if coerced <= 0:
        message = (
            f"{name}={coerced} is unlimited/invalid; pass a positive bound. "
            "An unbounded step budget is exactly the invalid configuration this "
            "wrapper exists to prevent."
        )
        raise ValueError(message)
    return coerced


def _coerce_positive_float(value: object, name: str) -> float:
    """Coerce *value* to a ``float`` and reject anything ``<= 0``.

    :param value: The raw kwarg value.
    :param name: The kwarg name, for error messages.
    :returns: The validated positive float.
    :raises TypeError: If *value* is not a number or numeric string.
    :raises ValueError: If *value* is not a number greater than zero.
    """
    if isinstance(value, bool):
        message = f"{name} must be a positive number, not a bool"
        raise TypeError(message)
    if isinstance(value, (int, float)):
        coerced = float(value)
    elif isinstance(value, str):
        try:
            coerced = float(value)
        except ValueError as exc:
            message = f"{name} must be a number, got {value!r}"
            raise ValueError(message) from exc
    else:
        message = f"{name} must be a number, got {type(value).__name__}"
        raise TypeError(message)
    if coerced <= 0:
        message = f"{name}={coerced} is unlimited/invalid; pass a positive bound"
        raise ValueError(message)
    return coerced


class ExperimentMiniSweAgent(MiniSweAgentLitellmProxy):
    """Experiment-grade ``mini-swe-agent`` with bounded steps and no task in argv.

    Extends :class:`MiniSweAgentLitellmProxy` (the experiment always wants the
    packaging fix too) with the two remaining validity controls from the live
    pilot:

    **Fix 1 -- explicit, constant step & cost bounds.** Stock Harbor forces
    ``--cost-limit 0`` (unlimited) and leaves ``step_limit`` at mini.yaml's ``0``
    (also unlimited), so the only bound is wall-clock -- which makes effective
    step counts a function of provider latency and arms incomparable. This
    wrapper drops Harbor's forced flag (``CLI_FLAGS = []``) and sets both bounds
    from constructor kwargs (defaulting to :data:`DEFAULT_STEP_LIMIT` /
    :data:`DEFAULT_COST_LIMIT`), writing the resolved values into the per-trial
    config so mini-swe-agent records them in its trajectory's
    ``info.config.agent``. Passing ``step_limit``/``cost_limit`` via ``--ak``
    additionally records them verbatim in Harbor's ``config.json`` (agent
    ``kwargs``). A ``step_limit`` (or ``cost_limit``) of ``0`` is rejected.

    **Fix 2 -- task text out of argv.** Upstream passes the whole issue statement
    as ``--task='<text>'``; any agent command that runs ``pkill -f`` / ``pgrep
    -f`` on a word appearing in that statement then matches -- and kills -- the
    agent's own process (this SIGTERM'd trial 15098 mid-run). Instead the task is
    written to :data:`_TRIAL_CONFIG_PATH` and supplied through mini-swe-agent's
    native ``run.task`` config key via ``-c``; ``--task`` is omitted entirely, so
    the constructed agent command line never contains the task text. The write
    itself base64-encodes the payload, keeping the plaintext off ``argv`` there
    too.
    """

    #: Drop Harbor's ``cost_limit`` CLI flag entirely: its ``"0"`` default is the
    #: unlimited-budget bug. This wrapper sets the bound via the trial config.
    CLI_FLAGS: ClassVar[list[CliFlag]] = []

    def __init__(
        self,
        *args: object,
        step_limit: object = DEFAULT_STEP_LIMIT,
        cost_limit: object = DEFAULT_COST_LIMIT,
        **kwargs: object,
    ) -> None:
        """Validate and store the step/cost bounds, then defer to Harbor.

        :param args: Positional Harbor constructor arguments.
        :param step_limit: Maximum model calls; must be a positive integer.
            Supplied via ``--ak step_limit=<n>`` or left at
            :data:`DEFAULT_STEP_LIMIT`.
        :param cost_limit: Cost ceiling in USD; must be a positive number.
        :param kwargs: Additional Harbor constructor arguments.
        :raises ValueError: If either bound is zero/unlimited or non-numeric.
        """
        self._step_limit = _coerce_positive_int(step_limit, "step_limit")
        self._cost_limit = _coerce_positive_float(cost_limit, "cost_limit")
        # Harbor forwards its framework kwargs through the same splat; ty cannot
        # see that the factory only ever passes them by keyword.
        super().__init__(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    @staticmethod
    @override
    def name() -> str:
        return "mini-swe-agent-experiment"

    def _trial_config_content(self, instruction: str) -> str:
        """Return the per-trial mini-swe-agent config as JSON (valid YAML).

        Carries the step/cost bounds *and* the task text. JSON is a subset of
        YAML, so ``mini-swe-agent``'s ``yaml.safe_load`` parses it and the task
        string round-trips exactly without hand-rolled YAML quoting.
        """
        return json.dumps(
            {
                "agent": {
                    "step_limit": self._step_limit,
                    "cost_limit": self._cost_limit,
                },
                "run": {"task": instruction},
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _write_file_command(content: str, path: str) -> str:
        """Return a shell snippet writing *content* to *path* via base64.

        Base64 keeps the payload (which includes the task text) off the argv of
        every process in the write, so even this setup step cannot be matched by
        a later ``pkill -f``.
        """
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        directory = PurePosixPath(path).parent
        return f"mkdir -p {directory}; printf %s '{payload}' | base64 -d > {path}"

    def build_run_commands(self, instruction: str) -> tuple[str, str]:
        """Build the ``(write_task_file, run_agent)`` commands for *instruction*.

        Pure and environment-independent so the no-task-in-argv invariant can be
        asserted directly in tests. The returned agent command carries neither
        ``--task`` nor the task text: the task reaches mini-swe-agent through the
        ``run.task`` key of the ``-c`` config file written by the first command.

        :param instruction: The (already prompt-templated) task statement.
        :returns: ``(write_command, agent_command)`` shell strings.
        :raises ValueError: If ``model_name`` is missing or not ``provider/name``,
            or if a ``config_file`` was supplied (unsupported here).
        """
        if not self.model_name or "/" not in self.model_name:
            message = "Model name must be in the format provider/model_name"
            raise ValueError(message)
        # Upstream honors a ``config_file`` kwarg by layering its YAML into the
        # ``-c`` chain. This wrapper owns the trial config (bounds + task); an
        # extra config silently altering an arm is exactly the invisible skew the
        # experiment controls exist to prevent, so refuse loudly rather than drop.
        if self._config_yaml:
            message = (
                "config_file is not supported by ExperimentMiniSweAgent; "
                "experiment parameters must go through step_limit/cost_limit"
            )
            raise ValueError(message)

        write_command = self._write_file_command(
            self._trial_config_content(instruction),
            _TRIAL_CONFIG_PATH,
        )

        # ``-c`` is a spec list that *replaces* the packaged default, so load the
        # builtin ``mini`` config first and layer the trial config (bounds + task)
        # on top. Any model-tuning overrides follow so they win last.
        config_flags = f"-c mini -c {_TRIAL_CONFIG_PATH} "
        config_flags += self._model_tuning_config_flags()

        agent_command = (
            f"{_PATH_SHIM}"
            f"mini-swe-agent --yolo --model={self.model_name} "
            f"--output={self._mini_swe_agent_trajectory_path} "
            f"{config_flags}"
            "--exit-immediately 2>&1 </dev/null | tee /logs/agent/mini-swe-agent.txt"
        )
        return write_command, agent_command

    def _model_tuning_config_flags(self) -> str:
        """Return ``-c`` overrides for reasoning effort / max tokens, if set.

        Mirrors upstream Harbor's per-provider handling so this wrapper stays a
        faithful superset; the experiment leaves both unset, yielding ``""``.
        """
        flags = ""
        if self._reasoning_effort:
            effort = self._reasoning_effort
            if self.model_name and self.model_name.startswith("openai/"):
                flags += (
                    "-c model.model_class=litellm_response "
                    f"-c model.model_kwargs.reasoning.effort={effort} "
                )
            elif self.model_name and self.model_name.startswith("anthropic/"):
                flags += f"-c model.model_kwargs.reasoning_effort={effort} "
            else:
                flags += f"-c model.model_kwargs.extra_body.reasoning_effort={effort} "
        if self._max_tokens is not None:
            token_key = (
                "max_output_tokens"
                if self.model_name
                and self.model_name.startswith("openai/")
                and self._reasoning_effort
                else "max_tokens"
            )
            flags += f"-c model.model_kwargs.{token_key}={self._max_tokens} "
        return flags

    def _resolve_model_api_keys(self) -> dict[str, str]:
        """Resolve the model's provider API keys from the agent environment.

        :returns: ``{env_var: value}`` for each key the model needs.
        :raises ValueError: If the model is unknown or a required key is unset.
        """
        model_name = self.model_name or ""
        try:
            api_key_vars = get_api_key_var_names_from_model_name(model_name)
        except ValueError as exc:
            message = (
                f"Unable to determine API key for model {model_name}: {exc}. "
                "Please set MSWEA_API_KEY environment variable as fallback"
            )
            raise ValueError(message) from exc
        resolved: dict[str, str] = {}
        for api_key_var in api_key_vars:
            api_key = self._get_env(api_key_var)
            if api_key is None:
                message = (
                    f"Unset API variable for model {model_name}. Please set "
                    f"{api_key_var} or MSWEA_API_KEY environment variable"
                )
                raise ValueError(message)
            resolved[api_key_var] = api_key
        return resolved

    def _build_exec_env(self) -> dict[str, str]:
        """Assemble the mini-swe-agent process environment (keys, base URL).

        Reproduces upstream Harbor's key resolution so the litellm proxy and API
        keys are wired identically; only the command construction differs.

        :returns: Environment variables for the agent exec.
        :raises ValueError: If no API key can be resolved for the model.
        """
        env = {
            "MSWEA_CONFIGURED": "true",  # Disable interactive setup
            "MSWEA_COST_TRACKING": "ignore_errors",  # Ignore unknown model costs
        }
        mswea_api_key = self._get_env("MSWEA_API_KEY")
        if mswea_api_key is not None:
            env["MSWEA_API_KEY"] = mswea_api_key
        else:
            env.update(self._resolve_model_api_keys())

        # OPENAI_API_BASE (LiteLLM) and OPENAI_BASE_URL (OpenAI SDK) are two names
        # for the same value; forward both under whichever is set.
        api_base = self._get_env("OPENAI_BASE_URL") or self._get_env("OPENAI_API_BASE")
        if api_base is not None:
            env["OPENAI_BASE_URL"] = api_base
            env["OPENAI_API_BASE"] = api_base
        return env

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Write the task config, then run mini-swe-agent without ``--task``.

        The ``context`` is populated post-run by the inherited
        ``populate_context_post_run``; it is unused here.
        """
        del context
        if self.mcp_servers:
            mcp_info = (
                "\n\nMCP Servers:\nThe following MCP servers are available for "
                "this task.\n"
            )
            for server in self.mcp_servers:
                if server.transport == "stdio":
                    args_str = " ".join(server.args)
                    mcp_info += (
                        f"- {server.name}: stdio transport, command: "
                        f"{server.command} {args_str}\n"
                    )
                else:
                    mcp_info += (
                        f"- {server.name}: {server.transport} transport, "
                        f"url: {server.url}\n"
                    )
            instruction += mcp_info

        env = self._build_exec_env()
        write_command, agent_command = self.build_run_commands(instruction)
        await self.exec_as_agent(environment, command=write_command, env=env)
        await self.exec_as_agent(environment, command=agent_command, env=env)


class ExperimentReviewAgent(ExperimentMiniSweAgent):
    """Implement->review->revise treatment for Experiment 001 (three conditions).

    Extends :class:`ExperimentMiniSweAgent` -- and therefore keeps every earlier
    control: the litellm ``proxy`` extra (Hazard 1), bounded step/cost validated at
    construction (Fix 1), and task text off ``argv`` (Fix 2). The added variable is
    a single ``condition`` knob selecting *where the review happens* -- the only
    manipulated variable of the experiment (``DESIGN.md`` § Conditions):

    * ``control`` -- implement only.
    * ``self_review`` -- the author reviews its own patch in its own context, then
      revises.
    * ``adversarial`` -- a fresh context of the same model reviews a blind copy,
      and the author revises using that review.

    **Why a driver, not the CLI.** ``mini-swe-agent``'s ``mini`` CLI cannot continue
    a conversation across phases: :meth:`DefaultAgent.run` resets ``self.messages``
    on entry (``agents/default.py:91``) and there is no resume mechanism. "Same
    context" (``self_review``) is only achievable by keeping one live
    ``agent.messages`` object across phases, so all three conditions run through the
    in-sandbox library driver :mod:`ai_benchmark.review_driver` (shipped verbatim,
    same machinery for every arm). This wrapper's job is purely to *ship* that
    driver plus the registered prompts and a JSON config, then invoke it under the
    ``mini-swe-agent`` tool venv's Python.

    The task text and the prompts travel inside the base64-written JSON config
    (never argv); the driver receives only the config *path* on argv, preserving
    the Fix-2 invariant. The name is deliberately distinct so a results table cannot
    conflate this agent with the implement-only :class:`ExperimentMiniSweAgent`.
    """

    def __init__(
        self,
        *args: object,
        condition: object,
        step_limit: object = DEFAULT_STEP_LIMIT,
        cost_limit: object = DEFAULT_COST_LIMIT,
        **kwargs: object,
    ) -> None:
        """Validate the ``condition`` knob, then defer to the bounded parent.

        :param args: Positional Harbor constructor arguments.
        :param condition: One of ``control`` / ``self_review`` / ``adversarial``
            (supplied via ``--ak condition=<name>``). Any other value is rejected.
        :param step_limit: Per-phase model-call budget (validated by the parent).
        :param cost_limit: Per-phase cost budget in USD (validated by the parent).
        :param kwargs: Additional Harbor constructor arguments.
        :raises ValueError: If ``condition`` is missing or not a known condition.
        """
        if not isinstance(condition, str) or condition not in _REVIEW_CONDITIONS:
            message = (
                f"condition must be one of {sorted(_REVIEW_CONDITIONS)}, "
                f"got {condition!r}"
            )
            raise ValueError(message)
        self._condition = condition
        super().__init__(*args, step_limit=step_limit, cost_limit=cost_limit, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        # Distinct from the implement-only experiment agent so the review arms are
        # never conflated with the control-only wrapper in a results table.
        return "mini-swe-agent-review"

    def _review_config_content(self, instruction: str) -> str:
        """Return the driver config as JSON, embedding the task and both prompts.

        The registered ``critique.txt`` / ``revise.txt`` are read at build time and
        shipped verbatim, so the operative prompts are exactly the vendored ones.
        The canonical author-trajectory path is passed through so the driver writes
        the author's FINAL (post-revise) trajectory where ``harbor_report`` and the
        zero-call validity guard read it.

        :param instruction: The (prompt-templated) task statement.
        :returns: The JSON config text (base64-written into the sandbox).
        """
        critique = _CRITIQUE_PROMPT_PATH.read_text(encoding="utf-8")
        revise = _REVISE_PROMPT_PATH.read_text(encoding="utf-8")
        return json.dumps(
            {
                "task": instruction,
                "condition": self._condition,
                "model_name": self.model_name,
                "step_limit": self._step_limit,
                "cost_limit": self._cost_limit,
                "critique": critique,
                "revise": revise,
                "author_trajectory": str(self._mini_swe_agent_trajectory_path),
            },
            ensure_ascii=False,
            indent=2,
        )

    @override
    def build_run_commands(self, instruction: str) -> tuple[str, str]:
        """Build the ``(ship_driver_and_config, run_driver)`` commands.

        Pure and environment-independent so the no-task-in-argv invariant can be
        asserted directly in tests. The returned commands carry neither ``--task``
        nor the task text nor the prompt text on argv: all of it is inside the
        base64-written JSON config; the driver reads only its *path*.

        :param instruction: The (prompt-templated) task statement.
        :returns: ``(setup_command, driver_command)`` shell strings.
        :raises ValueError: If ``model_name`` is missing or not ``provider/name``,
            if a ``config_file`` was supplied, or if ``reasoning_effort`` /
            ``max_tokens`` were set (not plumbed through the v1 driver -- refuse
            loudly rather than silently drop and skew an arm).
        """
        if not self.model_name or "/" not in self.model_name:
            message = "Model name must be in the format provider/model_name"
            raise ValueError(message)
        if self._config_yaml:
            message = (
                "config_file is not supported by ExperimentReviewAgent; "
                "experiment parameters must go through condition/step_limit/cost_limit"
            )
            raise ValueError(message)
        if self._reasoning_effort is not None or self._max_tokens is not None:
            message = (
                "reasoning_effort/max_tokens are not plumbed through the review "
                "driver in v1; leave them unset (the experiment holds them unset)"
            )
            raise ValueError(message)

        driver_source = _REVIEW_DRIVER_SOURCE_PATH.read_text(encoding="utf-8")
        write_driver = self._write_file_command(driver_source, _REVIEW_DRIVER_PATH)
        write_config = self._write_file_command(
            self._review_config_content(instruction),
            _REVIEW_CONFIG_PATH,
        )
        setup_command = f"{write_driver}; {write_config}"

        # Run the driver under the mini tool venv's Python -- the one the litellm
        # ``proxy`` extra was installed into, so ``minisweagent`` and its deps
        # import cleanly. The task text is inside the config file, so only the
        # config *path* appears on argv.
        mini_python = '"$(uv tool dir)/mini-swe-agent/bin/python"'
        driver_command = (
            f"{_PATH_SHIM}"
            f"{mini_python} {_REVIEW_DRIVER_PATH} {_REVIEW_CONFIG_PATH} "
            "2>&1 </dev/null | tee /logs/agent/mini-swe-agent-review.txt"
        )
        return setup_command, driver_command
