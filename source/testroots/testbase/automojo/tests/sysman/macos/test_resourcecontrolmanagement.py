"""
    Tests for :class:`MacosResourceControlManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values keyed by argv tuple.  No live ``launchctl`` /
    ``taskpolicy`` / ``tee`` / ``cat`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.resourcecontrolmanagement import (
    MacosIoLimitSpec,
    MacosResourceControlActionResult,
    MacosResourceControlActionStatus,
    MacosResourceControlManager,
    MacosResourceControlManagerConfig,
    MacosResourceControlQueryError,
    MacosResourceGroupInfo,
    MacosResourceGroupSpec,
    MacosResourceGroupUsage,
    _parse_plist_attached_pids,
    _parse_plist_memory_bytes,
    _parse_top_output,
    _render_plist_body,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


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
# Seed helpers
# ----------------------------------------------------------------------


def _seed_test_e_present(
    runner: _MockCommandRunner,
    *,
    path: str,
    present: bool = True,
) -> None:
    """
        Seed ``test -e <path>``.
    """
    key = ("test", "-e", path)
    if present:
        runner.results[key] = RunResult(exit_code=0, stdout="", stderr="")
    else:
        runner.results[key] = RunResult(exit_code=1, stdout="", stderr="")
    return


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed ``cat <path>``.  ``body=None`` reports a missing
        file.
    """
    key = ("cat", path)
    if body is None:
        runner.results[key] = RunResult(exit_code=1, stdout="", stderr="")
    else:
        runner.results[key] = RunResult(
            exit_code=0, stdout=body, stderr="",
        )
    return


def _path_for(name: str) -> str:
    """
        :returns: Default plist path for ``name``.
    """
    rtnval = "/Library/LaunchDaemons/automojo-rc-{}.plist".format(name)
    return rtnval


# ----------------------------------------------------------------------
# create_group
# ----------------------------------------------------------------------


def test_create_group_invokes_launchctl():
    """
        ``create_group`` must write the plist body via ``tee``
        and then dispatch ``launchctl bootstrap``.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")
    _seed_cat(runner, path=plist_path, body=None)

    config = MacosResourceControlManagerConfig(elevated=True)
    manager = MacosResourceControlManager(runner=runner, config=config)

    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 2)
    testbase.assert_equal(mutating[0]["args"], ["tee", plist_path])
    testbase.assert_equal(
        mutating[1]["args"],
        ["launchctl", "bootstrap", "system", plist_path],
    )

    # Body landed on tee stdin.
    body = mutating[0]["stdin"]
    testbase.assert_equal("automojo-rc-batch" in body, True)
    testbase.assert_equal("RLIMIT_AS" in body, True)
    testbase.assert_equal("1073741824" in body, True)
    return


def test_create_group_skipped_when_present():
    """
        ``create_group`` must return SKIPPED when the on-disk
        plist matches the rendered body byte-for-byte.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    body = _render_plist_body(spec=spec, label_prefix="automojo-rc-")
    _seed_cat(runner, path=plist_path, body=body)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SKIPPED)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 0)
    return


def test_create_group_dry_run():
    """
        ``create_group`` must return DRY_RUN with the planned
        ``tee`` argv and never invoke the runner.
    """
    runner = _MockCommandRunner()
    config = MacosResourceControlManagerConfig(dry_run=True)
    manager = MacosResourceControlManager(runner=runner, config=config)

    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.DRY_RUN)
    testbase.assert_equal(result.command, ["tee", _path_for("batch")])
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_group
# ----------------------------------------------------------------------


def test_delete_group_invokes_launchctl():
    """
        ``delete_group`` must invoke ``launchctl bootout`` and
        ``rm -f``.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")
    _seed_test_e_present(runner, path=plist_path, present=True)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("rm", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 2)
    testbase.assert_equal(
        mutating[0]["args"],
        ["launchctl", "bootout", "system/automojo-rc-batch"],
    )
    testbase.assert_equal(mutating[1]["args"], ["rm", "-f", plist_path])
    return


def test_delete_group_skipped_when_absent():
    """
        ``delete_group`` must return SKIPPED when the plist
        does not exist.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")
    _seed_test_e_present(runner, path=plist_path, present=False)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SKIPPED)
    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("rm", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 0)
    return


