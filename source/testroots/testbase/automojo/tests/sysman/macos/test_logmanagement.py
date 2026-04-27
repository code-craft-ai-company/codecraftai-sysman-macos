"""
    Tests for :class:`MacosLogManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live ``log show`` / ``log stream`` /
    ``newsyslog`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.logmanagement import (
    MacosLogActionStatus,
    MacosLogManager,
    MacosLogManagerConfig,
    MacosLogQueryError,
    MacosRotationPolicy,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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


def _ndjson_lines(entries: list) -> str:
    """
        Build a multi-line ``log show --style ndjson`` stdout payload
        from a list of dicts.
    """
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    rtnval = "\n".join(lines) + "\n"
    return rtnval


# ----------------------------------------------------------------------
# get_log
# ----------------------------------------------------------------------


def test_get_log_constructs_log_show_command():
    """
        get_log builds the canonical
        ``log show --predicate 'subsystem == "<name>"' --last <window>
        --style ndjson`` argv.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    manager.get_log(name="com.apple.network", lines=50)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "log", "show",
            "--predicate", 'subsystem == "com.apple.network"',
            "--last", "1h",
            "--style", "ndjson",
        ],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


def test_get_log_includes_since_when_provided():
    """
        get_log emits ``--start <iso>`` only when the parameter is
        set.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    manager.get_log(
        name="com.apple.network",
        lines=10,
        since="2024-01-01T00:00:00Z",
    )
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "log", "show",
            "--predicate", 'subsystem == "com.apple.network"',
            "--start", "2024-01-01T00:00:00Z",
            "--style", "ndjson",
        ],
    )
    return


def test_get_log_parses_ndjson_payload():
    """
        get_log decodes one entry per ndjson line.
    """
    payload = _ndjson_lines([
        {
            "timestamp": "2024-01-01 00:00:00",
            "processImagePath": "/usr/sbin/configd",
            "subsystem": "com.apple.network",
            "eventMessage": "first message",
        },
        {
            "timestamp": "2024-01-01 00:01:00",
            "process": "configd",
            "subsystem": "com.apple.network",
            "eventMessage": "second message",
        },
    ])
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    entries = manager.get_log(name="com.apple.network", lines=100)
    testbase.assert_equal(entries.total_count, 2)
    testbase.assert_equal(entries.entries[0].process, "configd")
    testbase.assert_equal(entries.entries[0].subsystem, "com.apple.network")
    testbase.assert_equal(entries.entries[0].message, "first message")
    testbase.assert_equal(entries.entries[1].process, "configd")
    return


def test_get_log_raises_query_error_on_failure():
    """
        get_log raises :class:`MacosLogQueryError` when ``log show``
        exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=1, stdout="", stderr="permission denied",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    raised = False
    try:
        manager.get_log(name="com.apple.network", lines=10)
    except MacosLogQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_get_log_trims_to_lines_cap():
    """
        get_log trims the parsed list when more entries arrive than
        ``lines`` requested.
    """
    payload = _ndjson_lines([
        {"timestamp": "t1", "eventMessage": "a"},
        {"timestamp": "t2", "eventMessage": "b"},
        {"timestamp": "t3", "eventMessage": "c"},
    ])
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    entries = manager.get_log(name="com.apple.foo", lines=2)
    testbase.assert_equal(entries.total_count, 2)
    testbase.assert_equal(entries.entries[0].message, "b")
    testbase.assert_equal(entries.entries[1].message, "c")
    return


# ----------------------------------------------------------------------
# tail_log
# ----------------------------------------------------------------------


def test_tail_log_uses_log_stream_and_yields_entries():
    """
        tail_log dispatches ``log stream`` and yields one entry per
        ndjson line in the runner's stdout.
    """
    payload = _ndjson_lines([
        {
            "timestamp": "t1",
            "subsystem": "com.apple.foo",
            "eventMessage": "alpha",
        },
        {
            "timestamp": "t2",
            "subsystem": "com.apple.foo",
            "eventMessage": "beta",
        },
    ])
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    yielded = list(manager.tail_log(name="com.apple.foo", follow=True))
    testbase.assert_equal(len(yielded), 2)
    testbase.assert_equal(yielded[0].message, "alpha")
    testbase.assert_equal(yielded[1].message, "beta")

    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "log", "stream",
            "--predicate", 'subsystem == "com.apple.foo"',
            "--style", "ndjson",
        ],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


