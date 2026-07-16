"""Tests for :mod:`ai_benchmark.live_agents`.

These pin the workaround for the litellm packaging bug documented in
:class:`~ai_benchmark.live_agents.MiniSweAgentLitellmProxy`. The bug is
measurement-critical: without the extra, every model query raises
``ModuleNotFoundError`` and the trial silently degrades into "agent made no
edits" rather than failing loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ai_benchmark.live_agents import LITELLM_PROXY_EXTRA, MiniSweAgentLitellmProxy

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment

_UPSTREAM_INSTALL = "<upstream-install>"


def _fake_environment() -> BaseEnvironment:
    """Return a stand-in environment; the recorded commands never execute."""
    return cast("BaseEnvironment", object())


class _RecordingAgent(MiniSweAgentLitellmProxy):
    """Record the commands ``install`` issues, without a real environment."""

    def __init__(self, version: str | None = None) -> None:
        """Bypass Harbor's constructor; these tests exercise ``install`` only."""
        self._version = version
        self.commands: list[str] = []

    async def exec_as_agent(
        self,
        environment: BaseEnvironment,
        command: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> None:
        """Capture *command* instead of running it."""
        del environment, env, cwd, timeout_sec
        self.commands.append(command)


def _stub_upstream_install(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _RecordingAgent,
) -> None:
    """Replace the upstream installer with a marker so ordering is observable."""

    # Must be async to stand in for the async method it replaces, even though
    # the body has nothing to await.
    async def fake_install(_self: object, _environment: object) -> None:  # noqa: RUF029
        recorder.commands.append(_UPSTREAM_INSTALL)

    monkeypatch.setattr(
        "harbor.agents.installed.mini_swe_agent.MiniSweAgent.install",
        fake_install,
    )


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> _RecordingAgent:
    """Build a recording agent whose upstream ``install`` is stubbed out."""
    recorder = _RecordingAgent()
    _stub_upstream_install(monkeypatch, recorder)
    return recorder


def test_name_is_distinct_from_builtin() -> None:
    """Ensure the patched agent is not conflatable with stock mini-swe-agent."""
    assert MiniSweAgentLitellmProxy.name() == "mini-swe-agent-litellm-proxy"
    assert MiniSweAgentLitellmProxy.name() != "mini-swe-agent"


@pytest.mark.trio
async def test_install_runs_upstream_first(agent: _RecordingAgent) -> None:
    """Run the upstream install (build tools, uv, the tool itself) first."""
    await agent.install(_fake_environment())
    assert agent.commands[0] == _UPSTREAM_INSTALL
    assert len(agent.commands) == 2


@pytest.mark.trio
async def test_install_adds_litellm_proxy_extra(agent: _RecordingAgent) -> None:
    """Add the proxy extra that satisfies the eager ``litellm.proxy`` import."""
    await agent.install(_fake_environment())
    command = agent.commands[1]
    assert f'--with "{LITELLM_PROXY_EXTRA}"' in command
    assert "uv tool install --force mini-swe-agent " in command


@pytest.mark.trio
async def test_install_verifies_imports_eagerly(agent: _RecordingAgent) -> None:
    """Fail loudly at install time, not via a retry-until-timeout loop."""
    await agent.install(_fake_environment())
    command = agent.commands[1]
    assert 'import fastapi, orjson"' in command
    assert "set -euo pipefail" in command


@pytest.mark.trio
async def test_install_pins_version_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Honour a pinned agent version in the force-reinstall."""
    recorder = _RecordingAgent(version="2.4.5")
    _stub_upstream_install(monkeypatch, recorder)
    await recorder.install(_fake_environment())
    assert "mini-swe-agent==2.4.5" in recorder.commands[1]
