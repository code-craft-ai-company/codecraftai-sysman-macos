"""
    Tests for :class:`MacosEnvironmentConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``launchctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.environmentconfiguration import (
    MacosEnvActionStatus,
    MacosEnvironmentConfigurationManager,
    MacosEnvironmentConfigurationManagerConfig,
    MacosEnvScope,
    MacosEnvValue,
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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _seed_user_resolution(
    *,
    runner: _MockCommandRunner,
    user: str,
    uid: int,
    home_dir: str,
) -> None:
    """
        Seed the mock runner with successful ``id -u`` and
        ``dscl . -read /Users/<user>`` results.
    """
    runner.results[("id", "-u", user)] = RunResult(
        exit_code=0, stdout="{}\n".format(uid), stderr="",
    )
    runner.results[(
        "dscl", ".", "-read",
        "/Users/{}".format(user), "NFSHomeDirectory",
    )] = RunResult(
        exit_code=0,
        stdout="NFSHomeDirectory: {}\n".format(home_dir),
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# set_system_env
# ----------------------------------------------------------------------


def test_set_system_env_invokes_launchctl_and_writes_zshenv():
    """
        set_system_env dispatches launchctl setenv AND writes the
        sentinel block into /etc/zshenv.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="cat: no such file",
    )
    runner.results[("launchctl", "setenv", "FOO", "bar")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("tee", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_system_env(name="FOO", value="bar")

    testbase.assert_equal(result.status, MacosEnvActionStatus.SUCCESS)
    setenv_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "setenv"]
    ]
    testbase.assert_equal(len(setenv_calls), 1)
    testbase.assert_equal(setenv_calls[0]["elevated"], True)

    tee_calls = [
        c for c in runner.calls
        if c["args"] == ["tee", "/etc/zshenv"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true("# --- ccai system env start ---" in payload)
    testbase.assert_true("# --- ccai system env end ---" in payload)
    testbase.assert_true('export FOO="bar"' in payload)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_set_system_env_skipped_when_both_sources_match():
    """
        set_system_env returns SKIPPED when launchctl AND zshenv
        already report the desired value.
    """
    existing = (
        "# --- ccai system env start ---\n"
        'export FOO="bar"\n'
        "# --- ccai system env end ---\n"
    )
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout=existing, stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.set_system_env(name="FOO", value="bar")

    testbase.assert_equal(result.status, MacosEnvActionStatus.SKIPPED)
    setenv_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "setenv"]
    ]
    testbase.assert_equal(setenv_calls, [])
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_set_system_env_dry_run_no_side_effects():
    """
        Dry-run short-circuits set_system_env without invoking
        launchctl setenv or tee.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(dry_run=True),
    )
    result = manager.set_system_env(name="FOO", value="bar")

    testbase.assert_equal(result.status, MacosEnvActionStatus.DRY_RUN)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "setenv"]
        or c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# unset_system_env
# ----------------------------------------------------------------------


def test_unset_system_env_idempotency_when_absent():
    """
        unset_system_env returns SKIPPED when the variable is missing
        from runtime and the file does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_system_env(name="FOO")
    testbase.assert_equal(result.status, MacosEnvActionStatus.SKIPPED)
    return


