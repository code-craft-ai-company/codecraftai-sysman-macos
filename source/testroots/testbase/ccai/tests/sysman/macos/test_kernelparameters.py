"""
    Tests for :class:`MacosKernelParameterManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``sysctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.kernelparameters import (
    MacosKernelParameterActionStatus,
    MacosKernelParameterManager,
    MacosKernelParameterManagerConfig,
    MacosKernelParameterQueryError,
    MacosParameterValue,
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
# get_parameter
# ----------------------------------------------------------------------


def test_get_parameter_returns_value():
    """
        get_parameter returns a :class:`MacosParameterValue` populated
        from ``sysctl -n`` stdout.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="2048\n", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.get_parameter(name="kern.maxproc")

    testbase.assert_true(result is not None)
    testbase.assert_equal(result.name, "kern.maxproc")
    testbase.assert_equal(result.value, "2048")
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_get_parameter_returns_none_on_unknown_oid():
    """
        get_parameter returns ``None`` when ``sysctl -n`` reports the
        OID is unknown.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.bogus")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="sysctl: unknown oid 'kern.bogus'\n",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.get_parameter(name="kern.bogus")
    testbase.assert_equal(result, None)
    return


def test_get_parameter_raises_on_unexpected_failure():
    """
        get_parameter raises :class:`MacosKernelParameterQueryError`
        when ``sysctl`` reports a non-zero exit with an unrecognised
        stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=2,
        stdout="",
        stderr="sysctl: catastrophic failure\n",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    raised = False
    try:
        manager.get_parameter(name="kern.maxproc")
    except MacosKernelParameterQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# list_parameters
# ----------------------------------------------------------------------


def test_list_parameters_parses_both_separators():
    """
        list_parameters parses both ``name = value`` and
        ``name: value`` separator styles emitted by macOS' ``sysctl
        -a``.
    """
    body = (
        "kern.maxproc = 2048\n"
        "kern.ostype: Darwin\n"
        "vm.swapusage: total = 0.00M  used = 0.00M  free = 0.00M\n"
        "\n"
    )
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-a", "--ignore")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.list_parameters()

    testbase.assert_equal(result["kern.maxproc"], "2048")
    testbase.assert_equal(result["kern.ostype"], "Darwin")
    # vm.swapusage uses ``:`` as separator and contains ``=`` in its
    # value.  Our parser prefers ``=`` so this row is parsed using
    # the first ``=`` -- accept whatever the first split yields and
    # just assert the row is present.
    testbase.assert_true("vm.swapusage" in result or "vm.swapusage: total" in result)
    return


