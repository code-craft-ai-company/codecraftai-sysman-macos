"""
    Tests for :class:`MacosPrecisionTimeManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``systemsetup`` / ``sntp`` / ``defaults`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.precisiontimemanagement import (
    MacosPrecisionTimeActionStatus,
    MacosPrecisionTimeManager,
    MacosPrecisionTimeManagerConfig,
    MacosPrecisionTimeQueryError,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
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


def _using_on_payload() -> str:
    """
        Build the ``Network Time: On`` payload.
    """
    rtnval = "Network Time: On\n"
    return rtnval


def _using_off_payload() -> str:
    """
        Build the ``Network Time: Off`` payload.
    """
    rtnval = "Network Time: Off\n"
    return rtnval


def _server_payload(*, server: str) -> str:
    """
        Build a ``Network Time Server: <server>`` payload.
    """
    rtnval = "Network Time Server: {}\n".format(server)
    return rtnval


def _defaults_payload(*, servers: list[str]) -> str:
    """
        Build a synthetic ``defaults read`` plist payload.
    """
    lines = ["{"]
    lines.append("    TimeServers =     (")
    for idx, server in enumerate(servers):
        suffix = "," if idx < len(servers) - 1 else ""
        lines.append('        "{}"{}'.format(server, suffix))
    lines.append("    );")
    lines.append("}")
    body = "\n".join(lines) + "\n"
    return body


def _sntp_payload() -> str:
    """
        Build a synthetic ``sntp <server>`` offset payload.
    """
    rtnval = (
        "+0.001234 +/- 0.005000s ntp1.example.com 192.0.2.10\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_get_time_status_parses_systemsetup_output():
    """
        get_time_status combines ``-getusingnetworktime`` and
        ``-getnetworktimeserver``.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_on_payload(), stderr="",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.synchronized, True)
    testbase.assert_equal(status.current_server, "time.apple.com")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    testbase.assert_equal(runner.calls[1]["elevated"], False)
    return


def test_get_time_status_returns_disabled_when_off():
    """
        get_time_status reports ``enabled=False`` when network time is
        off.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_off_payload(), stderr="",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout="Network Time Server: \n",
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, False)
    testbase.assert_equal(status.synchronized, False)
    testbase.assert_equal(status.current_server, None)
    return


def test_get_time_sources_parses_systemsetup_output():
    """
        get_time_sources falls back to the systemsetup primary when
        ``defaults`` is unavailable.
    """
    runner = _MockCommandRunner()
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=1, stdout="",
        stderr="The domain/default pair does not exist",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    sources = manager.get_time_sources()

    testbase.assert_equal(len(sources), 1)
    testbase.assert_equal(sources[0].server, "time.apple.com")
    testbase.assert_equal(sources[0].state, "selected")
    return


def test_get_time_sources_parses_defaults_output():
    """
        get_time_sources prefers the ``defaults`` payload when
        present.
    """
    runner = _MockCommandRunner()
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(
            servers=["time.apple.com", "ntp.example.com"],
        ),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    sources = manager.get_time_sources()

    testbase.assert_equal(len(sources), 2)
    by_server = {item.server: item for item in sources}
    testbase.assert_equal(by_server["time.apple.com"].state, "selected")
    testbase.assert_equal(by_server["ntp.example.com"].state, "candidate")
    return


def test_get_drift_parses_sntp_output():
    """
        get_drift parses ``sntp`` offset / ``+/-`` payload.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )
    runner.results[("sntp", "time.apple.com")] = RunResult(
        exit_code=0, stdout=_sntp_payload(), stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    drift = manager.get_drift()

    testbase.assert_equal(
        abs(drift.offset_seconds - 0.001234) < 1e-9, True,
    )
    testbase.assert_equal(
        abs((drift.jitter_seconds or 0.0) - 0.005000) < 1e-9, True,
    )
    return


# ----------------------------------------------------------------------
# add_time_source
# ----------------------------------------------------------------------


def test_add_time_source_invokes_systemsetup():
    """
        add_time_source dispatches
        ``systemsetup -setnetworktimeserver <list>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=1, stdout="",
        stderr="The domain/default pair does not exist",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.add_time_source(server="ntp.example.com")

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setnetworktimeserver"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["systemsetup", "-setnetworktimeserver",
         "time.apple.com ntp.example.com"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_add_time_source_skipped_when_present():
    """
        add_time_source returns SKIPPED when the server is already in
        the configured list.
    """
    runner = _MockCommandRunner()
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(servers=["time.apple.com"]),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    result = manager.add_time_source(server="time.apple.com")

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setnetworktimeserver"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_add_time_source_dry_run():
    """
        add_time_source returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(dry_run=True),
    )
    result = manager.add_time_source(server="ntp.example.com")

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["systemsetup", "-setnetworktimeserver", "ntp.example.com"],
    )
    return


# ----------------------------------------------------------------------
# remove_time_source
# ----------------------------------------------------------------------


