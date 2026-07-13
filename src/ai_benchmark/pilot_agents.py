"""Custom Harbor agents for the Experiment 001 adversarial-review pilot.

These host-side agents drive a SWE-bench-style task environment **without any LLM
API calls**, so the benchmark instrument (task orchestration, verifier isolation
and per-test PASS_TO_PASS regression reporting) can be validated deterministically.

* :class:`GoldPatchAgent` applies the task's reference (gold) patch. It is
  equivalent to Harbor's built-in ``oracle`` agent but self-contained: it reads
  the patch from the task's ``tests/config.json`` rather than requiring a shipped
  ``solution/solve.sh``.
* :class:`SaboteurAgent` applies the gold patch and then one deterministic,
  documented source regression. The regression is designed to break a specific
  hidden PASS_TO_PASS test while leaving the task's own FAIL_TO_PASS tests
  passing -- exactly the "looks resolved but silently regresses" case that
  adversarial review is meant to catch.

The class hierarchy (a base patch-application agent plus per-condition
subclasses) is the skeleton for the real experiment's condition wrapper: each
future condition (no-review, self-review, adversarial-review) becomes another
subclass that overrides :meth:`_TestbedPatchAgent._apply`.
"""

from __future__ import annotations

import base64
import json
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, override

from harbor.agents.base import BaseAgent

if TYPE_CHECKING:
    import logging

    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

_TESTBED = "/testbed"
_GOLD_PATCH_PATH = "/tmp/harbor_gold.patch"  # noqa: S108
_REGRESSION_SCRIPT_PATH = "/tmp/harbor_regression.py"  # noqa: S108


@dataclass(frozen=True)
class Regression:
    """A single deterministic source edit injected on top of the gold patch.

    :param file: Path of the file to edit, relative to the testbed root.
    :param find: Exact substring to replace; must occur verbatim and be unique.
    :param replace: Replacement substring.
    :param target_test: The hidden PASS_TO_PASS test expected to fail as a result.
    :param description: Human-readable rationale for the pilot write-up.
    """

    file: str
    find: str
    replace: str
    target_test: str
    description: str


# Per-instance saboteur regressions. Keyed by SWE-bench instance id. Each entry
# is chosen to regress a bounded, named set of PASS_TO_PASS tests (ideally one),
# so the pilot can show the regression surfacing at per-test granularity.
REGRESSIONS: dict[str, Regression] = {
    "django__django-15098": Regression(
        file="django/utils/translation/__init__.py",
        find="        return locale[:p].lower() + '-' + locale[p + 1:].lower()",
        replace="        return locale[:p].upper() + '-' + locale[p + 1:].lower()",
        target_test="test_to_language (i18n.tests.TranslationTests)",
        description=(
            "Upper-cases the language subtag in django.utils.translation."
            "to_language so to_language('en_US') returns 'EN-us' instead of "
            "'en-us'. Regresses exactly the hidden PASS_TO_PASS test "
            "test_to_language while the task's own FAIL_TO_PASS locale-regex "
            "tests still pass (partial P2P failure)."
        ),
    ),
    "django__django-16429": Regression(
        file="django/utils/timesince.py",
        find='time_strings["minute"] % {"num": 0}',
        replace='time_strings["hour"] % {"num": 0}',
        target_test="utils_tests.test_timesince zero-duration tests",
        description=(
            "Reports the zero / sub-minute timesince result as '0 hours' "
            "instead of '0 minutes', regressing 13 PASS_TO_PASS timesince "
            "tests that compare equal or ordered datetimes (broad P2P failure)."
        ),
    ),
    "django__django-16315": Regression(
        file="django/db/models/query.py",
        find="setattr(obj_without_pk, field.attname, result)",
        replace="field  # saboteur: drop generated-PK write-back",
        target_test="bulk_create.tests.BulkCreateTests PK-write-back tests",
        description=(
            "Drops the write-back of database-generated primary keys onto "
            "newly bulk-created objects, regressing 3 PASS_TO_PASS tests that "
            "assert on obj.pk after bulk_create while the task's own "
            "FAIL_TO_PASS test still passes (partial P2P failure)."
        ),
    ),
}


def _shell_write_file(content: str, target: str) -> str:
    """Return a shell snippet that writes *content* to *target* via base64.

    Base64 keeps arbitrary patch/script text (quotes, newlines, ``$``) clear of
    shell quoting rules: the encoded payload only uses characters that are inert
    inside single quotes.
    """
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf %s '{payload}' | base64 -d > {target}"


