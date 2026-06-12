"""
    Tests for :class:`MacosServiceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live ``launchctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.servicemanagement import (
    MacosServiceActionStatus,
    MacosServiceInfo,
    MacosServiceManager,
    MacosServiceManagerConfig,
    MacosServiceQueryError,
    MacosServiceScope,
    MacosServiceStatus,
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

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _print_output(
    *,
    label: str,
    state: str = "running",
    pid: int = 0,
    program: str = "/usr/bin/example",
    last_exit_code: int = 0,
    disabled: str = "false",
    extra_lines: Optional[list] = None,
) -> str:
    """
        Build a representative ``launchctl print <domain>/<label>``
        output block.
    """
    lines = ["{} = {{".format(label)]
    lines.append("\tactive count = 1")
    lines.append("\tpath = /Library/LaunchDaemons/{}.plist".format(label))
    lines.append("\ttype = LaunchDaemon")
    lines.append("\tstate = {}".format(state))
    lines.append("\tprogram = {}".format(program))
    if pid != 0:
        lines.append("\tpid = {}".format(pid))
    lines.append("\tlast exit code = {}".format(last_exit_code))
    lines.append("\tdisabled = {}".format(disabled))
    if extra_lines is not None:
        for raw in extra_lines:
            lines.append(raw)
    lines.append("}")
    rtnval = "\n".join(lines) + "\n"
    return rtnval


def _print_result(
    *,
    label: str,
    state: str = "running",
    pid: int = 0,
    program: str = "/usr/bin/example",
    last_exit_code: int = 0,
    disabled: str = "false",
) -> RunResult:
    """
        Build a canonical ``launchctl print`` :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=_print_output(
            label=label,
            state=state,
            pid=pid,
            program=program,
            last_exit_code=last_exit_code,
            disabled=disabled,
        ),
        stderr="",
    )
    return rtnval


def _seed_service_state(
    *,
    runner: _MockCommandRunner,
    name: str,
    domain: str,
    state: str = "running",
    disabled: str = "false",
    pid: int = 0,
) -> None:
    """
        Seed a ``launchctl print <domain>/<name>`` result for the given
        service state.
    """
    target = "{}/{}".format(domain, name)
    runner.results[("launchctl", "print", target)] = _print_result(
        label=name,
        state=state,
        disabled=disabled,
        pid=pid,
    )
    return


def _success_runner_for(
    *,
    name: str,
    scope: MacosServiceScope,
    state: str,
    disabled: str,
    target_uid: Optional[int] = None,
) -> _MockCommandRunner:
    """
        Build a runner seeded with a service state and a default
        success :class:`RunResult` for any un-seeded write command.
    """
    if scope == MacosServiceScope.SYSTEM:
        domain = "system"
    else:
        if target_uid is None:
            target_uid = 501
        domain = "gui/{}".format(target_uid)

    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_service_state(
        runner=runner,
        name=name,
        domain=domain,
        state=state,
        disabled=disabled,
    )
    return runner


# ----------------------------------------------------------------------
# start_service
# ----------------------------------------------------------------------


def test_start_service_invokes_launchctl_kickstart():
    """
        start_service on a not-running service dispatches
        ``launchctl kickstart -k``.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    expected = ["launchctl", "kickstart", "-k", "system/com.example.svc"]
    testbase.assert_equal(write_call["args"], expected)
    testbase.assert_equal(result.service_name, "com.example.svc")
    testbase.assert_equal(result.scope, MacosServiceScope.SYSTEM)
    return


def test_start_service_skipped_when_already_running():
    """
        start_service returns SKIPPED when the service is already
        running.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "kickstart"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_start_service_dry_run_short_circuits():
    """
        Dry-run short-circuits start_service without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(dry_run=True),
    )
    result = manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    expected = ["launchctl", "kickstart", "-k", "system/com.example.svc"]
    testbase.assert_equal(result.command, expected)
    return


# ----------------------------------------------------------------------
# stop_service
# ----------------------------------------------------------------------


def test_stop_service_invokes_launchctl_bootout():
    """
        stop_service on a running service dispatches
        ``launchctl bootout``.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    expected = ["launchctl", "bootout", "system/com.example.svc"]
    testbase.assert_equal(write_call["args"], expected)
    return


