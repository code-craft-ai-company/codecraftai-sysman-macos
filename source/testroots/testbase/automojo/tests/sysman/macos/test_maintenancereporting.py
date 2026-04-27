"""
    Tests for :class:`MacosMaintenanceReporter`.

    All tests use a ``_MockCommandRunner`` to capture ``mkdir -p`` and
    ``tee`` calls.  No real filesystem is touched.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.maintenancereporting import (
    MacosActionRecord,
    MacosActionStatus,
    MacosMaintenanceReporter,
    MacosMaintenanceReporterConfig,
    MacosReportFormat,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal :class:`ICommandRunner` stand-in.  Records every call
        and returns a configurable default result.
    """

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
        record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(record)

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _make_reporter(
    *,
    runner: Optional[_MockCommandRunner] = None,
    config: Optional[MacosMaintenanceReporterConfig] = None,
) -> tuple:
    """
        Build a reporter + runner pair for use in tests.
    """
    if runner is None:
        runner = _MockCommandRunner()
    if config is None:
        config = MacosMaintenanceReporterConfig()
    reporter = MacosMaintenanceReporter(runner=runner, config=config)
    rtnval = (reporter, runner)
    return rtnval


def test_begin_session_creates_session():
    """
        :meth:`begin_session` produces a session with the supplied id
        and an empty action list.
    """
    reporter, runner = _make_reporter()
    session = reporter.begin_session(session_id="macos-001")
    testbase.assert_equal(session.run_id, "macos-001")
    testbase.assert_true(reporter.session is not None)
    testbase.assert_equal(len(reporter.actions), 0)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_record_action_appends_to_actions():
    """
        :meth:`record_action` stores the supplied record on the
        actions list in insertion order.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-002")
    reporter.record_action(MacosActionRecord(
        feature="package-management",
        operation="install",
        status=MacosActionStatus.SUCCESS,
    ))
    reporter.record_action(MacosActionRecord(
        feature="service-management",
        operation="restart",
        status=MacosActionStatus.SKIPPED,
    ))
    actions = reporter.actions
    testbase.assert_equal(len(actions), 2)
    testbase.assert_equal(actions[0].operation, "install")
    testbase.assert_equal(actions[1].status, MacosActionStatus.SKIPPED)
    return


def test_record_query_appends_to_queries():
    """
        :meth:`record_query` stores records on the queries list.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-003")
    reporter.record_query(MacosActionRecord(
        feature="package-management",
        operation="list_installed",
        status=MacosActionStatus.SUCCESS,
    ))
    queries = reporter.queries
    testbase.assert_equal(len(queries), 1)
    testbase.assert_equal(queries[0].operation, "list_installed")
    testbase.assert_equal(len(reporter.actions), 0)
    return


def test_record_error_captures_traceback():
    """
        :meth:`record_error` captures the type, message, traceback,
        and appends a FAILED record to the actions list.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-004")
    try:
        raise ValueError("nope")
    except ValueError as exc:
        event = reporter.record_error(
            error=exc,
            context={"feature": "kernel-parameters", "operation": "apply"},
        )
    testbase.assert_equal(event["type"], "ValueError")
    testbase.assert_equal(event["message"], "nope")
    testbase.assert_true("ValueError" in event["traceback"])
    testbase.assert_equal(len(reporter.actions), 1)
    testbase.assert_equal(reporter.actions[0].status, MacosActionStatus.FAILED)
    return


def test_end_session_sets_ended_at_and_status():
    """
        :meth:`end_session` stamps ``ended_at`` and recomputes
        ``status`` based on the recorded actions.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-005")
    reporter.record_action(MacosActionRecord(
        feature="f", operation="op", status=MacosActionStatus.SUCCESS,
    ))
    reporter.record_action(MacosActionRecord(
        feature="f", operation="op", status=MacosActionStatus.FAILED,
    ))
    finalised = reporter.end_session()
    testbase.assert_true(finalised.ended_at is not None)
    testbase.assert_equal(finalised.status, MacosActionStatus.FAILED)
    return