class _TestbedPatchAgent(BaseAgent):
    """Base agent that mutates a checked-out testbed by applying patches.

    Subclasses implement :meth:`_apply` to perform their condition-specific
    edits. Construction reads the gold patch and instance id from the task's
    ``tests/config.json`` so no network or LLM access is needed at run time.
    """

    SUPPORTS_WINDOWS: bool = False
    logger: logging.Logger

    def __init__(
        self,
        logs_dir: Path,
        task_dir: Path | str | None = None,
        model_name: str | None = None,
        extra_env: dict[str, str] | None = None,
        agent_timeout_sec: float | None = None,
        **kwargs: object,
    ) -> None:
        """Load the gold patch and instance id from the task's config.

        Harbor only auto-injects ``task_dir`` for its built-in ``oracle`` agent,
        so custom import-path agents must be given it explicitly via a Harbor
        agent kwarg (``--ak task_dir=<path>``).

        :param logs_dir: Directory for agent logs (supplied by Harbor).
        :param task_dir: The task directory; pass via ``--ak task_dir=<path>``.
        :param model_name: Unused; accepted for Harbor agent-constructor parity.
        :param extra_env: Extra environment variables from the trial config.
        :param agent_timeout_sec: Per-exec timeout budget, if any.
        :param kwargs: Additional Harbor-supplied constructor arguments.
        :raises ValueError: If ``task_dir`` was not supplied.
        """
        # Harbor forwards framework kwargs (logger, mcp_servers, skills_dir) that
        # BaseAgent accepts via its own **kwargs; ty cannot see through the splat.
        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            extra_env=extra_env,
            **kwargs,  # ty: ignore[invalid-argument-type]
        )
        if task_dir is None:
            message = (
                "task_dir is required; pass it to Harbor with "
                "'--ak task_dir=<path to the task directory>'."
            )
            raise ValueError(message)
        self._task_dir = Path(task_dir)
        self._agent_timeout_sec = agent_timeout_sec
        config = json.loads(
            (self._task_dir / "tests" / "config.json").read_text(encoding="utf-8"),
        )
        self._instance_id: str = config["instance_id"]
        self._gold_patch: str = config["patch"]

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        """No setup is required; patches are applied in :meth:`run`."""
        return

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Apply this condition's edits to the testbed.

        The natural-language ``instruction`` and ``context`` are intentionally
        ignored: these agents are deterministic instruments, not LLM solvers.
        """
        await self._apply(environment)

    @abstractmethod
    async def _apply(self, environment: BaseEnvironment) -> None:
        """Perform the condition-specific testbed edits."""

    async def _exec_or_raise(
        self,
        environment: BaseEnvironment,
        command: str,
        *,
        description: str,
    ) -> None:
        """Run *command* in the environment, raising if it exits non-zero."""
        timeout = int(self._agent_timeout_sec) if self._agent_timeout_sec else None
        result = await environment.exec(
            command=command,
            timeout_sec=timeout,
            user="root",
        )
        if result.return_code != 0:
            message = f"{description} failed with exit code {result.return_code}"
            raise RuntimeError(message)

    async def _apply_gold_patch(self, environment: BaseEnvironment) -> None:
        """Apply the task's reference gold patch to the testbed."""
        patch = self._gold_patch
        if not patch.endswith("\n"):
            patch += "\n"
        write = _shell_write_file(patch, _GOLD_PATCH_PATH)
        command = (
            f"set -euo pipefail; cd {_TESTBED}; {write}; "
            f"git apply -v {_GOLD_PATCH_PATH} "
            f"|| patch --fuzz=5 -p1 -i {_GOLD_PATCH_PATH}"
        )
        await self._exec_or_raise(environment, command, description="gold patch")
        self.logger.info("Applied gold patch for %s", self._instance_id)

    async def _apply_regression(
        self,
        environment: BaseEnvironment,
        regression: Regression,
    ) -> None:
        """Apply a single deterministic source regression to the testbed."""
        script = (
            "import pathlib\n"
            "import sys\n"
            f"path = pathlib.Path({regression.file!r})\n"
            "text = path.read_text(encoding='utf-8')\n"
            f"needle = {regression.find!r}\n"
            "if needle not in text:\n"
            "    sys.exit('saboteur anchor not found in ' + str(path))\n"
            f"path.write_text(text.replace(needle, {regression.replace!r}, 1), "
            "encoding='utf-8')\n"
        )
        write = _shell_write_file(script, _REGRESSION_SCRIPT_PATH)
        command = (
            f"set -euo pipefail; cd {_TESTBED}; {write}; "
            f"python3 {_REGRESSION_SCRIPT_PATH}"
        )
        await self._exec_or_raise(environment, command, description="regression edit")
        self.logger.info(
            "Injected regression targeting %s: %s",
            regression.target_test,
            regression.description,
        )


class GoldPatchAgent(_TestbedPatchAgent):
    """Applies only the gold patch (a self-contained ``oracle`` equivalent)."""

    @staticmethod
    @override
    def name() -> str:
        return "gold-patch"

    @override
    async def _apply(self, environment: BaseEnvironment) -> None:
        await self._apply_gold_patch(environment)


class SaboteurAgent(_TestbedPatchAgent):
    """Applies the gold patch plus one deterministic, documented regression."""

    @staticmethod
    @override
    def name() -> str:
        return "saboteur"

    def regression(self) -> Regression:
        """Return the regression for this instance, or raise if none is defined."""
        regression = REGRESSIONS.get(self._instance_id)
        if regression is None:
            message = f"No saboteur regression for instance {self._instance_id!r}"
            raise KeyError(message)
        return regression

    @override
    async def _apply(self, environment: BaseEnvironment) -> None:
        await self._apply_gold_patch(environment)
        await self._apply_regression(environment, self.regression())