def test_remove_time_source_invokes_systemsetup():
    """
        remove_time_source dispatches ``systemsetup`` with the trimmed
        list.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(
            servers=["time.apple.com", "ntp.example.com"],
        ),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.remove_time_source(server="time.apple.com")

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setnetworktimeserver"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["systemsetup", "-setnetworktimeserver", "ntp.example.com"],
    )
    return


def test_remove_time_source_skipped_when_absent():
    """
        remove_time_source returns SKIPPED when the server is absent.
    """
    runner = _MockCommandRunner()
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(servers=["time.apple.com"]),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    result = manager.remove_time_source(server="ntp.example.com")

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setnetworktimeserver"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# set_time_servers
# ----------------------------------------------------------------------


def test_set_time_servers_invokes_systemsetup():
    """
        set_time_servers dispatches systemsetup with a space-joined
        list.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(servers=["old.example.com"]),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.set_time_servers(
        servers=["a.example.com", "b.example.com"],
    )

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setnetworktimeserver"]
    ]
    testbase.assert_equal(
        write_calls[0]["args"],
        ["systemsetup", "-setnetworktimeserver",
         "a.example.com b.example.com"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_time_servers_skipped_when_match():
    """
        set_time_servers returns SKIPPED on order-insensitive match.
    """
    runner = _MockCommandRunner()
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=0,
        stdout=_defaults_payload(
            servers=["a.example.com", "b.example.com"],
        ),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    result = manager.set_time_servers(
        servers=["b.example.com", "a.example.com"],
    )

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# force_sync_now
# ----------------------------------------------------------------------


def test_force_sync_now_invokes_sntp():
    """
        force_sync_now dispatches ``sntp -sS <current_server>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.force_sync_now()

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SUCCESS)
    sntp_calls = [c for c in runner.calls if c["args"][0] == "sntp"]
    testbase.assert_equal(len(sntp_calls), 1)
    testbase.assert_equal(
        sntp_calls[0]["args"], ["sntp", "-sS", "time.apple.com"],
    )
    testbase.assert_equal(sntp_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# enable_sync / disable_sync
# ----------------------------------------------------------------------


def test_enable_sync_skipped_when_already_enabled():
    """
        enable_sync returns SKIPPED when network time is already on.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_on_payload(), stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    result = manager.enable_sync()

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setusingnetworktime"]
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_disable_sync_invokes_systemsetup():
    """
        disable_sync dispatches
        ``systemsetup -setusingnetworktime off``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_on_payload(), stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.disable_sync()

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-setusingnetworktime"]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        set_calls[0]["args"],
        ["systemsetup", "-setusingnetworktime", "off"],
    )
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_off_payload(), stderr="",
    )
    defaults_argv = (
        "defaults", "read",
        "/Library/Preferences/com.apple.timed",
        "timeServiceConfig",
    )
    runner.results[defaults_argv] = RunResult(
        exit_code=1, stdout="",
        stderr="The domain/default pair does not exist",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    manager.add_time_source(server="ntp.example.com")
    manager.enable_sync()
    manager.force_sync_now()

    for call in runner.calls:
        head = call["args"]
        if head[:2] == ["systemsetup", "-getusingnetworktime"]:
            testbase.assert_equal(call["elevated"], False)
            continue
        if head[:2] == ["systemsetup", "-getnetworktimeserver"]:
            testbase.assert_equal(call["elevated"], False)
            continue
        if head[0] == "defaults":
            testbase.assert_equal(call["elevated"], False)
            continue
        if head[:2] == ["systemsetup", "-setnetworktimeserver"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[:2] == ["systemsetup", "-setusingnetworktime"]:
            testbase.assert_equal(call["elevated"], True)
            continue
        if head[0] == "sntp":
            testbase.assert_equal(call["elevated"], True)
            continue
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_on_payload(), stderr="",
    )
    runner.results[("systemsetup", "-getnetworktimeserver")] = RunResult(
        exit_code=0,
        stdout=_server_payload(server="time.apple.com"),
        stderr="",
    )
    runner.results[("sntp", "time.apple.com")] = RunResult(
        exit_code=0, stdout=_sntp_payload(), stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=True),
    )
    manager.get_time_status()
    manager.get_time_sources()
    manager.get_drift()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("systemsetup", "-setusingnetworktime", "on")
    ] = RunResult(
        exit_code=2, stdout="",
        stderr="setNetworkTimeServer: requires root privileges",
    )
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=0, stdout=_using_off_payload(), stderr="",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(elevated=False),
    )
    result = manager.enable_sync()

    testbase.assert_equal(result.status, MacosPrecisionTimeActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        result.stderr, "setNetworkTimeServer: requires root privileges",
    )
    testbase.assert_equal(
        result.command,
        ["systemsetup", "-setusingnetworktime", "on"],
    )
    return


def test_get_time_status_raises_on_systemsetup_failure():
    """
        get_time_status raises when ``systemsetup`` fails.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-getusingnetworktime")] = RunResult(
        exit_code=1, stdout="",
        stderr="systemsetup: command not found",
    )

    manager = MacosPrecisionTimeManager(
        runner=runner,
        config=MacosPrecisionTimeManagerConfig(),
    )
    raised = False
    try:
        manager.get_time_status()
    except MacosPrecisionTimeQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
