"""Tests for :mod:`ai_benchmark.live_agents`.

These pin the workaround for the litellm packaging bug documented in
:class:`~ai_benchmark.live_agents.MiniSweAgentLitellmProxy`. The bug is
measurement-critical: without the extra, every model query raises
``ModuleNotFoundError`` and the trial silently degrades into "agent made no
edits" rather than failing loudly.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ai_benchmark.live_agents import (
    DEFAULT_COST_LIMIT,
    DEFAULT_STEP_LIMIT,
    LITELLM_PROXY_EXTRA,
    ExperimentMiniSweAgent,
    ExperimentReviewAgent,
    MiniSweAgentLitellmProxy,
)

if TYPE_CHECKING:
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

#: Repo-side sources the review agent ships into the sandbox, resolved from this
#: test file so the verbatim-shipping assertions read the same bytes the wrapper
#: does (without importing the wrapper's private path constants).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = _REPO_ROOT / "experiments" / "001-adversarial-review" / "prompts"
_REVIEW_DRIVER_SOURCE = _REPO_ROOT / "src" / "ai_benchmark" / "review_driver.py"

_UPSTREAM_INSTALL = "<upstream-install>"

# A task statement carrying the exact self-kill trap from the pilot (Hazard 2):
# the issue text contains "runserver", and an agent that runs `pkill -f runserver`
# would SIGTERM its own process if this text were in the agent's argv.
_SELF_KILL_TASK = (
    "Fix the dev server: cd proj && python manage.py runserver 8001 & "
    'sleep 5 && pkill -f runserver. Handle "quotes", colons: and unicode cafe.'
)


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


# --- ExperimentMiniSweAgent: Fix 1 (bounded steps/cost) + Fix 2 (task off argv) ---

_MODEL = "openrouter/qwen/qwen3-coder"


def _experiment_agent(
    tmp_path: Path,
    **kwargs: object,
) -> ExperimentMiniSweAgent:
    """Construct a real :class:`ExperimentMiniSweAgent` (no network at init)."""
    return ExperimentMiniSweAgent(
        logs_dir=tmp_path,
        model_name=_MODEL,
        **kwargs,
    )


def _decode_task_file(write_command: str) -> tuple[str, dict[str, object]]:
    """Return ``(path, parsed_config)`` from the base64 task-file write command."""
    payload = re.search(r"printf %s '([A-Za-z0-9+/=]+)'", write_command)
    assert payload is not None, write_command
    target = re.search(r"> (\S+)$", write_command)
    assert target is not None, write_command
    content = base64.b64decode(payload.group(1)).decode("utf-8")
    return target.group(1), json.loads(content)


def test_experiment_name_is_distinct() -> None:
    """The experiment agent is not conflatable with the pilot's patched agent."""
    assert ExperimentMiniSweAgent.name() == "mini-swe-agent-experiment"
    assert ExperimentMiniSweAgent.name() != MiniSweAgentLitellmProxy.name()
    assert ExperimentMiniSweAgent.name() != "mini-swe-agent"


def test_experiment_drops_forced_cost_limit_flag() -> None:
    """Harbor's forced ``--cost-limit 0`` flag is removed, not merely overridden."""
    assert ExperimentMiniSweAgent.CLI_FLAGS == []


@pytest.mark.parametrize("bad", [0, 0.0, "0", -1, -5.0])
def test_rejects_unlimited_step_limit(tmp_path: Path, bad: object) -> None:
    """An explicit 0/unlimited (or negative) step_limit is refused."""
    with pytest.raises(ValueError, match="step_limit"):
        _experiment_agent(tmp_path, step_limit=bad)


def test_rejects_unlimited_cost_limit(tmp_path: Path) -> None:
    """An explicit 0/unlimited cost_limit is refused."""
    with pytest.raises(ValueError, match="cost_limit"):
        _experiment_agent(tmp_path, cost_limit=0)


