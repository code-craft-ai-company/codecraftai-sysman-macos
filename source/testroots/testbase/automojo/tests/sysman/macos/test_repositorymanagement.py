"""
    Tests for :class:`MacosRepositoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``brew`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.repositorymanagement import (
    MacosRepositoryActionStatus,
    MacosRepositoryInfo,
    MacosRepositoryManager,
    MacosRepositoryManagerConfig,
    MacosRepositoryQueryError,
    MacosRepositorySpec,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
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
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _brew_tap_list_result(*, names: list) -> RunResult:
    """
        Build a ``brew tap`` result whose stdout lists ``names``, one
        per line.
    """
    body = "".join("{}\n".format(n) for n in names)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# add_repository
# ----------------------------------------------------------------------


def test_add_repository_invokes_brew_tap():
    """
        add_repository on a missing tap dispatches ``brew tap <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["homebrew/core"],
    )

    spec = MacosRepositorySpec(name="hashicorp/tap")

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.add_repository(spec=spec)

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SUCCESS)
    tap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "tap"] and len(c["args"]) > 2
    ]
    testbase.assert_equal(len(tap_calls), 1)
    testbase.assert_equal(
        tap_calls[0]["args"],
        ["brew", "tap", "hashicorp/tap"],
    )
    testbase.assert_equal(tap_calls[0]["elevated"], False)
    return


def test_add_repository_with_url_passes_url():
    """
        add_repository with ``spec.url`` dispatches
        ``brew tap <name> <url>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "tap")] = _brew_tap_list_result(names=[])

    spec = MacosRepositorySpec(
        name="myorg/tools",
        url="https://git.example.com/myorg/homebrew-tools.git",
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.add_repository(spec=spec)

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SUCCESS)
    testbase.assert_equal(result.url, spec.url)
    tap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "tap"] and len(c["args"]) >= 4
    ]
    testbase.assert_equal(len(tap_calls), 1)
    testbase.assert_equal(
        tap_calls[0]["args"],
        [
            "brew",
            "tap",
            "myorg/tools",
            "https://git.example.com/myorg/homebrew-tools.git",
        ],
    )
    return


def test_add_repository_skipped_when_already_tapped():
    """
        add_repository returns SKIPPED when the tap is already
        present in ``brew tap`` output.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["homebrew/core", "hashicorp/tap"],
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.add_repository(
        spec=MacosRepositorySpec(name="hashicorp/tap"),
    )

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SKIPPED)
    tap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "tap"] and len(c["args"]) > 2
    ]
    testbase.assert_equal(tap_calls, [])
    return


# ----------------------------------------------------------------------
# remove_repository
# ----------------------------------------------------------------------


def test_remove_repository_invokes_brew_untap():
    """
        remove_repository on a present tap dispatches ``brew untap``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["hashicorp/tap"],
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.remove_repository(name="hashicorp/tap")

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SUCCESS)
    untap_calls = [
        c for c in runner.calls if c["args"][:2] == ["brew", "untap"]
    ]
    testbase.assert_equal(len(untap_calls), 1)
    testbase.assert_equal(
        untap_calls[0]["args"],
        ["brew", "untap", "hashicorp/tap"],
    )
    testbase.assert_equal(untap_calls[0]["elevated"], False)
    return


def test_remove_repository_skipped_when_not_tapped():
    """
        remove_repository returns SKIPPED when the tap is not listed
        in ``brew tap``.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["homebrew/core"],
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.remove_repository(name="hashicorp/tap")

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SKIPPED)
    untap_calls = [
        c for c in runner.calls if c["args"][:2] == ["brew", "untap"]
    ]
    testbase.assert_equal(untap_calls, [])
    return


# ----------------------------------------------------------------------
# enable / disable (N/A on Homebrew)
# ----------------------------------------------------------------------


