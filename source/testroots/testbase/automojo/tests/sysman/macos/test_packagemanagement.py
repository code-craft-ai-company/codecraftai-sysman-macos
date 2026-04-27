"""
    Tests for :class:`MacosPackageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``brew`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.packagemanagement import (
    MacosPackageActionStatus,
    MacosPackageInfo,
    MacosPackageKind,
    MacosPackageManager,
    MacosPackageManagerConfig,
    MacosPackageQueryError,
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


def _brew_list_versions_installed_result(
    *,
    name: str,
    version: str = "1.25.0",
) -> RunResult:
    """
        Build a canonical ``brew list --versions <name>`` result for an
        installed package.
    """
    body = "{} {}\n".format(name, version)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


def _brew_list_versions_missing_result() -> RunResult:
    """
        Build a ``brew list --versions <name>`` result for a package
        that is not installed (``brew`` exits 1 with no stdout).
    """
    rtnval = RunResult(exit_code=1, stdout="", stderr="")
    return rtnval


def _brew_outdated_empty_result() -> RunResult:
    """
        Build a ``brew outdated --json=v2 <name>`` result that reports
        no outdated packages.
    """
    body = '{"formulae": [], "casks": []}\n'
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


def _brew_outdated_has_formula_result(*, name: str) -> RunResult:
    """
        Build a ``brew outdated --json=v2 <name>`` result that reports
        the named formula as outdated.
    """
    body = (
        '{"formulae": [{"name": "' + name + '",'
        ' "installed_versions": ["1.24.0"],'
        ' "current_version": "1.25.0"}],'
        ' "casks": []}\n'
    )
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


def _brew_info_formula_json(*, name: str, version: str = "1.25.0") -> str:
    """
        Build a realistic ``brew info --json=v2`` payload for a formula.
    """
    rtnval = (
        '{"formulae": [{'
        '"name": "' + name + '",'
        ' "versions": {"stable": "' + version + '", "head": null, "bottle": true},'
        ' "installed": [{"version": "' + version + '", '
        '"installed_on_request": true}],'
        ' "homepage": "https://nginx.org/",'
        ' "desc": "HTTP(S) server"'
        '}], "casks": []}'
    )
    return rtnval


# ----------------------------------------------------------------------
# install_package
# ----------------------------------------------------------------------


def test_install_package_invokes_brew_install():
    """
        install_package on a missing package dispatches
        ``brew install``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_missing_result()
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    install_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(
        install_calls[0]["args"],
        ["brew", "install", "nginx"],
    )
    testbase.assert_equal(install_calls[0]["elevated"], False)
    return


def test_install_package_skipped_when_installed():
    """
        install_package returns SKIPPED when ``brew list --versions``
        reports the package is installed.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_installed_result(name="nginx", version="1.25.0")
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SKIPPED)
    testbase.assert_equal(result.version, "1.25.0")
    install_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(install_calls, [])
    return


def test_install_package_with_version_constructs_versioned_formula():
    """
        install_package(name, version) dispatches ``brew install
        name@version``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "list", "--versions", "node")] = (
        _brew_list_versions_missing_result()
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.install_package(name="node", version="18")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    install_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "install"]
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(
        install_calls[0]["args"],
        ["brew", "install", "node@18"],
    )
    return


def test_install_cask_passes_cask_flag():
    """
        install_package with ``kind=CASK`` dispatches
        ``brew install --cask <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "brew", "list", "--versions", "--cask", "firefox",
    )] = _brew_list_versions_missing_result()

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.install_package(
        name="firefox",
        kind=MacosPackageKind.CASK,
    )

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    testbase.assert_equal(result.kind, MacosPackageKind.CASK)
    install_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["brew", "install", "--cask"]
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(
        install_calls[0]["args"],
        ["brew", "install", "--cask", "firefox"],
    )
    return


# ----------------------------------------------------------------------
# remove_package
# ----------------------------------------------------------------------


def test_remove_package_invokes_brew_uninstall():
    """
        remove_package on an installed package dispatches
        ``brew uninstall``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_installed_result(name="nginx")
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.remove_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    uninstall_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "uninstall"]
    ]
    testbase.assert_equal(len(uninstall_calls), 1)
    testbase.assert_equal(
        uninstall_calls[0]["args"],
        ["brew", "uninstall", "nginx"],
    )
    testbase.assert_equal(uninstall_calls[0]["elevated"], False)
    return