def test_rejects_non_integer_step_limit(tmp_path: Path) -> None:
    """A bool masquerading as an int bound is rejected as a type error."""
    with pytest.raises(TypeError, match="step_limit"):
        _experiment_agent(tmp_path, step_limit=True)


def test_default_bounds_are_recorded_in_trial_config(tmp_path: Path) -> None:
    """Pinned defaults land in the config mini-swe-agent records in its trajectory."""
    agent = _experiment_agent(tmp_path)
    write_command, _ = agent.build_run_commands("do the thing")
    _, config = _decode_task_file(write_command)
    assert config["agent"] == {
        "step_limit": DEFAULT_STEP_LIMIT,
        "cost_limit": DEFAULT_COST_LIMIT,
    }


def test_explicit_bounds_flow_into_trial_config(tmp_path: Path) -> None:
    """Bounds passed as kwargs (e.g. via --ak) override the pinned defaults."""
    agent = _experiment_agent(tmp_path, step_limit=42, cost_limit=2.5)
    write_command, _ = agent.build_run_commands("do the thing")
    _, config = _decode_task_file(write_command)
    assert config["agent"] == {"step_limit": 42, "cost_limit": 2.5}


def test_task_text_never_in_agent_command(tmp_path: Path) -> None:
    """Fix 2 invariant: the constructed agent command omits the task text."""
    agent = _experiment_agent(tmp_path)
    _, agent_command = agent.build_run_commands(_SELF_KILL_TASK)
    # The self-kill trap word, and any recognisable slice of the task, are absent.
    assert "runserver" not in agent_command
    assert "pkill" not in agent_command
    assert _SELF_KILL_TASK not in agent_command
    # No --task in argv, and Harbor's forced --cost-limit is gone.
    assert "--task" not in agent_command
    assert "--cost-limit" not in agent_command
    # The task is delivered via the builtin config plus the written trial config.
    assert "-c mini " in agent_command


def test_task_delivered_via_config_file_exactly(tmp_path: Path) -> None:
    """The task reaches mini-swe-agent through run.task, byte-for-byte intact."""
    agent = _experiment_agent(tmp_path)
    write_command, agent_command = agent.build_run_commands(_SELF_KILL_TASK)
    path, config = _decode_task_file(write_command)
    assert config["run"] == {"task": _SELF_KILL_TASK}
    # The write itself keeps the plaintext off argv (payload is base64-encoded).
    assert "runserver" not in write_command
    # The agent command reads exactly the file the write command produced.
    assert f"-c {path} " in agent_command


def test_build_run_commands_requires_provider_model(tmp_path: Path) -> None:
    """A bare model name (no provider/) is rejected before any command is built."""
    agent = _experiment_agent(tmp_path)
    agent.model_name = "qwen3-coder"
    with pytest.raises(ValueError, match="provider/model_name"):
        agent.build_run_commands("task")


def test_rejects_config_file_rather_than_dropping_it(tmp_path: Path) -> None:
    """A supplied config_file must refuse loudly, not silently skew an arm."""
    config = tmp_path / "extra.yaml"
    config.write_text("agent:\n  step_limit: 7\n", encoding="utf-8")
    agent = _experiment_agent(tmp_path, config_file=str(config))
    with pytest.raises(ValueError, match="config_file is not supported"):
        agent.build_run_commands("task")


class _RecordingExperimentAgent(ExperimentMiniSweAgent):
    """Capture the commands ``run`` issues instead of executing them."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Record exec calls; delegates construction (and validation) upstream."""
        super().__init__(*args, **kwargs)
        self.execs: list[str] = []

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
        self.execs.append(command)


