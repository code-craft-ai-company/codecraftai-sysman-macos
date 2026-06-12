"""
    Tests for :class:`MacosRemoteLogForwardingManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``syslogd`` / ``launchctl`` /
    ``logger`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos import remotelogforwarding as rlf
from ccai.sysman.macos.remotelogforwarding import (
    MacosForwarderActionStatus,
    MacosForwarderInfo,
    MacosForwarderManagerConfig,
    MacosForwarderQueryError,
    MacosForwarderSpec,
    MacosForwarderStatus,
    MacosForwarderTestResult,
    MacosRemoteLogForwardingManager,
    _render_block,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        call and returns pre-seeded :class:`RunResult` values
        keyed by argv tuple.
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed the runner so that ``cat <path>`` returns ``body``
        on success, or a non-zero exit when ``body`` is None.
    """
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


# ----------------------------------------------------------------------
# add_forwarder
# ----------------------------------------------------------------------


def test_add_forwarder_invokes_tee():
    """
        add_forwarder dispatches ``tee /etc/syslog.conf`` with
        the merged body on stdin and follows up with
        ``launchctl kickstart -k system/com.apple.syslogd``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    _seed_cat(runner, path="/etc/syslog.conf", body="")

    config = MacosForwarderManagerConfig(elevated=True)
    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        protocol="udp", selector="*.*",
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/syslog.conf"],
    )
    body = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "# >>> codecraftai-fwd:collector1 >>>" in body, True,
    )
    testbase.assert_equal(
        "*.* @logs.example.com:514" in body, True,
    )

    kickstart_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "launchctl"
            and call["args"][1] == "kickstart"
        )
    ]
    testbase.assert_equal(len(kickstart_calls), 1)
    testbase.assert_equal(
        kickstart_calls[0]["args"],
        ["launchctl", "kickstart", "-k", "system/com.apple.syslogd"],
    )
    testbase.assert_equal(kickstart_calls[0]["elevated"], True)
    return


def test_add_forwarder_skipped_when_present():
    """
        add_forwarder returns SKIPPED when the on-disk body
        already contains the rendered block byte-for-byte.
    """
    runner = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        protocol="udp", selector="*.*",
    )
    block_body = _render_block(spec=spec, prefix="codecraftai-fwd")
    _seed_cat(runner, path="/etc/syslog.conf", body=block_body)

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_add_forwarder_dry_run():
    """
        With ``dry_run=True``, add_forwarder records the
        intended argv but does not invoke ``tee`` or
        ``launchctl``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/syslog.conf", body="")

    config = MacosForwarderManagerConfig(dry_run=True)
    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.DRY_RUN,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_tcp_protocol_downgrades_to_udp_with_message():
    """
        A TCP / TLS spec still renders a UDP action line and
        the result's ``message`` flags the downgrade.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    _seed_cat(runner, path="/etc/syslog.conf", body="")

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        protocol="tcp",
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )
    testbase.assert_equal(
        "downgraded to UDP" in (result.message or ""), True,
    )
    return


# ----------------------------------------------------------------------
# remove_forwarder
# ----------------------------------------------------------------------


def test_remove_forwarder_invokes_tee():
    """
        remove_forwarder writes the file body with the matching
        block stripped and follows up with the kickstart.
    """
    runner = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
    )
    body = (
        "# operator-authored line\n"
        + _render_block(spec=spec, prefix="codecraftai-fwd")
        + "*.notice  /var/log/messages\n"
    )
    _seed_cat(runner, path="/etc/syslog.conf", body=body)
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosRemoteLogForwardingManager(
        runner=runner,
        config=MacosForwarderManagerConfig(elevated=True),
    )
    result = manager.remove_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    body_after = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "codecraftai-fwd:collector1" in body_after, False,
    )
    testbase.assert_equal(
        "operator-authored line" in body_after, True,
    )
    testbase.assert_equal(
        "*.notice  /var/log/messages" in body_after, True,
    )
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_remove_forwarder_skipped_when_absent():
    """
        remove_forwarder returns SKIPPED when no matching block
        is present.
    """
    runner = _MockCommandRunner()
    body = "# operator-authored line\n*.notice /var/log/messages\n"
    _seed_cat(runner, path="/etc/syslog.conf", body=body)

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    result = manager.remove_forwarder(id="ghost")
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# list_forwarders
# ----------------------------------------------------------------------


def test_list_forwarders_parses_syslog_conf_output():
    """
        list_forwarders parses the syslog.conf body and reports
        enabled / disabled state per managed block.
    """
    runner = _MockCommandRunner()
    spec1 = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        protocol="udp", selector="*.*", enabled=True,
    )
    spec2 = MacosForwarderSpec(
        id="collector2", host="logs2.example.com", port=601,
        protocol="udp", selector="auth.*", enabled=False,
    )
    body = (
        "# operator preamble\n"
        + _render_block(spec=spec1, prefix="codecraftai-fwd")
        + _render_block(spec=spec2, prefix="codecraftai-fwd")
    )
    _seed_cat(runner, path="/etc/syslog.conf", body=body)

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    forwarders = manager.list_forwarders()
    testbase.assert_equal(len(forwarders), 2)

    by_id = {item.id: item for item in forwarders}
    testbase.assert_equal(
        isinstance(by_id["collector1"], MacosForwarderInfo), True,
    )
    testbase.assert_equal(by_id["collector1"].host, "logs.example.com")
    testbase.assert_equal(by_id["collector1"].port, 514)
    testbase.assert_equal(by_id["collector1"].enabled, True)
    testbase.assert_equal(by_id["collector1"].selector_or_query, "*.*")

    testbase.assert_equal(by_id["collector2"].enabled, False)
    testbase.assert_equal(by_id["collector2"].port, 601)
    testbase.assert_equal(
        by_id["collector2"].selector_or_query, "auth.*",
    )
    return


def test_list_forwarders_raises_on_unexpected_failure():
    """
        list_forwarders raises MacosForwarderQueryError when
        ``cat`` reports a non-zero exit with a non-empty
        stderr that does not look like 'no such file'.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/syslog.conf")] = RunResult(
        exit_code=2, stdout="", stderr="Permission denied",
    )

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    raised = False
    try:
        manager.list_forwarders()
    except MacosForwarderQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# enable_forwarder / disable_forwarder
