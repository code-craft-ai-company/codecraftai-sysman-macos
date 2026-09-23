"""
    Tests for :class:`MacosAuditManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``audit`` / ``praudit`` / ``launchctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.auditmanagement import (
    MacosAuditActionStatus,
    MacosAuditEventQuery,
    MacosAuditManager,
    MacosAuditManagerConfig,
    MacosAuditRuleSpec,
    MacosAuditStatus,
    _add_class_to_flags,
    _parse_audit_control_flags,
    _parse_audit_status,
    _parse_praudit_lines,
    _remove_class_from_flags,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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


def _seed_audit_control(
    runner: _MockCommandRunner,
    *,
    flags_value: str,
) -> None:
    """
        Pre-seed the runner so that ``cat /etc/security/audit_control``
        returns a body whose ``flags`` line is ``flags_value``.
    """
    body = (
        "# audit_control\n"
        "dir:/var/audit\n"
        "flags:{}\n"
        "minfree:5\n"
        "naflags:lo,aa\n"
        "policy:cnt,argv\n"
        "filesz:2M\n"
    ).format(flags_value)
    runner.results[("cat", "/etc/security/audit_control")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


def _seed_audit_status(
    runner: _MockCommandRunner,
    *,
    auditing: bool,
    pid: int,
    locked: bool = False,
) -> None:
    """
        Pre-seed the runner so that ``audit -s`` reports a known
        state.
    """
    if auditing:
        condition_line = "AUC_AUDITING:auditing"
    else:
        condition_line = "AUC_AUDITING:no audit"

    body_lines = [
        condition_line,
        "policy:cnt,argv",
        "pid:{}".format(pid),
    ]
    if locked:
        body_lines.append("locked:yes")
    body = "\n".join(body_lines) + "\n"
    runner.results[("audit", "-s")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_rule
# ----------------------------------------------------------------------


def test_add_rule_invokes_audit():
    """
        add_rule writes the modified audit_control via tee and
        dispatches ``audit -s`` to reload.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo,aa")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(elevated=True),
    )
    spec = MacosAuditRuleSpec(name="file_attr", event_class="fa")
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, MacosAuditActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/security/audit_control"],
    )
    testbase.assert_true("flags:lo,aa,fa" in tee_calls[0]["stdin"])
    testbase.assert_equal(tee_calls[0]["elevated"], True)

    reload_calls = [
        c for c in runner.calls if c["args"] == ["audit", "-s"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_add_rule_skipped_when_present():
    """
        add_rule returns SKIPPED when the class is already in
        flags.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo,aa,fa")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    spec = MacosAuditRuleSpec(name="file_attr", event_class="fa")
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, MacosAuditActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_add_rule_dry_run_short_circuits():
    """
        add_rule with dry_run=True returns DRY_RUN and never invokes
        the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(dry_run=True),
    )
    spec = MacosAuditRuleSpec(name="file_attr", event_class="fa")
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, MacosAuditActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_event_class_added_to_audit_control():
    """
        add_rule appends the new event_class to the ``flags`` line.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    spec = MacosAuditRuleSpec(name="process", event_class="pc")
    manager.add_rule(spec=spec)

    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_true("flags:lo,pc" in tee_calls[0]["stdin"])
    testbase.assert_true("naflags:lo,aa" in tee_calls[0]["stdin"])
    return


# ----------------------------------------------------------------------
# remove_rule
# ----------------------------------------------------------------------


def test_remove_rule_invokes_audit():
    """
        remove_rule writes the modified audit_control via tee and
        dispatches ``audit -s``.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo,aa,fa")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="fa")

    testbase.assert_equal(result.status, MacosAuditActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("flags:lo,aa" in tee_calls[0]["stdin"])
    testbase.assert_true("flags:lo,aa,fa" not in tee_calls[0]["stdin"])
    return


def test_remove_rule_skipped_when_absent():
    """
        remove_rule returns SKIPPED when the class is not in flags.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo,aa")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="not_there")

    testbase.assert_equal(result.status, MacosAuditActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# list_rules
# ----------------------------------------------------------------------


def test_list_rules_parses_output():
    """
        list_rules parses both ``audit_control`` flags and
        ``audit_user`` rows.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo,aa,fa")
    runner.results[("cat", "/etc/security/audit_user")] = RunResult(
        exit_code=0,
        stdout=(
            "# per-user overrides\n"
            "root:lo,aa:no\n"
            "alice:fa:no\n"
        ),
        stderr="",
    )

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    rules = manager.list_rules()

    class_rules = [r for r in rules if r.subsystem == "class"]
    user_rules = [r for r in rules if r.subsystem == "user"]

    testbase.assert_equal(len(class_rules), 3)
    class_tokens = [r.event_class for r in class_rules]
    testbase.assert_true("lo" in class_tokens)
    testbase.assert_true("aa" in class_tokens)
    testbase.assert_true("fa" in class_tokens)

    testbase.assert_equal(len(user_rules), 2)
    testbase.assert_equal(user_rules[0].name, "root")
    testbase.assert_equal(user_rules[1].name, "alice")
    return


# ----------------------------------------------------------------------
# enable / disable
# ----------------------------------------------------------------------


def test_enable_auditing_invokes_audit():
    """
        enable_auditing dispatches ``audit -i`` and
        ``launchctl bootstrap``.
    """
    runner = _MockCommandRunner()
    _seed_audit_status(runner=runner, auditing=False, pid=0)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(elevated=True),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, MacosAuditActionStatus.SUCCESS)
    primary_calls = [
        c for c in runner.calls if c["args"] == ["audit", "-i"]
    ]
    testbase.assert_equal(len(primary_calls), 1)
    testbase.assert_equal(primary_calls[0]["elevated"], True)

    bootstrap_calls = [
        c for c in runner.calls if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(
        bootstrap_calls[0]["args"],
        [
            "launchctl", "bootstrap", "system",
            "/System/Library/LaunchDaemons/com.apple.auditd.plist",
        ],
    )
    return


def test_enable_auditing_skipped_when_enabled():
    """
        enable_auditing returns SKIPPED when ``audit -s`` reports the
        subsystem as enabled and running.
    """
    runner = _MockCommandRunner()
    _seed_audit_status(runner=runner, auditing=True, pid=4321)

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, MacosAuditActionStatus.SKIPPED)
    invoked = [c for c in runner.calls if c["args"] == ["audit", "-i"]]
    testbase.assert_equal(invoked, [])
    return


def test_disable_auditing_invokes_launchctl():
    """
        disable_auditing dispatches ``launchctl bootout``.
    """
    runner = _MockCommandRunner()
    _seed_audit_status(runner=runner, auditing=True, pid=4321)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(elevated=True),
    )
    result = manager.disable_auditing()

    testbase.assert_equal(result.status, MacosAuditActionStatus.SUCCESS)
    bootout_calls = [
        c for c in runner.calls if c["args"][:2] == ["launchctl", "bootout"]
    ]
    testbase.assert_equal(len(bootout_calls), 1)
    testbase.assert_equal(
        bootout_calls[0]["args"],
        ["launchctl", "bootout", "system/com.apple.auditd"],
    )
    testbase.assert_equal(bootout_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_audit_status
# ----------------------------------------------------------------------


def test_get_audit_status_parses_audit_output():
    """
        get_audit_status parses ``audit -s`` text into a
        MacosAuditStatus.
    """
    runner = _MockCommandRunner()
    body = (
        "AUC_AUDITING:auditing\n"
        "policy:cnt,argv\n"
        "pid:8765\n"
        "lost:0\n"
    )
    runner.results[("audit", "-s")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    status = manager.get_audit_status()

    testbase.assert_equal(isinstance(status, MacosAuditStatus), True)
    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.pid, 8765)
    testbase.assert_equal(status.lost_events, 0)
    testbase.assert_equal(status.locked, False)
    return


def test_get_audit_status_detects_locked_mode():
    """
        get_audit_status flags the ``locked:yes`` line as immutable.
    """
    runner = _MockCommandRunner()
    runner.results[("audit", "-s")] = RunResult(
        exit_code=0,
        stdout="AUC_AUDITING:auditing\nlocked:yes\n",
        stderr="",
    )

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    status = manager.get_audit_status()
    testbase.assert_equal(status.locked, True)
    return


# ----------------------------------------------------------------------
# search_events
# ----------------------------------------------------------------------


def test_search_events_invokes_praudit_with_query():
    """
        search_events builds a ``praudit -l <log>`` argv from the
        configured log directory.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    query = MacosAuditEventQuery(key="login")
    manager.search_events(query=query)

    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"], ["praudit", "-l", "/var/audit/current"],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


def test_search_events_filters_lines_by_key():
    """
        search_events filters praudit lines by the query's ``key``
        substring.
    """
    runner = _MockCommandRunner()
    payload = (
        "header,1,2,login - aa,Mon Apr 22 10:00:00 2024,subject root\n"
        "header,1,2,file write,Mon Apr 22 10:00:01 2024,subject alice\n"
    )
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    query = MacosAuditEventQuery(key="login", limit=5)
    events = manager.search_events(query=query)

    testbase.assert_equal(len(events), 1)
    testbase.assert_true("login" in events[0].raw)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass elevated=True to the runner when the config opts
        in.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(elevated=True),
    )
    spec = MacosAuditRuleSpec(name="fa", event_class="fa")
    manager.add_rule(spec=spec)

    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads never pass elevated=True.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo")
    runner.results[("cat", "/etc/security/audit_user")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("audit", "-s")] = RunResult(
        exit_code=0, stdout="AUC_AUDITING:auditing\n", stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(elevated=True),
    )
    manager.list_rules()
    manager.get_audit_status()
    manager.search_events(query=MacosAuditEventQuery(key="probe"))

    elevated_reads = [
        c for c in runner.calls
        if c["elevated"] is True
        and (
            c["args"] == ["audit", "-s"]
            or c["args"][:1] == ["praudit"]
            or c["args"][:1] == ["cat"]
        )
    ]
    testbase.assert_equal(elevated_reads, [])
    return


def test_failed_command_surfaces_stderr():
    """
        When tee exits non-zero, the result reports FAILED and
        surfaces stderr.
    """
    runner = _MockCommandRunner()
    _seed_audit_control(runner=runner, flags_value="lo")
    runner.results[("tee", "/etc/security/audit_control")] = RunResult(
        exit_code=1, stdout="", stderr="boom",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosAuditManager(
        runner=runner, config=MacosAuditManagerConfig(),
    )
    spec = MacosAuditRuleSpec(name="fa", event_class="fa")
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, MacosAuditActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "boom")
    testbase.assert_equal(result.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Pure-function tests (renderers / parsers)
# ----------------------------------------------------------------------


def test_add_class_to_flags_appends():
    """
        _add_class_to_flags appends a missing token and reports
        changed=True.
    """
    text = "flags:lo,aa\nminfree:5\n"
    new_body, changed = _add_class_to_flags(text=text, event_class="fa")
    testbase.assert_equal(changed, True)
    testbase.assert_true("flags:lo,aa,fa" in new_body)
    testbase.assert_true("minfree:5" in new_body)
    return


def test_remove_class_from_flags_drops():
    """
        _remove_class_from_flags drops a present token.
    """
    text = "flags:lo,aa,fa\n"
    new_body, changed = _remove_class_from_flags(
        text=text, event_class="aa",
    )
    testbase.assert_equal(changed, True)
    testbase.assert_true("flags:lo,fa" in new_body)
    testbase.assert_true("aa" not in new_body)
    return


def test_parse_audit_control_flags_handles_full_file():
    """
        _parse_audit_control_flags surfaces every comma-separated
        token in the flags line.
    """
    text = "dir:/var/audit\nflags:lo,aa,fa\nminfree:5\n"
    rows = _parse_audit_control_flags(
        text=text, source="/etc/security/audit_control",
    )
    tokens = [r.event_class for r in rows]
    testbase.assert_equal(tokens, ["lo", "aa", "fa"])
    return


def test_parse_audit_status_handles_partial_output():
    """
        _parse_audit_status tolerates missing fields.
    """
    text = "AUC_AUDITING:no audit\n"
    status = _parse_audit_status(text=text)
    testbase.assert_equal(status.enabled, False)
    testbase.assert_equal(status.running, False)
    testbase.assert_equal(status.pid, None)
    return


def test_parse_praudit_lines_respects_limit():
    """
        _parse_praudit_lines stops after collecting ``limit``
        records.
    """
    payload = (
        "header,1,2,login,Mon\n"
        "header,1,2,login,Tue\n"
        "header,1,2,login,Wed\n"
    )
    events = _parse_praudit_lines(
        text=payload,
        key=None,
        event_class=None,
        user=None,
        limit=2,
    )
    testbase.assert_equal(len(events), 2)
    return