def test_enable_repository_returns_skipped_with_message():
    """
        enable_repository is a no-op on Homebrew; it returns SKIPPED
        with an explanatory message and does not invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.enable_repository(name="homebrew/cask")

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SKIPPED)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("no-op" in result.message)
    return


def test_disable_repository_returns_failed_with_message():
    """
        disable_repository returns FAILED on Homebrew because the only
        way to stop consulting a tap is to untap it.
    """
    runner = _MockCommandRunner()
    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.disable_repository(name="homebrew/cask")

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("homebrew_no_disable" in result.message)
    return


# ----------------------------------------------------------------------
# list_repositories / refresh_repositories
# ----------------------------------------------------------------------


def test_list_repositories_parses_brew_tap_output():
    """
        list_repositories parses ``brew tap`` into
        :class:`MacosRepositoryInfo` rows.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["homebrew/cask", "homebrew/core", "hashicorp/tap"],
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    rows = manager.list_repositories()

    testbase.assert_equal(len(rows), 3)
    by_name = {info.name: info for info in rows}

    testbase.assert_true("homebrew/core" in by_name)
    homebrew_core = by_name["homebrew/core"]
    testbase.assert_equal(homebrew_core.url, None)
    testbase.assert_equal(homebrew_core.is_cask_only, False)

    testbase.assert_true("hashicorp/tap" in by_name)
    return


def test_list_repositories_raises_on_runner_failure():
    """
        list_repositories raises :class:`MacosRepositoryQueryError`
        on non-zero exit.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "tap")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="brew tap failed",
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    raised = False
    try:
        manager.list_repositories()
    except MacosRepositoryQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_refresh_repositories_invokes_brew_update():
    """
        refresh_repositories dispatches ``brew update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.refresh_repositories()

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["brew", "update"])
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_add():
    """
        Dry-run short-circuits add_repository without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(dry_run=True),
    )
    result = manager.add_repository(
        spec=MacosRepositorySpec(name="hashicorp/tap"),
    )

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["brew", "tap", "hashicorp/tap"],
    )
    return


def test_brew_never_elevates_even_when_config_elevated():
    """
        Homebrew refuses to run as root; every brew invocation must be
        dispatched with ``elevated=False`` even when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "tap")] = _brew_tap_list_result(
        names=["homebrew/core"],
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(elevated=True),
    )
    manager.add_repository(spec=MacosRepositorySpec(name="hashicorp/tap"))
    manager.remove_repository(name="homebrew/core")
    manager.refresh_repositories()
    manager.list_repositories()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_brew_user_wraps_with_sudo_u():
    """
        When ``config.brew_user`` is set, every brew argv is wrapped
        with ``sudo -u <brew_user> --``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "sudo", "-u", "brew_user", "--", "brew", "tap",
    )] = _brew_tap_list_result(names=[])

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(brew_user="brew_user"),
    )
    result = manager.add_repository(
        spec=MacosRepositorySpec(name="hashicorp/tap"),
    )

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.SUCCESS)
    tap_calls = [
        c for c in runner.calls
        if c["args"][:5] == [
            "sudo", "-u", "brew_user", "--", "brew",
        ] and len(c["args"]) > 5 and c["args"][5] == "tap"
        and len(c["args"]) >= 7
    ]
    testbase.assert_equal(len(tap_calls), 1)
    testbase.assert_equal(
        tap_calls[0]["args"],
        [
            "sudo", "-u", "brew_user", "--",
            "brew", "tap", "hashicorp/tap",
        ],
    )
    testbase.assert_equal(tap_calls[0]["elevated"], False)
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "tap")] = _brew_tap_list_result(names=[])
    runner.results[("brew", "tap", "hashicorp/tap")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Error: invalid tap",
    )

    manager = MacosRepositoryManager(
        runner=runner,
        config=MacosRepositoryManagerConfig(),
    )
    result = manager.add_repository(
        spec=MacosRepositorySpec(name="hashicorp/tap"),
    )

    testbase.assert_equal(result.status, MacosRepositoryActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Error: invalid tap")
    testbase.assert_equal(
        result.command,
        ["brew", "tap", "hashicorp/tap"],
    )
    return