# ----------------------------------------------------------------------
# list_logs
# ----------------------------------------------------------------------


def test_list_logs_combines_unified_and_newsyslog_sources():
    """
        list_logs reports unified subsystems from ``log config
        --list`` and drop-ins from ``ls -1 /etc/newsyslog.d``.
    """
    runner = _MockCommandRunner()
    runner.results[("log", "config", "--list")] = RunResult(
        exit_code=0,
        stdout="subsystem: com.apple.network\nsubsystem: com.example\n",
        stderr="",
    )
    runner.results[("ls", "-1", "/etc/newsyslog.d")] = RunResult(
        exit_code=0,
        stdout="apache2.conf\nnginx.conf\n",
        stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    listed = manager.list_logs()
    testbase.assert_equal(len(listed), 4)
    testbase.assert_equal(listed[0].name, "com.apple.network")
    testbase.assert_equal(listed[0].source, "unified")
    testbase.assert_equal(listed[2].name, "apache2")
    testbase.assert_equal(listed[2].source, "newsyslog")
    testbase.assert_equal(listed[2].path, "/etc/newsyslog.d/apache2.conf")
    return


# ----------------------------------------------------------------------
# rotate_log
# ----------------------------------------------------------------------


def test_rotate_log_uses_drop_in_when_present():
    """
        rotate_log dispatches ``newsyslog -F -f <drop_in>`` when the
        drop-in exists.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=0, stdout="", stderr="")
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    result = manager.rotate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["newsyslog", "-F"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["newsyslog", "-F", "-f", "/etc/newsyslog.d/apache2.conf"],
    )
    testbase.assert_equal(result.path, "/etc/newsyslog.d/apache2.conf")
    return


def test_rotate_log_falls_back_to_global_newsyslog():
    """
        rotate_log falls back to ``newsyslog -F`` when no per-name
        drop-in exists.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    result = manager.rotate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["newsyslog", "-F"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["newsyslog", "-F"])
    testbase.assert_equal(result.path, None)
    return


def test_rotate_log_dry_run_does_not_invoke_newsyslog():
    """
        rotate_log in dry-run mode short-circuits without invoking
        ``newsyslog``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner,
        config=MacosLogManagerConfig(dry_run=True),
    )
    result = manager.rotate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.DRY_RUN)
    write_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["newsyslog"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# truncate_log
# ----------------------------------------------------------------------


def test_truncate_log_returns_failed_without_drop_in():
    """
        truncate_log returns FAILED when no per-name newsyslog
        drop-in exists, with a descriptive message about the unified
        log limitation.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    result = manager.truncate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.FAILED)
    testbase.assert_true(
        "macOS unified logging cannot be truncated" in (result.message or ""),
    )
    truncate_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["truncate"]
    ]
    testbase.assert_equal(truncate_calls, [])
    return


def test_truncate_log_dispatches_truncate_for_file_path():
    """
        truncate_log extracts the log path from the drop-in and
        dispatches ``truncate -s 0 <path>`` against it.
    """
    drop_in_body = (
        "# Generated by automojo\n"
        "/var/log/apache2/error_log root:wheel 640 7 * $D0 Z\n"
    )
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=0, stdout="", stderr="")
    )
    runner.results[("cat", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=0, stdout=drop_in_body, stderr="")
    )
    runner.results[("cat", "/var/log/apache2/error_log")] = (
        RunResult(exit_code=0, stdout="some content\n", stderr="")
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    result = manager.truncate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.SUCCESS)
    truncate_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["truncate"]
    ]
    testbase.assert_equal(len(truncate_calls), 1)
    testbase.assert_equal(
        truncate_calls[0]["args"],
        ["truncate", "-s", "0", "/var/log/apache2/error_log"],
    )
    return


