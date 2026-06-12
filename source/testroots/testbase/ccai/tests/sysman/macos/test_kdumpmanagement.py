"""
    Tests for :class:`MacosKdumpManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live ``ls`` / ``cat`` / ``find`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.kdumpmanagement import (
    MacosKdumpActionStatus,
    MacosKdumpManager,
    MacosKdumpManagerConfig,
    MacosKdumpQueryError,
    MacosKdumpTargetKind,
    MacosKdumpTargetSpec,
)


_REPORT_DIR = "/Library/Logs/DiagnosticReports"


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


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> MacosKdumpManager:
    """
        Construct a manager wired to ``runner`` with default paths.
    """
    config = MacosKdumpManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        report_dir=_REPORT_DIR,
    )
    rtnval = MacosKdumpManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# Unsupported writes
# ----------------------------------------------------------------------


def test_enable_kdump_returns_failed_with_macos_message():
    """
        ``enable_kdump`` on macOS returns ``FAILED`` with the
        unsupported-on-macOS advisory rather than executing a command.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, MacosKdumpActionStatus.FAILED)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("macOS has no kdump equivalent" in result.message)
    testbase.assert_true("sysdiagnose" in result.message)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_true(result.command is None)
    return


def test_disable_kdump_returns_failed_with_macos_message():
    """
        ``disable_kdump`` on macOS returns ``FAILED`` with the
        unsupported-on-macOS advisory.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)

    result = manager.disable_kdump()

    testbase.assert_equal(result.status, MacosKdumpActionStatus.FAILED)
    testbase.assert_true("macOS has no kdump equivalent" in result.message)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_configure_kdump_target_returns_failed_with_macos_message():
    """
        ``configure_kdump_target`` on macOS returns ``FAILED`` and
        echoes the requested ``spec.path`` onto ``target``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)

    spec = MacosKdumpTargetSpec(
        target_kind=MacosKdumpTargetKind.LOCAL_PATH,
        path="/var/dumps",
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, MacosKdumpActionStatus.FAILED)
    testbase.assert_true("macOS has no kdump equivalent" in result.message)
    testbase.assert_equal(result.target, "/var/dumps")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_kdump_status
# ----------------------------------------------------------------------


def test_get_kdump_status_returns_synthetic_status():
    """
        ``get_kdump_status`` on macOS returns a fixed always-on snapshot
        without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)

    status = manager.get_kdump_status()

    testbase.assert_true(status.enabled)
    testbase.assert_true(status.running)
    testbase.assert_equal(status.target_kind, MacosKdumpTargetKind.LOCAL_PATH)
    testbase.assert_equal(status.target_path, _REPORT_DIR)
    testbase.assert_equal(status.message, "macOS native kernel-panic logging")
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# list_recent_dumps
# ----------------------------------------------------------------------


def test_list_recent_dumps_parses_directory_listing():
    """
        ``list_recent_dumps`` returns one info per ``*.panic`` /
        ``*.ips`` entry; everything else is filtered.
    """
    runner = _MockCommandRunner()
    listing = (
        "Kernel-2026-04-24-093131.panic\n"
        "myapp-2026-04-24-093131.ips\n"
        "summary.txt\n"
        ".DS_Store\n"
    )
    runner.results[("ls", "-1t", _REPORT_DIR)] = _ok(stdout=listing)
    manager = _make_manager(runner=runner)

    result = manager.list_recent_dumps()

    testbase.assert_equal(len(result), 2)
    ids = [item.id for item in result]
    testbase.assert_true("Kernel-2026-04-24-093131.panic" in ids)
    testbase.assert_true("myapp-2026-04-24-093131.ips" in ids)
    first = result[0]
    testbase.assert_equal(
        first.path,
        "/Library/Logs/DiagnosticReports/Kernel-2026-04-24-093131.panic",
    )
    return


def test_list_recent_dumps_empty_when_directory_missing():
    """
        ``list_recent_dumps`` returns an empty list when ``ls`` reports
        the directory missing.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1t", _REPORT_DIR)] = _fail(
        stderr="ls: /Library/Logs/DiagnosticReports: No such file or directory",
    )
    manager = _make_manager(runner=runner)

    result = manager.list_recent_dumps()

    testbase.assert_equal(result, [])
    return