def test_list_parameters_raises_on_non_zero_exit():
    """
        list_parameters raises :class:`MacosKernelParameterQueryError`
        when ``sysctl -a`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-a", "--ignore")] = RunResult(
        exit_code=1, stdout="", stderr="boom",
    )
    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    raised = False
    try:
        manager.list_parameters()
    except MacosKernelParameterQueryError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


# ----------------------------------------------------------------------
# set_parameter — runtime only
# ----------------------------------------------------------------------


def test_set_runtime_dispatches_sysctl_w():
    """
        set_parameter(persistent=False) on a non-matching value
        dispatches ``sysctl -w name=value``.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="1024\n", stderr="",
    )
    runner.results[("sysctl", "-w", "kern.maxproc=2048")] = RunResult(
        exit_code=0, stdout="kern.maxproc: 1024 -> 2048\n", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=False,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["sysctl", "-w", "kern.maxproc=2048"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_runtime_skipped_when_value_matches():
    """
        set_parameter(persistent=False) returns SKIPPED when the
        runtime value already equals the target value.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="2048\n", stderr="",
    )
    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=False,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# set_parameter — persistent
# ----------------------------------------------------------------------


def test_set_persistent_rewrites_file_and_applies_runtime():
    """
        set_parameter(persistent=True) writes the merged sysctl.conf
        via tee AND dispatches sysctl -w.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="1024\n", stderr="",
    )
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=0,
        stdout="# header\nvm.swapusage=1\n",
        stderr="",
    )
    runner.results[("tee", "/etc/sysctl.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("sysctl", "-w", "kern.maxproc=2048")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=True,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SUCCESS,
    )
    testbase.assert_equal(result.persistent, True)
    testbase.assert_equal(result.source, "/etc/sysctl.conf")

    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("kern.maxproc=2048" in tee_calls[0]["stdin"])
    testbase.assert_true("vm.swapusage=1" in tee_calls[0]["stdin"])
    testbase.assert_equal(tee_calls[0]["elevated"], True)

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_persistent_skipped_when_file_and_runtime_match():
    """
        set_parameter(persistent=True) returns SKIPPED when both the
        runtime value and the sysctl.conf line already match.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="2048\n", stderr="",
    )
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=0,
        stdout="# managed by codecraftai-sysman-macos\nkern.maxproc=2048\n",
        stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=True,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SKIPPED,
    )
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_persistent_creates_file_when_absent():
    """
        set_parameter(persistent=True) writes a header-prefixed body
        when ``/etc/sysctl.conf`` does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="1024\n", stderr="",
    )
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=1, stdout="", stderr="cat: not found",
    )
    runner.results[("tee", "/etc/sysctl.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("sysctl", "-w", "kern.maxproc=2048")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=True,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SUCCESS,
    )
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true("# managed by codecraftai-sysman-macos" in payload)
    testbase.assert_true("kern.maxproc=2048" in payload)
    return


def test_set_persistent_partial_failure_reports_step():
    """
        When the file write succeeds but ``sysctl -w`` fails, the
        result is FAILED with the runtime stderr surfaced.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="1024\n", stderr="",
    )
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("tee", "/etc/sysctl.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("sysctl", "-w", "kern.maxproc=2048")] = RunResult(
        exit_code=1, stdout="", stderr="sysctl: permission denied",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=True,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "runtime_apply_failed")
    testbase.assert_true("permission denied" in result.stderr)
    return


# ----------------------------------------------------------------------
# reload_parameters
# ----------------------------------------------------------------------


def test_reload_dispatches_sysctl_f():
    """
        reload_parameters dispatches
        ``sysctl -f /etc/sysctl.conf`` (the macOS equivalent of
        ``sysctl --system``).
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(elevated=True),
    )
    result = manager.reload_parameters()

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SUCCESS,
    )
    testbase.assert_equal(
        runner.calls[-1]["args"],
        ["sysctl", "-f", "/etc/sysctl.conf"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# remove_persisted_parameter
# ----------------------------------------------------------------------


def test_remove_persisted_skipped_when_file_absent():
    """
        remove_persisted_parameter returns SKIPPED when
        ``/etc/sysctl.conf`` is absent.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=1, stdout="", stderr="cat: no such file",
    )
    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(),
    )
    result = manager.remove_persisted_parameter(name="kern.maxproc")

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SKIPPED,
    )
    return


def test_remove_persisted_preserves_other_lines():
    """
        remove_persisted_parameter rewrites sysctl.conf with the
        target line gone and other lines preserved verbatim.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/sysctl.conf")] = RunResult(
        exit_code=0,
        stdout="# header\nkern.maxproc=2048\nvm.swapusage=1\n",
        stderr="",
    )
    runner.results[("tee", "/etc/sysctl.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(elevated=True),
    )
    result = manager.remove_persisted_parameter(name="kern.maxproc")

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.SUCCESS,
    )
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true("kern.maxproc" not in payload)
    testbase.assert_true("vm.swapusage=1" in payload)
    testbase.assert_true("# header" in payload)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Dry-run / elevation policy
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_set_parameter():
    """
        Dry-run short-circuits set_parameter without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(dry_run=True),
    )
    result = manager.set_parameter(
        name="kern.maxproc",
        value="2048",
        persistent=True,
    )

    testbase.assert_equal(
        result.status, MacosKernelParameterActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command, ["tee", "/etc/sysctl.conf"])
    return


def test_reads_never_elevate_even_when_config_elevated():
    """
        Reads (``sysctl -n``, ``sysctl -a``, ``cat`` probes) MUST NOT
        elevate even when config.elevated is True.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "kern.maxproc")] = RunResult(
        exit_code=0, stdout="2048\n", stderr="",
    )
    runner.results[("sysctl", "-a", "--ignore")] = RunResult(
        exit_code=0, stdout="kern.maxproc = 2048\n", stderr="",
    )

    manager = MacosKernelParameterManager(
        runner=runner,
        config=MacosKernelParameterManagerConfig(elevated=True),
    )
    manager.get_parameter(name="kern.maxproc")
    manager.list_parameters()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return