def test_truncate_log_skipped_when_file_already_empty():
    """
        truncate_log returns SKIPPED when the target file is already
        zero bytes.
    """
    drop_in_body = (
        "/var/log/apache2/error_log root:wheel 640 7 * $D0 Z\n"
    )
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=0, stdout="", stderr="")
    )
    runner.results[("cat", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=0, stdout=drop_in_body, stderr="")
    )
    runner.results[("cat", "/var/log/apache2/error_log")] = (
        RunResult(exit_code=0, stdout="", stderr="")
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    result = manager.truncate_log(name="apache2")
    testbase.assert_equal(result.status, MacosLogActionStatus.SKIPPED)
    truncate_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["truncate"]
    ]
    testbase.assert_equal(truncate_calls, [])
    return


# ----------------------------------------------------------------------
# configure_rotation
# ----------------------------------------------------------------------


def test_configure_rotation_writes_via_tee():
    """
        configure_rotation dispatches ``tee /etc/newsyslog.d/<name>.conf``
        with the rendered policy on stdin when no existing content
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    policy = MacosRotationPolicy(
        frequency="daily",
        rotate_count=5,
        compress=True,
        size_kb=512,
    )
    result = manager.configure_rotation(name="apache2", policy=policy)
    testbase.assert_equal(result.status, MacosLogActionStatus.SUCCESS)

    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/newsyslog.d/apache2.conf"],
    )
    rendered = tee_calls[0]["stdin"]
    testbase.assert_true("/var/log/apache2.log" in rendered)
    testbase.assert_true("root:wheel" in rendered)
    testbase.assert_true(" 640 5 512 $D0 Z" in rendered)
    return


def test_configure_rotation_skipped_when_content_matches():
    """
        configure_rotation returns SKIPPED when the existing drop-in
        body matches the rendered desired content.
    """
    runner = _MockCommandRunner()
    policy = MacosRotationPolicy(
        frequency="weekly",
        rotate_count=4,
        compress=False,
        size_kb=None,
    )

    # Two-pass approach: build the rendered body by running once with
    # an empty existing-body; this gives us the exact bytes the manager
    # would write.  Then seed those bytes as the existing body and
    # confirm the second call skips.
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager_one = MacosLogManager(
        runner=runner, config=MacosLogManagerConfig(),
    )
    runner.results[("cat", "/etc/newsyslog.d/myapp.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    manager_one.configure_rotation(name="myapp", policy=policy)
    rendered = None
    for c in runner.calls:
        if c["args"][:1] == ["tee"]:
            rendered = c["stdin"]
            break
    testbase.assert_true(rendered is not None)

    # Now seed the rendered body as the existing on-disk content and
    # verify a fresh manager reports SKIPPED.
    runner_two = _MockCommandRunner()
    runner_two.results[("cat", "/etc/newsyslog.d/myapp.conf")] = (
        RunResult(exit_code=0, stdout=rendered, stderr="")
    )
    manager_two = MacosLogManager(
        runner=runner_two, config=MacosLogManagerConfig(),
    )
    result = manager_two.configure_rotation(name="myapp", policy=policy)
    testbase.assert_equal(result.status, MacosLogActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner_two.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_configure_rotation_dry_run_short_circuits():
    """
        configure_rotation in dry-run mode does not invoke tee.
    """
    runner = _MockCommandRunner()
    manager = MacosLogManager(
        runner=runner,
        config=MacosLogManagerConfig(dry_run=True),
    )
    policy = MacosRotationPolicy(frequency="daily")
    result = manager.configure_rotation(name="apache2", policy=policy)
    testbase.assert_equal(result.status, MacosLogActionStatus.DRY_RUN)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate_when_configured_reads_do_not():
    """
        Writes pass ``elevated=True`` when the config requests it;
        reads and idempotency probes never elevate.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/newsyslog.d/apache2.conf")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = MacosLogManager(
        runner=runner,
        config=MacosLogManagerConfig(elevated=True),
    )
    policy = MacosRotationPolicy(frequency="daily")
    manager.configure_rotation(name="apache2", policy=policy)
    # Run a read.
    manager.get_log(name="com.apple.network", lines=10)

    write_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)

    read_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["log"] or c["args"][:1] == ["cat"]
    ]
    elevated_reads = [c for c in read_calls if c["elevated"]]
    testbase.assert_equal(elevated_reads, [])
    return