# ----------------------------------------------------------------------
# set_cpu_limit (FAILED with macOS detail)
# ----------------------------------------------------------------------


def test_set_cpu_limit_returns_failed_with_macos_detail():
    """
        ``set_cpu_limit`` must return FAILED with
        ``detail="macos_no_per_group_cpu_limit"`` and never
        invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosResourceControlManager(runner=runner)

    result = manager.set_cpu_limit(name="batch", percent=50)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.detail, "macos_no_per_group_cpu_limit")
    testbase.assert_equal(result.command, ["taskpolicy", "-c", "utility"])
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# set_memory_limit
# ----------------------------------------------------------------------


def test_set_memory_limit_invokes_tee():
    """
        ``set_memory_limit`` must re-render the plist via
        ``tee`` then re-bootstrap.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    existing_spec = MacosResourceGroupSpec(name="batch", memory_bytes=536870912)
    existing_body = _render_plist_body(spec=existing_spec, label_prefix="automojo-rc-")
    _seed_cat(runner, path=plist_path, body=existing_body)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.set_memory_limit(name="batch", bytes_=1073741824)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 3)
    testbase.assert_equal(mutating[0]["args"], ["tee", plist_path])
    testbase.assert_equal(
        mutating[1]["args"],
        ["launchctl", "bootout", "system/automojo-rc-batch"],
    )
    testbase.assert_equal(
        mutating[2]["args"],
        ["launchctl", "bootstrap", "system", plist_path],
    )

    new_body = mutating[0]["stdin"]
    testbase.assert_equal("1073741824" in new_body, True)
    return


def test_set_memory_limit_skipped_when_match():
    """
        ``set_memory_limit`` must return SKIPPED when the plist's
        existing ``RLIMIT_AS`` already equals the requested
        bytes.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    body = _render_plist_body(spec=spec, label_prefix="automojo-rc-")
    _seed_cat(runner, path=plist_path, body=body)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.set_memory_limit(name="batch", bytes_=1073741824)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SKIPPED)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "launchctl")
    ]
    testbase.assert_equal(len(mutating), 0)
    return


# ----------------------------------------------------------------------
# set_io_limits (FAILED with macOS detail)
# ----------------------------------------------------------------------


def test_set_io_limits_returns_failed_with_macos_detail():
    """
        ``set_io_limits`` must return FAILED with
        ``detail="macos_no_per_group_io_limit"`` and never
        invoke the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosResourceControlManager(runner=runner)

    spec = MacosIoLimitSpec(weight=200)
    result = manager.set_io_limits(name="batch", spec=spec)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.detail, "macos_no_per_group_io_limit")
    testbase.assert_equal(result.command, ["taskpolicy", "-d", "background"])
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# attach_process / detach_process
# ----------------------------------------------------------------------


def test_attach_process_invokes_taskpolicy():
    """
        ``attach_process`` must invoke ``taskpolicy -p <pid> -c
        utility`` and re-render the plist with the new PID.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    existing_spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=536870912, attached_pids=[111],
    )
    existing_body = _render_plist_body(
        spec=existing_spec, label_prefix="automojo-rc-",
    )
    _seed_cat(runner, path=plist_path, body=existing_body)

    config = MacosResourceControlManagerConfig(elevated=True)
    manager = MacosResourceControlManager(runner=runner, config=config)
    result = manager.attach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 222)

    tp_calls = [
        call for call in runner.calls if call["args"][0] == "taskpolicy"
    ]
    testbase.assert_equal(len(tp_calls), 1)
    testbase.assert_equal(
        tp_calls[0]["args"],
        ["taskpolicy", "-p", "222", "-c", "utility"],
    )
    testbase.assert_equal(tp_calls[0]["elevated"], True)

    tee_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal("<integer>111</integer>" in new_body, True)
    testbase.assert_equal("<integer>222</integer>" in new_body, True)
    return


def test_attach_process_skipped_when_present():
    """
        ``attach_process`` must return SKIPPED when the PID is
        already in the plist's ``AttachedPIDs``.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    existing_spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=536870912, attached_pids=[111, 222],
    )
    existing_body = _render_plist_body(
        spec=existing_spec, label_prefix="automojo-rc-",
    )
    _seed_cat(runner, path=plist_path, body=existing_body)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.attach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SKIPPED)

    tp_calls = [
        call for call in runner.calls if call["args"][0] == "taskpolicy"
    ]
    testbase.assert_equal(len(tp_calls), 0)
    return