def test_unset_system_env_invokes_launchctl_and_rewrites_zshenv():
    """
        unset_system_env dispatches launchctl unsetenv AND rewrites
        /etc/zshenv with the target line removed.
    """
    existing = (
        "# --- ccai system env start ---\n"
        'export FOO="bar"\n'
        'export OTHER="keep"\n'
        "# --- ccai system env end ---\n"
    )
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout=existing, stderr="",
    )
    runner.results[("launchctl", "unsetenv", "FOO")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("tee", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(elevated=True),
    )
    result = manager.unset_system_env(name="FOO")

    testbase.assert_equal(result.status, MacosEnvActionStatus.SUCCESS)
    unsetenv_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "unsetenv"]
    ]
    testbase.assert_equal(len(unsetenv_calls), 1)
    testbase.assert_equal(unsetenv_calls[0]["elevated"], True)

    tee_calls = [
        c for c in runner.calls
        if c["args"] == ["tee", "/etc/zshenv"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true('export FOO="bar"' not in payload)
    testbase.assert_true('export OTHER="keep"' in payload)
    return


def test_unset_system_env_deletes_file_when_block_emptied():
    """
        When removing the last entry empties the entire file, the
        manager dispatches ``rm -f`` rather than leaving an empty
        file behind.
    """
    existing = (
        "# --- ccai system env start ---\n"
        'export FOO="bar"\n'
        "# --- ccai system env end ---\n"
    )
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout=existing, stderr="",
    )
    runner.results[("launchctl", "unsetenv", "FOO")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("rm", "-f", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_system_env(name="FOO")

    testbase.assert_equal(result.status, MacosEnvActionStatus.SUCCESS)
    rm_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["rm"]
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(rm_calls[0]["args"], ["rm", "-f", "/etc/zshenv"])
    return


# ----------------------------------------------------------------------
# Read verbs
# ----------------------------------------------------------------------


def test_get_system_env_runtime_first():
    """
        get_system_env returns the launchctl runtime value when
        present, regardless of the file contents.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.get_system_env(name="FOO")

    testbase.assert_true(result is not None)
    testbase.assert_equal(result.value, "bar")
    testbase.assert_equal(result.scope, MacosEnvScope.SYSTEM)
    testbase.assert_equal(result.source, "launchctl")
    cat_calls = [c for c in runner.calls if c["args"][:1] == ["cat"]]
    testbase.assert_equal(cat_calls, [])
    return


def test_get_system_env_falls_back_to_zshenv():
    """
        get_system_env falls back to the sentinel block when launchctl
        reports the variable is unset.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=0,
        stdout=(
            "# --- ccai system env start ---\n"
            'export FOO="from-file"\n'
            "# --- ccai system env end ---\n"
        ),
        stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.get_system_env(name="FOO")

    testbase.assert_true(result is not None)
    testbase.assert_equal(result.value, "from-file")
    testbase.assert_equal(result.source, "/etc/zshenv")
    return


def test_list_system_env_parses_block_only():
    """
        list_system_env returns the keys defined within the sentinel
        block and ignores user-authored exports outside it.
    """
    body = (
        'export OUTSIDE="ignored"\n'
        "# --- ccai system env start ---\n"
        'export FOO="bar"\n'
        'export BAZ="qux"\n'
        "# --- ccai system env end ---\n"
    )
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.list_system_env()

    testbase.assert_equal(result["FOO"], "bar")
    testbase.assert_equal(result["BAZ"], "qux")
    testbase.assert_true("OUTSIDE" not in result)
    return


# ----------------------------------------------------------------------
# User-scope verbs
# ----------------------------------------------------------------------


def test_set_user_env_failed_for_unknown_user():
    """
        set_user_env returns FAILED when ``id -u`` reports the user is
        unknown.
    """
    runner = _MockCommandRunner()
    runner.results[("id", "-u", "ghost")] = RunResult(
        exit_code=1, stdout="", stderr="id: ghost: no such user",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.set_user_env(user="ghost", name="FOO", value="bar")
    testbase.assert_equal(result.status, MacosEnvActionStatus.FAILED)
    testbase.assert_true("user not found" in result.message)
    return


def test_set_user_env_invokes_launchctl_asuser_and_writes_zshenv():
    """
        set_user_env dispatches launchctl asuser <uid> launchctl setenv
        AND writes the sentinel block into ~<user>/.zshenv WITHOUT
        elevation.
    """
    runner = _MockCommandRunner()
    _seed_user_resolution(
        runner=runner, user="bob", uid=501, home_dir="/Users/bob",
    )
    runner.results[(
        "launchctl", "asuser", "501", "launchctl", "getenv", "FOO",
    )] = RunResult(exit_code=0, stdout="\n", stderr="")
    runner.results[("cat", "/Users/bob/.zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="cat: no such file",
    )
    runner.results[(
        "launchctl", "asuser", "501", "launchctl", "setenv", "FOO", "bar",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[("tee", "/Users/bob/.zshenv")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        # USER scope must NOT elevate even when the config is elevated.
        config=MacosEnvironmentConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_user_env(user="bob", name="FOO", value="bar")

    testbase.assert_equal(result.status, MacosEnvActionStatus.SUCCESS)

    asuser_setenv_calls = [
        c for c in runner.calls
        if c["args"][:5] == [
            "launchctl", "asuser", "501", "launchctl", "setenv",
        ]
    ]
    testbase.assert_equal(len(asuser_setenv_calls), 1)
    testbase.assert_equal(asuser_setenv_calls[0]["elevated"], False)

    tee_calls = [
        c for c in runner.calls
        if c["args"] == ["tee", "/Users/bob/.zshenv"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], False)
    return


def test_unset_user_env_failed_for_unknown_user():
    """
        unset_user_env returns FAILED when ``id -u`` reports the user
        is unknown.
    """
    runner = _MockCommandRunner()
    runner.results[("id", "-u", "ghost")] = RunResult(
        exit_code=1, stdout="", stderr="id: ghost: no such user",
    )
    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_user_env(user="ghost", name="FOO")
    testbase.assert_equal(result.status, MacosEnvActionStatus.FAILED)
    return


def test_unset_user_env_idempotent_when_absent():
    """
        unset_user_env returns SKIPPED when the variable is absent
        from runtime and the user file does not exist.
    """
    runner = _MockCommandRunner()
    _seed_user_resolution(
        runner=runner, user="bob", uid=501, home_dir="/Users/bob",
    )
    runner.results[(
        "launchctl", "asuser", "501", "launchctl", "getenv", "FOO",
    )] = RunResult(exit_code=0, stdout="\n", stderr="")
    runner.results[("cat", "/Users/bob/.zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.unset_user_env(user="bob", name="FOO")
    testbase.assert_equal(result.status, MacosEnvActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# Elevation / error surfaces
# ----------------------------------------------------------------------


def test_reads_never_elevate_even_when_config_elevated():
    """
        Reads (``launchctl getenv``, ``cat``, ``id``) MUST NOT elevate
        even when config.elevated is True.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="bar\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(elevated=True),
    )
    manager.get_system_env(name="FOO")
    manager.list_system_env()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on launchctl setenv maps to FAILED with the
        captured stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "getenv", "FOO")] = RunResult(
        exit_code=0, stdout="\n", stderr="",
    )
    runner.results[("cat", "/etc/zshenv")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("launchctl", "setenv", "FOO", "bar")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="launchctl: setenv permission denied",
    )

    manager = MacosEnvironmentConfigurationManager(
        runner=runner,
        config=MacosEnvironmentConfigurationManagerConfig(),
    )
    result = manager.set_system_env(name="FOO", value="bar")

    testbase.assert_equal(result.status, MacosEnvActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in result.stderr)
    testbase.assert_equal(result.message, "launchctl_setenv_failed")
    return
