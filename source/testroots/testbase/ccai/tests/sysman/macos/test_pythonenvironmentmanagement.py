"""
    Tests for :class:`MacosPythonEnvironmentManager`.

    The venv / pip verbs are exercised in depth by the shared base tests
    in ``codecraftai-sysman-common``; these tests cover what the macOS
    subclass adds — POSIX layout selection, the Homebrew prerequisite
    step (including the brew never-elevate rule), and one end-to-end
    deploy on macOS argv.  No live ``brew``, ``python3`` or ``pip`` is
    invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.pythonenvironmentlayout import PosixPythonEnvironmentLayout
from ccai.sysman.common.pythonenvironmentmanagementbase import PythonEnvManagerConfig

from ccai.sysman.macos.packagemanagement import MacosPackageActionStatus
from ccai.sysman.macos.pythonenvironmentmanagement import (
    MacosPythonEnvActionStatus,
    MacosPythonEnvManagerConfig,
    MacosPythonEnvSpec,
    MacosPythonEnvironmentManager,
)


_VENV = "/Users/ops/venvs/app"
_VENV_PYTHON = "/Users/ops/venvs/app/bin/python"
_PIP_PREFIX = [_VENV_PYTHON, "-m", "pip", "--disable-pip-version-check", "--no-input"]
_VERSION_SNIPPET = "import platform; print(platform.python_version())"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in keyed by argv tuple, with an
        optional list of argv prefixes that fail.
    """

    results: dict = field(default_factory=dict)
    fail_prefixes: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        self.calls.append({"args": list(args), "elevated": elevated})

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        for prefix in self.fail_prefixes:
            if tuple(args[:len(prefix)]) == tuple(prefix):
                rtnval = RunResult(exit_code=1, stdout="", stderr="brew failure")
                return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _brew_install_calls(runner: _MockCommandRunner) -> list:
    rtnval = [c for c in runner.calls if "install" in c["args"] and "brew" in c["args"]]
    return rtnval


def _pip_argv(*pip_args: str) -> list:
    rtnval = list(_PIP_PREFIX)
    rtnval.extend(pip_args)
    return rtnval


def test_layout_is_posix():
    """
        The macOS manager selects the POSIX layout.
    """
    manager = MacosPythonEnvironmentManager(runner=_MockCommandRunner())
    testbase.assert_true(isinstance(manager.layout, PosixPythonEnvironmentLayout))
    testbase.assert_equal(manager.layout.venv_python_path(path=_VENV), _VENV_PYTHON)
    testbase.assert_equal(manager.layout.default_interpreter_argv(), ["python3"])
    return


def test_ensure_prerequisites_installs_python_formula_without_elevation():
    """
        The default prerequisite is the ``python`` formula; brew is never
        elevated even when the manager config is elevated.
    """
    runner = _MockCommandRunner()
    manager = MacosPythonEnvironmentManager(
        runner=runner, config=MacosPythonEnvManagerConfig(elevated=True),
    )

    results = manager.ensure_prerequisites()

    testbase.assert_equal([r.status for r in results], [MacosPackageActionStatus.SUCCESS])
    installs = _brew_install_calls(runner)
    testbase.assert_equal([c["args"] for c in installs], [["brew", "install", "python"]])
    testbase.assert_equal(all(c["elevated"] is False for c in runner.calls), True)
    return


def test_ensure_prerequisites_wraps_with_brew_user_when_configured():
    """
        brew_user is forwarded so brew runs as that user via sudo -u.
    """
    runner = _MockCommandRunner()
    config = MacosPythonEnvManagerConfig(brew_user="ops", prerequisite_packages=["python@3.12"])
    manager = MacosPythonEnvironmentManager(runner=runner, config=config)

    manager.ensure_prerequisites()

    installs = _brew_install_calls(runner)
    testbase.assert_equal(len(installs), 1)
    testbase.assert_equal(installs[0]["args"][:3], ["sudo", "-u", "ops"])
    testbase.assert_equal(installs[0]["args"][-3:], ["brew", "install", "python@3.12"])
    return


def test_ensure_prerequisites_honours_override_and_stops_on_failure():
    """
        An explicit formula list is used and a failure aborts the rest.
    """
    runner = _MockCommandRunner(fail_prefixes=[["brew", "install", "python@3.11"]])
    manager = MacosPythonEnvironmentManager(runner=runner)

    results = manager.ensure_prerequisites(packages=["python@3.11", "python@3.12"])

    testbase.assert_equal([r.status for r in results], [MacosPackageActionStatus.FAILED])
    testbase.assert_equal([c["args"] for c in _brew_install_calls(runner)], [["brew", "install", "python@3.11"]])
    return


def test_ensure_prerequisites_dry_run_dispatches_nothing():
    """
        Dry-run yields DRY_RUN with no brew install call.
    """
    runner = _MockCommandRunner()
    manager = MacosPythonEnvironmentManager(
        runner=runner, config=MacosPythonEnvManagerConfig(dry_run=True),
    )

    results = manager.ensure_prerequisites()

    testbase.assert_equal([r.status for r in results], [MacosPackageActionStatus.DRY_RUN])
    testbase.assert_equal(_brew_install_calls(runner), [])
    return


def test_plain_base_config_is_accepted():
    """
        A plain PythonEnvManagerConfig works; macOS-only fields default.
    """
    runner = _MockCommandRunner()
    manager = MacosPythonEnvironmentManager(runner=runner, config=PythonEnvManagerConfig())

    manager.ensure_prerequisites()

    testbase.assert_equal([c["args"] for c in _brew_install_calls(runner)], [["brew", "install", "python"]])
    return


def test_deploy_end_to_end_with_homebrew_interpreter():
    """
        A deploy with a Homebrew base interpreter issues venv, pip
        upgrade and pip install through the runner with elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-d", _VENV)] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(_VENV_PYTHON, "-c", _VERSION_SNIPPET)] = RunResult(exit_code=127, stdout="", stderr="")
    runner.results[tuple(_pip_argv("list", "--format", "json"))] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    config = MacosPythonEnvManagerConfig(
        elevated=True, base_interpreter=["/opt/homebrew/bin/python3"],
    )
    manager = MacosPythonEnvironmentManager(runner=runner, config=config)

    result = manager.deploy(spec=MacosPythonEnvSpec(path=_VENV, packages=["rich"]))

    testbase.assert_equal(result.status, MacosPythonEnvActionStatus.SUCCESS)
    writes = [c["args"] for c in runner.calls if c["elevated"] is True]
    testbase.assert_equal(
        writes,
        [
            ["/opt/homebrew/bin/python3", "-m", "venv", _VENV],
            _pip_argv("install", "--upgrade", "pip"),
            _pip_argv("install", "rich"),
        ],
    )
    return