def test_remove_package_skipped_when_not_installed():
    """
        remove_package returns SKIPPED when the package is absent.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "ghost")] = (
        _brew_list_versions_missing_result()
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.remove_package(name="ghost")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SKIPPED)
    uninstall_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "uninstall"]
    ]
    testbase.assert_equal(uninstall_calls, [])
    return


# ----------------------------------------------------------------------
# upgrade_package / upgrade_all
# ----------------------------------------------------------------------


def test_upgrade_package_invokes_brew_upgrade():
    """
        upgrade_package dispatches ``brew upgrade <name>`` when the
        package is installed and outdated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_installed_result(name="nginx", version="1.24.0")
    )
    runner.results[(
        "brew", "outdated", "--json=v2", "nginx",
    )] = _brew_outdated_has_formula_result(name="nginx")

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    upgrade_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "upgrade"] and len(c["args"]) == 3
    ]
    testbase.assert_equal(len(upgrade_calls), 1)
    testbase.assert_equal(
        upgrade_calls[0]["args"],
        ["brew", "upgrade", "nginx"],
    )
    return


def test_upgrade_package_skipped_when_not_outdated():
    """
        upgrade_package returns SKIPPED when ``brew outdated`` reports
        no available upgrade.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_installed_result(name="nginx", version="1.25.0")
    )
    runner.results[(
        "brew", "outdated", "--json=v2", "nginx",
    )] = _brew_outdated_empty_result()

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SKIPPED)
    upgrade_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["brew", "upgrade"] and len(c["args"]) == 3
    ]
    testbase.assert_equal(upgrade_calls, [])
    return


def test_upgrade_all_invokes_brew_upgrade_all():
    """
        upgrade_all dispatches a bare ``brew upgrade``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.upgrade_all()

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["brew", "upgrade"])
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# is_installed / get_package_info / list_installed
# ----------------------------------------------------------------------


def test_is_installed_returns_true_for_installed():
    """
        is_installed returns True when ``brew list --versions`` returns
        non-empty stdout at exit 0.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_installed_result(name="nginx")
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.is_installed(name="nginx")
    testbase.assert_true(result)
    return


def test_is_installed_returns_false_for_not_installed():
    """
        is_installed returns False when ``brew list --versions`` exits
        non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "ghost")] = (
        _brew_list_versions_missing_result()
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.is_installed(name="ghost")
    testbase.assert_false(result)
    return


def test_get_package_info_parses_brew_info_json():
    """
        get_package_info parses ``brew info --json=v2`` into a
        :class:`MacosPackageInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "info", "--json=v2", "nginx")] = RunResult(
        exit_code=0,
        stdout=_brew_info_formula_json(name="nginx", version="1.25.0"),
        stderr="",
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    info = manager.get_package_info(name="nginx")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "nginx")
    testbase.assert_equal(info.version, "1.25.0")
    testbase.assert_equal(info.kind, MacosPackageKind.FORMULA)
    testbase.assert_equal(info.installed_on_request, True)
    testbase.assert_equal(info.homepage, "https://nginx.org/")
    testbase.assert_equal(info.description, "HTTP(S) server")
    return


def test_get_package_info_returns_none_for_unknown_formula():
    """
        get_package_info returns ``None`` when ``brew`` reports the
        formula is unknown.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "info", "--json=v2", "ghost")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Error: No available formula with the name \"ghost\".",
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    info = manager.get_package_info(name="ghost")
    testbase.assert_equal(info, None)
    return