# ----------------------------------------------------------------------
# clear_old_dumps
# ----------------------------------------------------------------------


def test_clear_old_dumps_invokes_find_with_mtime():
    """
        ``clear_old_dumps`` invokes ``find ... -mtime +N -exec rm``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.clear_old_dumps(age_days=14)

    testbase.assert_equal(result.status, MacosKdumpActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    argv = runner.calls[0]["args"]
    testbase.assert_equal(argv[0], "find")
    testbase.assert_true("-mtime" in argv)
    testbase.assert_true("+14" in argv)
    testbase.assert_true("*.panic" in argv)
    testbase.assert_true("*.ips" in argv)
    return


def test_clear_old_dumps_dry_run_short_circuits():
    """
        ``clear_old_dumps`` does not invoke the runner under
        ``dry_run=True``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True, dry_run=True)

    result = manager.clear_old_dumps(age_days=7)

    testbase.assert_equal(result.status, MacosKdumpActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_dump_info
# ----------------------------------------------------------------------


def test_get_dump_info_parses_panic_output():
    """
        ``get_dump_info`` surfaces kernel + panic message + size from
        the parsed panic file.
    """
    runner = _MockCommandRunner()
    dump_id = "Kernel-2026-04-24-093131.panic"
    full_path = "/Library/Logs/DiagnosticReports/{}".format(dump_id)

    panic_text = (
        "panic(cpu 4 caller 0xfffffe0027a83cf4): assertion failed\n"
        "Darwin Kernel Version 24.0.0: Mon Mar  3 22:34:32 PST 2026\n"
        "Backtrace:\n"
        "0xfffffe0027a83cf4\n"
    )
    runner.results[("cat", full_path)] = _ok(stdout=panic_text)
    runner.results[("stat", "-f", "%Sm;%z", full_path)] = _ok(
        stdout="Apr 24 09:31:31 2026;81920\n",
    )
    manager = _make_manager(runner=runner)

    info = manager.get_dump_info(dump_id=dump_id)

    testbase.assert_equal(info.id, dump_id)
    testbase.assert_equal(info.path, full_path)
    testbase.assert_true(info.kernel is not None)
    testbase.assert_true("Darwin Kernel Version" in info.kernel)
    testbase.assert_true(info.panic_message is not None)
    testbase.assert_true("panic(cpu 4" in info.panic_message)
    testbase.assert_equal(info.size_bytes, 81920)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        ``clear_old_dumps`` forwards ``elevated=True`` to the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    manager.clear_old_dumps(age_days=7)

    testbase.assert_equal(len(runner.calls), 1)
    testbase.assert_true(runner.calls[0]["elevated"])
    return


def test_reads_never_elevate():
    """
        Read verbs always pass ``elevated=False`` regardless of config.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1t", _REPORT_DIR)] = _ok(stdout="")
    manager = _make_manager(runner=runner, elevated=True)

    manager.list_recent_dumps()

    for call in runner.calls:
        testbase.assert_false(call["elevated"])
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``find`` exit produces a ``FAILED`` result with
        captured stderr.
    """
    runner = _MockCommandRunner()
    runner.default_result = _fail(stderr="find: permission denied", exit_code=1)
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.clear_old_dumps(age_days=7)

    testbase.assert_equal(result.status, MacosKdumpActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in (result.stderr or ""))
    return


def test_get_dump_info_raises_on_missing_file():
    """
        ``get_dump_info`` raises :class:`MacosKdumpQueryError` when the
        report file cannot be read.
    """
    runner = _MockCommandRunner()
    dump_id = "Kernel-2026-04-24-093131.panic"
    runner.results[
        ("cat", "/Library/Logs/DiagnosticReports/{}".format(dump_id))
    ] = _fail(stderr="cat: no such file", exit_code=1)
    manager = _make_manager(runner=runner)

    raised = False
    try:
        manager.get_dump_info(dump_id=dump_id)
    except MacosKdumpQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_clear_old_dumps_rejects_negative_age():
    """
        ``clear_old_dumps(-1)`` returns ``FAILED`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.clear_old_dumps(age_days=-1)

    testbase.assert_equal(result.status, MacosKdumpActionStatus.FAILED)
    testbase.assert_equal(result.message, "age_days must be >= 0")
    testbase.assert_equal(len(runner.calls), 0)
    return