@pytest.mark.trio
async def test_run_writes_task_file_then_invokes_agent(tmp_path: Path) -> None:
    """run() writes the task config first, then runs the clean agent command."""
    agent = _RecordingExperimentAgent(
        logs_dir=tmp_path,
        model_name=_MODEL,
        extra_env={"MSWEA_API_KEY": "dummy-not-a-real-key"},
    )
    context = cast("AgentContext", object())
    await agent.run(_SELF_KILL_TASK, _fake_environment(), context)
    assert len(agent.execs) == 2
    write_command, agent_command = agent.execs
    assert "base64 -d >" in write_command
    assert "mini-swe-agent --yolo" in agent_command
    assert "runserver" not in agent_command
    assert "--task" not in agent_command


# --- ExperimentReviewAgent: the implement->review->revise treatment ---------


def _review_agent(
    tmp_path: Path,
    *,
    condition: object = "adversarial",
    **kwargs: object,
) -> ExperimentReviewAgent:
    """Construct a real :class:`ExperimentReviewAgent` (no network at init)."""
    return ExperimentReviewAgent(
        logs_dir=tmp_path,
        model_name=_MODEL,
        condition=condition,
        **kwargs,
    )


def _decode_shipped_files(setup_command: str) -> dict[str, str]:
    """Return ``{sandbox_path: decoded_content}`` for each base64 write it does."""
    writes = re.findall(
        r"printf %s '([A-Za-z0-9+/=]+)' \| base64 -d > ([^\s;]+)",
        setup_command,
    )
    assert writes, setup_command
    return {path: base64.b64decode(payload).decode("utf-8") for payload, path in writes}


def _shipped_config(setup_command: str) -> dict[str, object]:
    """Return the parsed driver config from the shipped ``config.json``."""
    files = _decode_shipped_files(setup_command)
    content = next(text for path, text in files.items() if path.endswith("config.json"))
    return json.loads(content)


def _shipped_driver(setup_command: str) -> str:
    """Return the shipped driver source (the ``driver.py`` write)."""
    files = _decode_shipped_files(setup_command)
    return next(text for path, text in files.items() if path.endswith("driver.py"))


def test_review_name_is_distinct() -> None:
    """The review agent is not conflatable with the implement-only arms."""
    assert ExperimentReviewAgent.name() == "mini-swe-agent-review"
    assert ExperimentReviewAgent.name() != ExperimentMiniSweAgent.name()
    assert ExperimentReviewAgent.name() != MiniSweAgentLitellmProxy.name()


@pytest.mark.parametrize("condition", ["control", "self_review", "adversarial"])
def test_review_accepts_known_conditions(tmp_path: Path, condition: str) -> None:
    """Each of the three conditions constructs and lands in the shipped config."""
    agent = _review_agent(tmp_path, condition=condition)
    setup_command, _ = agent.build_run_commands("do the thing")
    assert _shipped_config(setup_command)["condition"] == condition


@pytest.mark.parametrize("bad", ["different_model", "", "CONTROL", None, 3])
def test_review_rejects_unknown_condition(tmp_path: Path, bad: object) -> None:
    """An unknown/invalid condition is refused at construction."""
    with pytest.raises(ValueError, match="condition must be one of"):
        _review_agent(tmp_path, condition=bad)


def test_review_condition_is_required(tmp_path: Path) -> None:
    """Omitting the condition knob is a hard error (no silent default arm)."""
    with pytest.raises(TypeError):
        ExperimentReviewAgent(logs_dir=tmp_path, model_name=_MODEL)  # ty: ignore[missing-argument]


def test_review_inherits_bound_validation(tmp_path: Path) -> None:
    """The inherited Fix-1 guard still rejects an unlimited step budget."""
    with pytest.raises(ValueError, match="step_limit"):
        _review_agent(tmp_path, step_limit=0)


def test_review_ships_registered_prompts_verbatim(tmp_path: Path) -> None:
    """The shipped config embeds critique.txt and revise.txt byte-for-byte."""
    agent = _review_agent(tmp_path)
    setup_command, _ = agent.build_run_commands("do the thing")
    config = _shipped_config(setup_command)
    critique = (_PROMPTS_DIR / "critique.txt").read_text(encoding="utf-8")
    revise = (_PROMPTS_DIR / "revise.txt").read_text(encoding="utf-8")
    assert config["critique"] == critique
    assert config["revise"] == revise