def test_stop_service_skipped_when_not_running():
    """
        stop_service on a not-running service returns SKIPPED.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootout"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# restart_service / reload_service
# ----------------------------------------------------------------------


def test_restart_service_unconditional():
    """
        restart_service always dispatches ``kickstart -k``, even when
        the service is already running.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.restart_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    kickstart_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "kickstart"]
    ]
    testbase.assert_equal(len(kickstart_calls), 1)
    expected = ["launchctl", "kickstart", "-k", "system/com.example.svc"]
    testbase.assert_equal(kickstart_calls[0]["args"], expected)
    return


def test_reload_service_refuses_when_inactive():
    """
        reload_service returns FAILED without invoking
        ``launchctl kill SIGHUP`` when the service is not running.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.reload_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.FAILED)
    testbase.assert_equal(
        result.message, "service is not active; cannot reload",
    )
    reload_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "kill"]
    ]
    testbase.assert_equal(reload_calls, [])
    return


# ----------------------------------------------------------------------
# enable_service / disable_service / mask_service / unmask_service
# ----------------------------------------------------------------------


def test_enable_service_invokes_launchctl_enable():
    """
        enable_service on a disabled service dispatches
        ``launchctl enable``.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="true",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    expected = ["launchctl", "enable", "system/com.example.svc"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_enable_service_skipped_when_enabled():
    """
        enable_service returns SKIPPED when the service is already
        enabled.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "enable"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_service_invokes_launchctl_disable():
    """
        disable_service on an enabled service dispatches
        ``launchctl disable``.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.disable_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    expected = ["launchctl", "disable", "system/com.example.svc"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_mask_service_invokes_launchctl_disable_with_mask_flag():
    """
        mask_service emulates "mask" via ``launchctl disable`` because
        launchd has no native mask primitive (see docstring).  When the
        service is currently enabled, the call dispatches a disable.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.mask_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    expected = ["launchctl", "disable", "system/com.example.svc"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_unmask_service_invokes_launchctl_enable():
    """
        unmask_service inverts the mask fallback: when the service is
        currently disabled (== masked in this fallback), the call
        dispatches ``launchctl enable``.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="true",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.unmask_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    expected = ["launchctl", "enable", "system/com.example.svc"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# get_service_status / list_services
# ----------------------------------------------------------------------


def test_get_service_status_parses_print_output():
    """
        get_service_status parses ``launchctl print`` into a
        :class:`MacosServiceStatus`.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "print", "system/com.example.svc")] = (
        _print_result(
            label="com.example.svc",
            state="running",
            pid=4321,
            program="/usr/local/bin/example",
            last_exit_code=0,
            disabled="false",
        )
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    status = manager.get_service_status(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(status.active_state, "running")
    testbase.assert_equal(status.enabled_state, "enabled")
    testbase.assert_equal(status.main_pid, 4321)
    testbase.assert_equal(status.last_exit_code, 0)
    testbase.assert_equal(status.service_name, "com.example.svc")
    testbase.assert_equal(status.scope, MacosServiceScope.SYSTEM)
    testbase.assert_equal(status.domain, "system")
    # ``path`` is preferred over ``program`` in the parser.
    testbase.assert_equal(
        status.path,
        "/Library/LaunchDaemons/com.example.svc.plist",
    )
    return


def test_list_services_parses_list_output():
    """
        list_services parses ``launchctl list`` output into
        :class:`MacosServiceInfo` rows, including the ``-`` PID and
        Status column markers.
    """
    list_stdout = (
        "PID\tStatus\tLabel\n"
        "1234\t0\tcom.apple.cfprefsd.xpc.daemon\n"
        "-\t0\tcom.apple.bluetoothd\n"
        "-\t-\tcom.example.disabled.service\n"
    )
    runner = _MockCommandRunner()
    runner.results[("launchctl", "list")] = RunResult(
        exit_code=0, stdout=list_stdout, stderr="",
    )

    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    listed = manager.list_services(scope=MacosServiceScope.SYSTEM)

    testbase.assert_equal(len(listed), 3)

    first = listed[0]
    testbase.assert_equal(first.service_name, "com.apple.cfprefsd.xpc.daemon")
    testbase.assert_equal(first.pid, 1234)
    testbase.assert_equal(first.status, 0)
    testbase.assert_equal(first.scope, MacosServiceScope.SYSTEM)
    testbase.assert_equal(first.domain, "system")

    second = listed[1]
    testbase.assert_equal(second.service_name, "com.apple.bluetoothd")
    testbase.assert_equal(second.pid, None)
    testbase.assert_equal(second.status, 0)

    third = listed[2]
    testbase.assert_equal(third.service_name, "com.example.disabled.service")
    testbase.assert_equal(third.pid, None)
    testbase.assert_equal(third.status, None)
    return


# ----------------------------------------------------------------------
# reload_daemon
# ----------------------------------------------------------------------


def test_reload_daemon_returns_success_noop():
    """
        reload_daemon returns SUCCESS without invoking the runner
        because launchd has no daemon-reload equivalent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(elevated=True),
    )
    result = manager.reload_daemon(scope=MacosServiceScope.SYSTEM)
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.message,
        "macOS launchd reloads plists at bootstrap; reload_daemon is a no-op.",
    )
    return


# ----------------------------------------------------------------------
# Scope + elevation
# ----------------------------------------------------------------------


def test_user_scope_uses_gui_domain():
    """
        USER scope produces a ``gui/<uid>/<name>`` target string.
    """
    runner = _success_runner_for(
        name="com.example.agent", scope=MacosServiceScope.USER,
        state="not running", disabled="false",
        target_uid=501,
    )
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(target_uid=501),
    )
    result = manager.start_service(
        name="com.example.agent", scope=MacosServiceScope.USER,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.SUCCESS)
    expected = [
        "launchctl", "kickstart", "-k", "gui/501/com.example.agent",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_user_scope_requires_target_uid():
    """
        USER scope without a configured ``target_uid`` raises
        :class:`MacosServiceQueryError` before invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(target_uid=None),
    )
    raised = False
    try:
        manager.start_service(
            name="com.example.agent", scope=MacosServiceScope.USER,
        )
    except MacosServiceQueryError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_write_with_elevated_config_passes_elevated_true():
    """
        SYSTEM-scope writes pass ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(elevated=True),
    )
    manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    write_call = runner.calls[-1]
    expected = ["launchctl", "kickstart", "-k", "system/com.example.svc"]
    testbase.assert_equal(write_call["args"], expected)
    testbase.assert_equal(write_call["elevated"], True)
    return