def test_list_installed_parses_brew_list_versions():
    """
        list_installed parses ``brew list --versions`` and merges the
        formula + cask listings.
    """
    formula_body = (
        "nginx 1.25.0\n"
        "jq 1.7\n"
        "node 20.1.0\n"
    )
    cask_body = (
        "firefox 124.0\n"
    )
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions")] = RunResult(
        exit_code=0,
        stdout=formula_body,
        stderr="",
    )
    runner.results[("brew", "list", "--cask", "--versions")] = RunResult(
        exit_code=0,
        stdout=cask_body,
        stderr="",
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    rows = manager.list_installed()

    testbase.assert_equal(len(rows), 4)

    nginx_row: Optional[MacosPackageInfo] = None
    firefox_row: Optional[MacosPackageInfo] = None
    for row in rows:
        if row.name == "nginx":
            nginx_row = row
        if row.name == "firefox":
            firefox_row = row

    testbase.assert_true(nginx_row is not None)
    testbase.assert_equal(nginx_row.version, "1.25.0")
    testbase.assert_equal(nginx_row.kind, MacosPackageKind.FORMULA)

    testbase.assert_true(firefox_row is not None)
    testbase.assert_equal(firefox_row.version, "124.0")
    testbase.assert_equal(firefox_row.kind, MacosPackageKind.CASK)
    return


# ----------------------------------------------------------------------
# refresh_metadata
# ----------------------------------------------------------------------


def test_refresh_metadata_invokes_brew_update():
    """
        refresh_metadata dispatches ``brew update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.refresh_metadata()

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["brew", "update"])
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_install():
    """
        Dry-run short-circuits install_package without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(dry_run=True),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["brew", "install", "nginx"],
    )
    return


def test_brew_never_elevates_even_when_config_elevated():
    """
        Homebrew refuses to run as root; every ``brew`` invocation
        must be dispatched with ``elevated=False`` even when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_missing_result()
    )
    runner.results[("brew", "list", "--versions", "curl")] = (
        _brew_list_versions_installed_result(name="curl")
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(elevated=True),
    )
    manager.install_package(name="nginx")
    manager.remove_package(name="curl")
    manager.upgrade_all()
    manager.refresh_metadata()
    manager.is_installed(name="nginx")

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
        "sudo", "-u", "brew_user", "--",
        "brew", "list", "--versions", "nginx",
    )] = _brew_list_versions_missing_result()

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(brew_user="brew_user"),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.SUCCESS)
    install_calls = [
        c for c in runner.calls
        if "install" in c["args"] and "brew" in c["args"]
    ]
    testbase.assert_equal(len(install_calls), 1)
    testbase.assert_equal(
        install_calls[0]["args"],
        ["sudo", "-u", "brew_user", "--", "brew", "install", "nginx"],
    )
    # And still elevated=False, because ``sudo -u`` is NOT elevation.
    testbase.assert_equal(install_calls[0]["elevated"], False)
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions", "nginx")] = (
        _brew_list_versions_missing_result()
    )
    runner.results[("brew", "install", "nginx")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Error: nginx: no bottle available!",
    )

    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, MacosPackageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr,
        "Error: nginx: no bottle available!",
    )
    testbase.assert_equal(
        result.command,
        ["brew", "install", "nginx"],
    )
    return


def test_list_installed_raises_on_formula_listing_failure():
    """
        list_installed raises :class:`MacosPackageQueryError` when
        ``brew list --versions`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("brew", "list", "--versions")] = RunResult(
        exit_code=2, stdout="", stderr="boom",
    )
    manager = MacosPackageManager(
        runner=runner,
        config=MacosPackageManagerConfig(),
    )
    raised = False
    try:
        manager.list_installed()
    except MacosPackageQueryError:
        raised = True
    testbase.assert_true(raised)
    return