# ----------------------------------------------------------------------


def test_enable_forwarder_invokes_tee():
    """
        enable_forwarder uncomments the block's action line and
        writes the updated file via ``tee``.
    """
    runner = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        enabled=False,
    )
    body = _render_block(spec=spec, prefix="codecraftai-fwd")
    _seed_cat(runner, path="/etc/syslog.conf", body=body)
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "# *.* @logs.example.com:514" in new_body, False,
    )
    testbase.assert_equal(
        "*.* @logs.example.com:514" in new_body, True,
    )
    return


def test_enable_forwarder_skipped_when_enabled():
    """
        enable_forwarder returns SKIPPED when the block is
        already enabled.
    """
    runner = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        enabled=True,
    )
    body = _render_block(spec=spec, prefix="codecraftai-fwd")
    _seed_cat(runner, path="/etc/syslog.conf", body=body)

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_disable_forwarder_invokes_tee():
    """
        disable_forwarder comments out the block's action line
        and writes the updated file via ``tee``.
    """
    runner = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        enabled=True,
    )
    body = _render_block(spec=spec, prefix="codecraftai-fwd")
    _seed_cat(runner, path="/etc/syslog.conf", body=body)
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    result = manager.disable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "# *.* @logs.example.com:514" in new_body, True,
    )
    return


# ----------------------------------------------------------------------
# test_forwarder
# ----------------------------------------------------------------------