def test_user_scope_writes_never_elevate():
    """
        USER-scope writes never pass ``elevated=True``, even when the
        config is configured to elevate system writes.
    """
    runner = _success_runner_for(
        name="com.example.agent", scope=MacosServiceScope.USER,
        state="not running", disabled="false",
        target_uid=501,
    )
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(
            elevated=True, target_uid=501,
        ),
    )
    manager.start_service(
        name="com.example.agent", scope=MacosServiceScope.USER,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "kickstart"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], False)
    return


def test_read_never_elevates():
    """
        Reads and idempotency-probe commands never elevate, even when
        the config is configured to elevate writes.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    manager = MacosServiceManager(
        runner=runner,
        config=MacosServiceManagerConfig(elevated=True),
    )
    # Direct read.
    manager.get_service_status(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    # Write whose idempotency probe must also not elevate.
    manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )

    elevated_probe_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "print"] and c["elevated"]
    ]
    testbase.assert_equal(elevated_probe_calls, [])

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "kickstart"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Failure surfaces
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_result_with_stderr():
    """
        A non-zero exit from the write maps to FAILED with the captured
        stderr / exit_code / command preserved on the result.
    """
    runner = _success_runner_for(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
        state="not running", disabled="false",
    )
    runner.results[(
        "launchctl", "kickstart", "-k", "system/com.example.svc",
    )] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Operation not permitted",
    )
    manager = MacosServiceManager(
        runner=runner, config=MacosServiceManagerConfig(),
    )
    result = manager.start_service(
        name="com.example.svc", scope=MacosServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, MacosServiceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Operation not permitted")
    expected = ["launchctl", "kickstart", "-k", "system/com.example.svc"]
    testbase.assert_equal(result.command, expected)
    return