def test_emit_json_report_writes_file():
    """
        :meth:`emit_report` with JSON format invokes ``mkdir -p`` and
        ``tee`` exactly once each.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="macos-006")
    reporter.record_action(MacosActionRecord(
        feature="package-management",
        operation="install",
        status=MacosActionStatus.SUCCESS,
        detail="installed nginx",
    ))
    reporter.end_session()
    write_result = reporter.emit_report(format=MacosReportFormat.JSON)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("/report.json"))
    testbase.assert_equal(len(runner.calls), 2)
    testbase.assert_equal(runner.calls[0]["args"][0], "mkdir")
    testbase.assert_equal(runner.calls[0]["args"][1], "-p")
    testbase.assert_equal(runner.calls[1]["args"][0], "tee")
    payload = json.loads(runner.calls[1]["stdin"])
    testbase.assert_equal(payload["session"]["run_id"], "macos-006")
    testbase.assert_equal(len(payload["actions"]), 1)
    testbase.assert_equal(payload["counts"]["succeeded"], 1)
    return


def test_emit_markdown_report_writes_file():
    """
        :meth:`emit_report` with MARKDOWN format produces a file with
        the expected ``.md`` extension and ``# macOS Maintenance
        Report`` heading.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="macos-007")
    reporter.record_action(MacosActionRecord(
        feature="firewall-management",
        operation="add_rule",
        status=MacosActionStatus.SUCCESS,
    ))
    reporter.end_session()
    write_result = reporter.emit_report(format=MacosReportFormat.MARKDOWN)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("/report.md"))
    body = runner.calls[1]["stdin"]
    testbase.assert_true(body.startswith("# macOS Maintenance Report"))
    testbase.assert_true("## Summary" in body)
    testbase.assert_true("## Actions" in body)
    return


def test_emit_yaml_report_writes_file():
    """
        :meth:`emit_report` with YAML format produces a file with the
        expected ``.yaml`` extension.  The body is YAML when PyYAML is
        importable, JSON otherwise -- both are valid string payloads
        that round-trip back to the report's session id.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="macos-008")
    reporter.end_session()
    write_result = reporter.emit_report(format=MacosReportFormat.YAML)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.path.endswith("/report.yaml"))
    body = runner.calls[1]["stdin"]
    testbase.assert_true("macos-008" in body)
    return


def test_session_counts_aggregate_correctly():
    """
        :meth:`summary_counts` tallies actions by status accurately.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-009")
    statuses = [
        MacosActionStatus.SUCCESS,
        MacosActionStatus.SUCCESS,
        MacosActionStatus.FAILED,
        MacosActionStatus.SKIPPED,
        MacosActionStatus.DRY_RUN,
    ]
    for s in statuses:
        reporter.record_action(MacosActionRecord(
            feature="f", operation="op", status=s,
        ))
    counts = reporter.summary_counts()
    testbase.assert_equal(counts.total, 5)
    testbase.assert_equal(counts.succeeded, 2)
    testbase.assert_equal(counts.failed, 1)
    testbase.assert_equal(counts.skipped, 1)
    testbase.assert_equal(counts.dry_run, 1)
    return


def test_emit_before_end_session_raises():
    """
        Calling :meth:`emit_report` before :meth:`end_session` raises
        :class:`RuntimeError`.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-010")
    raised = False
    try:
        reporter.emit_report(format=MacosReportFormat.JSON)
    except RuntimeError:
        raised = True
    testbase.assert_true(raised)
    return


def test_emit_dry_run_skips_runner():
    """
        :meth:`emit_report` with ``config.dry_run=True`` returns a
        ``dry_run=True`` result and does not invoke the runner.
    """
    reporter, runner = _make_reporter(
        config=MacosMaintenanceReporterConfig(dry_run=True),
    )
    reporter.begin_session(session_id="macos-011")
    reporter.end_session()
    write_result = reporter.emit_report(format=MacosReportFormat.JSON)
    testbase.assert_true(write_result.success)
    testbase.assert_true(write_result.dry_run)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_emit_failure_when_tee_fails():
    """
        Runner failures during ``tee`` surface as
        ``MacosReportWriteResult(success=False)`` with an error
        message.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1,
            stdout="",
            stderr="permission denied",
        ),
    )
    reporter, _ignored = _make_reporter(runner=runner)
    reporter.begin_session(session_id="macos-012")
    reporter.end_session()
    write_result = reporter.emit_report(format=MacosReportFormat.JSON)
    testbase.assert_false(write_result.success)
    testbase.assert_true(write_result.error is not None)
    testbase.assert_true("permission denied" in write_result.error)
    return


