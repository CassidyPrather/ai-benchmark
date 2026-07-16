"""Harbor agent shims for the Experiment 001 live-model arm.

Unlike :mod:`ai_benchmark.pilot_agents` (deterministic, no-LLM instruments), the
agents here drive a real model through Harbor.

:class:`MiniSweAgentLitellmProxy` exists solely to work around an upstream
packaging bug that otherwise silently destroys the measurement. See the class
docstring for the full diagnosis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from harbor.agents.installed.mini_swe_agent import MiniSweAgent

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment

#: Extra requirement injected into the mini-swe-agent tool environment.
#:
#: ``fastapi`` alone is not enough -- the offending import chain also reaches
#: ``orjson`` (and further proxy-only modules), so the packaging *extra* is
#: pinned rather than an ad-hoc list of individual modules.
LITELLM_PROXY_EXTRA = "litellm[proxy]"


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
                'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
                'else export PATH="$HOME/.local/bin:$PATH"; fi; '
                "set -euo pipefail; "
                f"uv tool install --force mini-swe-agent{version_spec} "
                f'--with "{LITELLM_PROXY_EXTRA}" && '
                # Fail loudly at install time rather than degrading into a
                # retry-until-timeout loop during the agent phase.
                'python_bin="$(uv tool dir)/mini-swe-agent/bin/python" && '
                '"$python_bin" -c "import fastapi, orjson"'
            ),
        )