def test_test_forwarder_invokes_logger():
    """
        test_forwarder dispatches ``logger -t codecraftai-test
        <message>`` without elevation.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosRemoteLogForwardingManager(
        runner=runner,
        config=MacosForwarderManagerConfig(elevated=True),
    )
    result = manager.test_forwarder(id="collector1")
    testbase.assert_equal(
        isinstance(result, MacosForwarderTestResult), True,
    )
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.SUCCESS,
    )
    testbase.assert_equal(result.forwarder_id, "collector1")
    testbase.assert_equal(
        result.test_message_id is not None, True,
    )

    logger_calls = [
        call for call in runner.calls
        if call["args"][0] == "logger"
    ]
    testbase.assert_equal(len(logger_calls), 1)
    testbase.assert_equal(logger_calls[0]["args"][0], "logger")
    testbase.assert_equal(logger_calls[0]["args"][1], "-t")
    testbase.assert_equal(logger_calls[0]["args"][2], "codecraftai-test")
    testbase.assert_equal(logger_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_forwarder_status
# ----------------------------------------------------------------------


def test_get_forwarder_status_parses_output():
    """
        get_forwarder_status reports ``enabled`` per the action
        line's comment status and reports ``enabled=False`` for
        absent forwarders.
    """
    runner_active = _MockCommandRunner()
    spec = MacosForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
        enabled=True,
    )
    body = _render_block(spec=spec, prefix="codecraftai-fwd")
    _seed_cat(runner_active, path="/etc/syslog.conf", body=body)
    manager_active = MacosRemoteLogForwardingManager(
        runner=runner_active,
        config=MacosForwarderManagerConfig(),
    )
    status_active = manager_active.get_forwarder_status(
        id="collector1",
    )
    testbase.assert_equal(
        isinstance(status_active, MacosForwarderStatus), True,
    )
    testbase.assert_equal(status_active.enabled, True)

    runner_absent = _MockCommandRunner()
    _seed_cat(runner_absent, path="/etc/syslog.conf", body="")
    manager_absent = MacosRemoteLogForwardingManager(
        runner=runner_absent,
        config=MacosForwarderManagerConfig(),
    )
    status_absent = manager_absent.get_forwarder_status(
        id="ghost",
    )
    testbase.assert_equal(status_absent.enabled, False)
    return


# ----------------------------------------------------------------------
# Elevation, error surfacing, parser
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        With ``elevated=True``, every write dispatch sets
        ``elevated=True`` on the runner call.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    _seed_cat(runner, path="/etc/syslog.conf", body="")
    config = MacosForwarderManagerConfig(elevated=True)
    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = MacosForwarderSpec(
        id="collector1", host="example.com", port=514,
    )
    manager.add_forwarder(spec=spec)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (``cat``, ``logger``) never elevate.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    _seed_cat(runner, path="/etc/syslog.conf", body="")
    config = MacosForwarderManagerConfig(elevated=True)
    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    manager.list_forwarders()
    manager.get_forwarder_status(id="collector1")
    manager.test_forwarder(id="collector1")

    read_argv0 = ("cat", "logger")
    for call in runner.calls:
        argv0 = call["args"][0]
        if argv0 in read_argv0:
            testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When ``tee`` returns non-zero the manager surfaces the
        exit code and stderr without raising and without
        invoking ``launchctl``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/syslog.conf", body="")
    runner.results[("tee", "/etc/syslog.conf")] = RunResult(
        exit_code=2, stdout="", stderr="Permission denied",
    )
    manager = MacosRemoteLogForwardingManager(
        runner=runner, config=MacosForwarderManagerConfig(),
    )
    spec = MacosForwarderSpec(
        id="collector1", host="example.com", port=514,
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, MacosForwarderActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "Permission denied")

    kickstart_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "launchctl"
            and call["args"][1] == "kickstart"
        )
    ]
    testbase.assert_equal(len(kickstart_calls), 0)
    return


def test_macos_unified_logging_limitation_documented():
    """
        The module docstring documents the unified-logging
        limitation prominently so that callers see it in
        ``help()`` output.
    """
    docstring = rlf.__doc__ or ""
    testbase.assert_equal(
        "unified-logging" in docstring.lower()
        or "unified logging" in docstring.lower(),
        True,
    )
    testbase.assert_equal(
        "filebeat" in docstring.lower()
        or "nxlog" in docstring.lower()
        or "fluent bit" in docstring.lower(),
        True,
    )
    return