def test_default_session_path_uses_output_dir():
    """
        :meth:`default_session_path` joins the configured output
        directory with the session id.
    """
    reporter, _runner = _make_reporter(
        config=MacosMaintenanceReporterConfig(output_dir="/var/log/automojo/maintenance"),
    )
    path = reporter.default_session_path(session_id="macos-013")
    testbase.assert_equal(path, "/var/log/automojo/maintenance/macos-013/")
    return


def test_record_action_without_session_raises():
    """
        :meth:`record_action` before :meth:`begin_session` raises
        :class:`RuntimeError`.
    """
    reporter, _runner = _make_reporter()
    raised = False
    try:
        reporter.record_action(MacosActionRecord(
            feature="f", operation="op", status=MacosActionStatus.SUCCESS,
        ))
    except RuntimeError:
        raised = True
    testbase.assert_true(raised)
    return


def test_actions_by_status_filter():
    """
        :meth:`actions_by_status` returns only matching records in
        insertion order.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-014")
    reporter.record_action(MacosActionRecord(
        feature="f", operation="ok-1", status=MacosActionStatus.SUCCESS,
    ))
    reporter.record_action(MacosActionRecord(
        feature="f", operation="bad-1", status=MacosActionStatus.FAILED,
    ))
    reporter.record_action(MacosActionRecord(
        feature="f", operation="ok-2", status=MacosActionStatus.SUCCESS,
    ))
    successes = reporter.actions_by_status(MacosActionStatus.SUCCESS)
    failures = reporter.actions_by_status(MacosActionStatus.FAILED)
    testbase.assert_equal(len(successes), 2)
    testbase.assert_equal(successes[0].operation, "ok-1")
    testbase.assert_equal(successes[1].operation, "ok-2")
    testbase.assert_equal(len(failures), 1)
    testbase.assert_equal(failures[0].operation, "bad-1")
    return


def test_actions_by_feature_filter():
    """
        :meth:`actions_by_feature` returns only records emitted by
        the supplied feature.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-015")
    reporter.record_action(MacosActionRecord(
        feature="package-management", operation="install",
        status=MacosActionStatus.SUCCESS,
    ))
    reporter.record_action(MacosActionRecord(
        feature="firewall-management", operation="add_rule",
        status=MacosActionStatus.SUCCESS,
    ))
    pkg_records = reporter.actions_by_feature(feature="package-management")
    testbase.assert_equal(len(pkg_records), 1)
    testbase.assert_equal(pkg_records[0].operation, "install")
    return


def test_emit_unknown_format_returns_error_result():
    """
        :meth:`emit_report` with a non-:class:`MacosReportFormat`
        argument returns ``success=False`` without invoking the
        runner.
    """
    reporter, runner = _make_reporter()
    reporter.begin_session(session_id="macos-016")
    reporter.end_session()
    write_result = reporter.emit_report(format="bogus", path="/tmp/x")
    testbase.assert_false(write_result.success)
    testbase.assert_true(write_result.error is not None)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_to_text_contains_key_fields():
    """
        :meth:`to_text` includes the run id, host, and a per-action
        line.
    """
    reporter, _runner = _make_reporter()
    reporter.begin_session(session_id="macos-017")
    reporter.record_action(MacosActionRecord(
        feature="package-management",
        operation="install",
        status=MacosActionStatus.SUCCESS,
        detail="installed nginx",
    ))
    body = reporter.to_text()
    testbase.assert_true("macos-017" in body)
    testbase.assert_true("MACOS MAINTENANCE REPORT" in body)
    testbase.assert_true("install" in body)
    testbase.assert_true("installed nginx" in body)
    return