def test_detach_process_invokes_taskpolicy():
    """
        ``detach_process`` must invoke ``taskpolicy -p <pid> -c
        default``.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    existing_spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=536870912, attached_pids=[111, 222],
    )
    existing_body = _render_plist_body(
        spec=existing_spec, label_prefix="automojo-rc-",
    )
    _seed_cat(runner, path=plist_path, body=existing_body)

    manager = MacosResourceControlManager(runner=runner)
    result = manager.detach_process(name="batch", pid=222)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 222)

    tp_calls = [
        call for call in runner.calls if call["args"][0] == "taskpolicy"
    ]
    testbase.assert_equal(len(tp_calls), 1)
    testbase.assert_equal(
        tp_calls[0]["args"],
        ["taskpolicy", "-p", "222", "-c", "default"],
    )

    tee_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal("<integer>222</integer>" in new_body, False)
    testbase.assert_equal("<integer>111</integer>" in new_body, True)
    return


# ----------------------------------------------------------------------
# list_groups
# ----------------------------------------------------------------------


def test_list_groups_returns_list():
    """
        ``list_groups`` must enumerate plists in the plist
        directory and return one
        :class:`MacosResourceGroupInfo` per matching entry.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/Library/LaunchDaemons")] = RunResult(
        exit_code=0,
        stdout=(
            "automojo-rc-batch.plist\n"
            "automojo-rc-worker.plist\n"
            "com.apple.someother.plist\n"
        ),
        stderr="",
    )

    batch_spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=1073741824, attached_pids=[111, 222],
    )
    worker_spec = MacosResourceGroupSpec(name="worker")
    _seed_cat(
        runner, path=_path_for("batch"),
        body=_render_plist_body(spec=batch_spec, label_prefix="automojo-rc-"),
    )
    _seed_cat(
        runner, path=_path_for("worker"),
        body=_render_plist_body(spec=worker_spec, label_prefix="automojo-rc-"),
    )

    manager = MacosResourceControlManager(runner=runner)
    groups = manager.list_groups()

    testbase.assert_equal(len(groups), 2)
    by_name = {info.name: info for info in groups}
    testbase.assert_equal("batch" in by_name, True)
    testbase.assert_equal(by_name["batch"].memory_bytes, 1073741824)
    testbase.assert_equal(by_name["batch"].attached_pids, [111, 222])
    testbase.assert_equal(by_name["batch"].cpu_percent, None)
    testbase.assert_equal(by_name["batch"].is_active, True)
    testbase.assert_equal(by_name["worker"].memory_bytes, None)

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_group_usage
# ----------------------------------------------------------------------