def test_review_ships_driver_source(tmp_path: Path) -> None:
    """The driver source is shipped verbatim into the sandbox."""
    agent = _review_agent(tmp_path)
    setup_command, _ = agent.build_run_commands("do the thing")
    driver_source = _shipped_driver(setup_command)
    assert driver_source == _REVIEW_DRIVER_SOURCE.read_text(encoding="utf-8")
    assert "def run_review_experiment(" in driver_source


def test_review_bounds_flow_into_config(tmp_path: Path) -> None:
    """Explicit bounds (e.g. via --ak) land in the driver config."""
    agent = _review_agent(tmp_path, step_limit=42, cost_limit=2.5)
    setup_command, _ = agent.build_run_commands("do the thing")
    config = _shipped_config(setup_command)
    assert config["step_limit"] == 42
    assert config["cost_limit"] == pytest.approx(2.5)


def test_review_defaults_bounds_in_config(tmp_path: Path) -> None:
    """The pinned default bounds are recorded when none are supplied."""
    agent = _review_agent(tmp_path)
    setup_command, _ = agent.build_run_commands("do the thing")
    config = _shipped_config(setup_command)
    assert config["step_limit"] == DEFAULT_STEP_LIMIT
    assert config["cost_limit"] == pytest.approx(DEFAULT_COST_LIMIT)


def test_review_task_never_in_any_command(tmp_path: Path) -> None:
    """Fix-2 invariant holds for the review agent: no task text on argv.

    The task (and both prompts) travel inside the base64 JSON config; neither the
    setup command nor the driver command may contain the task text or ``--task``.
    """
    agent = _review_agent(tmp_path)
    setup_command, driver_command = agent.build_run_commands(_SELF_KILL_TASK)
    for command in (setup_command, driver_command):
        assert "runserver" not in command
        assert "pkill" not in command
        assert _SELF_KILL_TASK not in command
    assert "--task" not in driver_command
    # But the task IS delivered intact inside the shipped config file.
    assert _shipped_config(setup_command)["task"] == _SELF_KILL_TASK


def test_review_driver_command_uses_mini_venv_python(tmp_path: Path) -> None:
    """The driver runs under the mini tool venv Python, reading only the config path."""
    agent = _review_agent(tmp_path)
    _, driver_command = agent.build_run_commands("do the thing")
    assert '"$(uv tool dir)/mini-swe-agent/bin/python"' in driver_command
    # The driver and its config are addressed by path only (no task text on argv).
    assert "driver.py" in driver_command
    assert "config.json" in driver_command
    # Unattended stdin so the interactive agent never blocks on a prompt.
    assert "</dev/null" in driver_command


def test_review_refuses_config_file(tmp_path: Path) -> None:
    """A supplied config_file must refuse loudly, not silently skew an arm."""
    config = tmp_path / "extra.yaml"
    config.write_text("agent:\n  step_limit: 7\n", encoding="utf-8")
    agent = _review_agent(tmp_path, config_file=str(config))
    with pytest.raises(ValueError, match="config_file is not supported"):
        agent.build_run_commands("task")


def test_review_refuses_reasoning_effort(tmp_path: Path) -> None:
    """reasoning_effort is not plumbed through the v1 driver, so it is refused."""
    agent = _review_agent(tmp_path, reasoning_effort="high")
    with pytest.raises(ValueError, match="reasoning_effort/max_tokens"):
        agent.build_run_commands("task")


def test_review_requires_provider_model(tmp_path: Path) -> None:
    """A bare model name (no provider/) is rejected before any command is built."""
    agent = _review_agent(tmp_path)
    agent.model_name = "qwen3-coder"
    with pytest.raises(ValueError, match="provider/model_name"):
        agent.build_run_commands("task")