def test_get_group_usage_parses_top_output():
    """
        ``get_group_usage`` must aggregate ``top`` snapshots
        per attached PID.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")

    existing_spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=536870912, attached_pids=[111, 222],
    )
    existing_body = _render_plist_body(
        spec=existing_spec, label_prefix="automojo-rc-",
    )
    _seed_cat(runner, path=plist_path, body=existing_body)

    runner.results[(
        "top", "-l", "1", "-stats", "pid,cpu,mem", "-pid", "111",
    )] = RunResult(
        exit_code=0,
        stdout="PID  %CPU MEM\n111  3.0  104857600\n",
        stderr="",
    )
    runner.results[(
        "top", "-l", "1", "-stats", "pid,cpu,mem", "-pid", "222",
    )] = RunResult(
        exit_code=0,
        stdout="PID  %CPU MEM\n222  1.5  52428800\n",
        stderr="",
    )

    manager = MacosResourceControlManager(runner=runner)
    usage = manager.get_group_usage(name="batch")

    testbase.assert_equal(usage.name, "batch")
    testbase.assert_equal(usage.process_count, 2)
    testbase.assert_equal(usage.memory_used_bytes, 104857600 + 52428800)
    testbase.assert_equal(usage.cpu_usage_seconds, None)
    testbase.assert_equal(usage.io_read_bytes, None)
    testbase.assert_equal(usage.io_write_bytes, None)
    return


def test_get_group_usage_unknown_group_raises():
    """
        ``get_group_usage`` must raise
        :class:`MacosResourceControlQueryError` with
        ``detail="unknown_group"`` when the plist file does
        not exist.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_path_for("batch"), body=None)

    manager = MacosResourceControlManager(runner=runner)
    raised: Optional[MacosResourceControlQueryError] = None
    try:
        manager.get_group_usage(name="batch")
    except MacosResourceControlQueryError as exc:
        raised = exc
    testbase.assert_not_equal(raised, None)
    testbase.assert_equal(raised.detail, "unknown_group")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs must pass ``elevated=True`` to the runner
        when ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")
    _seed_cat(runner, path=plist_path, body=None)

    config = MacosResourceControlManagerConfig(elevated=True)
    manager = MacosResourceControlManager(runner=runner, config=config)

    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    manager.create_group(spec=spec)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "launchctl")
    ]
    for call in mutating:
        testbase.assert_equal(call["elevated"], True)

    # The cat probe must always be elevated=False.
    cat_calls = [c for c in runner.calls if c["args"][0] == "cat"]
    for call in cat_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_reads_never_elevate():
    """
        Read verbs must always pass ``elevated=False`` to the
        runner regardless of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/Library/LaunchDaemons")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    config = MacosResourceControlManagerConfig(elevated=True)
    manager = MacosResourceControlManager(runner=runner, config=config)

    groups = manager.list_groups()
    testbase.assert_equal(groups, [])

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing dispatched command must surface its
        ``exit_code``, ``stderr``, and ``command`` on the
        FAILED result.
    """
    runner = _MockCommandRunner()
    plist_path = _path_for("batch")
    _seed_cat(runner, path=plist_path, body=None)

    runner.results[("tee", plist_path)] = RunResult(
        exit_code=1, stdout="",
        stderr="tee: /Library/LaunchDaemons/...: Permission denied\n",
    )

    manager = MacosResourceControlManager(runner=runner)
    spec = MacosResourceGroupSpec(name="batch", memory_bytes=1073741824)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, MacosResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("Permission denied" in result.stderr, True)
    testbase.assert_equal(result.command, ["tee", plist_path])
    testbase.assert_equal(result.message, "plist_write_failed")
    return


# ----------------------------------------------------------------------
# Pure parser tests
# ----------------------------------------------------------------------


def test_parse_plist_helpers():
    """
        ``_parse_plist_memory_bytes`` and
        ``_parse_plist_attached_pids`` must round-trip the
        rendered plist body.
    """
    spec = MacosResourceGroupSpec(
        name="batch", memory_bytes=2147483648, attached_pids=[10, 20, 30],
    )
    body = _render_plist_body(spec=spec, label_prefix="automojo-rc-")

    parsed_mem = _parse_plist_memory_bytes(body=body)
    testbase.assert_equal(parsed_mem, 2147483648)

    parsed_pids = _parse_plist_attached_pids(body=body)
    testbase.assert_equal(parsed_pids, [10, 20, 30])
    return


def test_parse_top_output_handles_byte_form():
    """
        ``_parse_top_output`` must accept the
        ``-stats mem`` byte-form output.
    """
    body = "PID %CPU MEM\n12345 5.0 104857600\n"
    cpu, mem = _parse_top_output(stdout=body)
    testbase.assert_equal(cpu, 5.0)
    testbase.assert_equal(mem, 104857600)
    return
