"""
    Tests for :class:`MacosVirtualizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  Reads (``virsh dominfo``, ``virsh dumpxml``,
    ``virsh snapshot-list``, ``virsh list``, ``which``) are exercised
    via seeded payloads; no live ``virsh`` / ``virt-install`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.virtualizationmanagement import (
    MacosVirtBackend,
    MacosVirtualizationActionStatus,
    MacosVirtualizationManager,
    MacosVirtualizationManagerConfig,
    MacosVirtualizationQueryError,
    MacosVmDiskSpec,
    MacosVmSpec,
    MacosVmState,
)


_VIRSH_BIN = "/opt/homebrew/bin/virsh"
_VIRT_INSTALL_BIN = "/opt/homebrew/bin/virt-install"


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


_DOMINFO_RUNNING = (
    "Id:             3\n"
    "Name:           webvm\n"
    "UUID:           aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
    "OS Type:        hvm\n"
    "State:          running\n"
    "CPU(s):         2\n"
    "Max memory:     2097152 KiB\n"
    "Used memory:    1048576 KiB\n"
)

_DOMINFO_STOPPED = (
    "Id:             -\n"
    "Name:           webvm\n"
    "UUID:           aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
    "OS Type:        hvm\n"
    "State:          shut off\n"
    "CPU(s):         2\n"
    "Max memory:     2097152 KiB\n"
)

_DOMINFO_PAUSED = (
    "Id:             4\n"
    "Name:           webvm\n"
    "UUID:           aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n"
    "OS Type:        hvm\n"
    "State:          paused\n"
    "CPU(s):         2\n"
    "Max memory:     2097152 KiB\n"
)

_DUMPXML_WITH_DISK = (
    "<domain type='qemu'>\n"
    "  <name>webvm</name>\n"
    "  <devices>\n"
    "    <disk type='file' device='disk'>\n"
    "      <source file='/Users/me/vm/webvm.qcow2'/>\n"
    "      <target dev='vda' bus='virtio'/>\n"
    "    </disk>\n"
    "  </devices>\n"
    "</domain>\n"
)

_DUMPXML_NO_DISK = (
    "<domain type='qemu'>\n"
    "  <name>webvm</name>\n"
    "  <devices/>\n"
    "</domain>\n"
)

_SNAPSHOT_LIST_TEXT = (
    " Name        Creation Time              State\n"
    "------------------------------------------------------------\n"
    " preupgrade  2024-01-01 12:00:00 +0000  shutoff\n"
    " checkpoint  2024-02-15 09:30:11 +0000  running\n"
)

_SNAPSHOT_LIST_EMPTY = (
    " Name        Creation Time              State\n"
    "------------------------------------------------------------\n"
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _virsh_argv(*tokens: str) -> tuple:
    """
        Build a runner-key tuple for a ``virsh`` invocation that
        carries the default ``--connect`` flag and the explicit
        absolute path.
    """
    rtnval = (_VIRSH_BIN, "--connect", "qemu:///system") + tuple(tokens)
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = True,
    dry_run: bool = False,
    backend: MacosVirtBackend = MacosVirtBackend.LIBVIRT,
    virsh_path: Optional[str] = _VIRSH_BIN,
    virt_install_path: Optional[str] = _VIRT_INSTALL_BIN,
) -> MacosVirtualizationManager:
    """
        Construct a manager with the canonical config used in tests.
    """
    config = MacosVirtualizationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        backend=backend,
        virsh_path=virsh_path,
        virt_install_path=virt_install_path,
    )
    rtnval = MacosVirtualizationManager(runner=runner, config=config)
    return rtnval


def _seed_dominfo_running(runner: _MockCommandRunner, name: str) -> None:
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    return


def _seed_dominfo_stopped(runner: _MockCommandRunner, name: str) -> None:
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_STOPPED, stderr="",
    )
    return


def _seed_dominfo_paused(runner: _MockCommandRunner, name: str) -> None:
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_PAUSED, stderr="",
    )
    return


def _seed_dominfo_missing(runner: _MockCommandRunner, name: str) -> None:
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=1,
        stdout="",
        stderr="error: failed to get domain '{}'".format(name),
    )
    return


# ----------------------------------------------------------------------
# list_vms
# ----------------------------------------------------------------------


def test_list_vms_parses_virsh_output():
    """
        list_vms parses ``virsh list --all --name`` and joins per-VM
        ``virsh dominfo`` payloads.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=0, stdout="webvm\nbatchvm\n\n", stderr="",
    )
    runner.results[_virsh_argv("dominfo", "webvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    runner.results[_virsh_argv("dominfo", "batchvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_STOPPED, stderr="",
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_vms()

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(by_name["webvm"].state, MacosVmState.RUNNING)
    testbase.assert_equal(by_name["webvm"].cpu_count, 2)
    testbase.assert_equal(by_name["webvm"].memory_mb, 2048)
    testbase.assert_equal(by_name["batchvm"].state, MacosVmState.STOPPED)
    return


def test_list_vms_raises_on_unexpected_failure():
    """
        list_vms raises :class:`MacosVirtualizationQueryError` when
        ``virsh list`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=1, stdout="", stderr="connect failed",
    )

    manager = _make_manager(runner=runner)
    raised = False
    try:
        manager.list_vms()
    except MacosVirtualizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_reads_never_elevate():
    """
        Read verbs always pass ``elevated=False`` to the runner.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=0, stdout="webvm\n", stderr="",
    )
    runner.results[_virsh_argv("dominfo", "webvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    manager.list_vms()
    manager.get_vm_info(name="webvm")
    manager.list_snapshots(vm="webvm")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_vm_info
# ----------------------------------------------------------------------


def test_get_vm_info_returns_none_for_unknown_vm():
    """
        get_vm_info returns None when the VM is not defined.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.get_vm_info(name="ghostvm")
    testbase.assert_equal(result, None)
    return


# ----------------------------------------------------------------------
# create_vm
# ----------------------------------------------------------------------


def test_create_vm_invokes_virt_install():
    """
        create_vm dispatches ``virt-install`` with the rendered argv.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    spec = MacosVmSpec(
        name="webvm",
        cpu_count=2,
        memory_mb=2048,
        disk_size_gb=20,
        os_variant="ubuntu22.04",
        network="network=default",
        graphics="none",
    )
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)

    install_calls = [
        c for c in runner.calls if c["args"][0] == _VIRT_INSTALL_BIN
    ]
    testbase.assert_equal(len(install_calls), 1)
    argv = install_calls[0]["args"]
    testbase.assert_equal(argv[argv.index("--name") + 1], "webvm")
    testbase.assert_equal(argv[argv.index("--memory") + 1], "2048")
    testbase.assert_equal(argv[argv.index("--vcpus") + 1], "2")
    testbase.assert_equal(argv[argv.index("--disk") + 1], "size=20")
    testbase.assert_equal("--noautoconsole" in argv, True)
    testbase.assert_equal(install_calls[0]["elevated"], True)
    return


def test_create_vm_skipped_when_present():
    """
        create_vm returns SKIPPED when a VM with the same name already
        exists.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    spec = MacosVmSpec(name="webvm", memory_mb=2048, cpu_count=2)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    install_calls = [
        c for c in runner.calls if c["args"][0] == _VIRT_INSTALL_BIN
    ]
    testbase.assert_equal(install_calls, [])
    return


def test_create_vm_dry_run():
    """
        create_vm returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)
    spec = MacosVmSpec(name="webvm", memory_mb=1024, cpu_count=1)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], _VIRT_INSTALL_BIN)
    return


# ----------------------------------------------------------------------
# delete_vm / lifecycle
# ----------------------------------------------------------------------


def test_delete_vm_invokes_virsh_undefine_and_carries_advisory():
    """
        delete_vm dispatches ``virsh undefine --remove-all-storage``
        and records a data-loss advisory.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.delete_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    undefine_calls = [
        c for c in runner.calls
        if c["args"][:1] == [_VIRSH_BIN] and "undefine" in c["args"]
    ]
    testbase.assert_equal(len(undefine_calls), 1)
    testbase.assert_equal(
        "--remove-all-storage" in undefine_calls[0]["args"], True,
    )
    testbase.assert_equal(undefine_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("destroyed" in result.message, True)
    return


def test_delete_vm_skipped_when_absent():
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.delete_vm(name="ghostvm")
    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    return


def test_start_vm_skipped_when_running():
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    result = manager.start_vm(name="webvm")
    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    return


def test_start_vm_invokes_virsh_when_stopped():
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    start_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("start", "webvm"))
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["elevated"], True)
    return


def test_stop_vm_force_invokes_virsh_destroy():
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    forced = manager.stop_vm(name="webvm", force=True)
    testbase.assert_equal(forced.status, MacosVirtualizationActionStatus.SUCCESS)
    destroy_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("destroy", "webvm"))
    ]
    testbase.assert_equal(len(destroy_calls), 1)

    shutdown_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("shutdown", "webvm"))
    ]
    testbase.assert_equal(shutdown_calls, [])
    return


def test_stop_vm_graceful_invokes_virsh_shutdown():
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.stop_vm(name="webvm", force=False)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    shutdown_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("shutdown", "webvm"))
    ]
    testbase.assert_equal(len(shutdown_calls), 1)
    return


def test_pause_vm_invokes_virsh_suspend():
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.pause_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    suspend_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("suspend", "webvm"))
    ]
    testbase.assert_equal(len(suspend_calls), 1)
    testbase.assert_equal(suspend_calls[0]["elevated"], True)
    return


def test_resume_vm_invokes_virsh_resume():
    runner = _MockCommandRunner()
    _seed_dominfo_paused(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.resume_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    resume_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("resume", "webvm"))
    ]
    testbase.assert_equal(len(resume_calls), 1)
    testbase.assert_equal(resume_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# attach_disk / detach_disk
# ----------------------------------------------------------------------


def test_attach_disk_invokes_virsh_attach_disk():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    disk = MacosVmDiskSpec(
        path="/Users/me/vm/extra.qcow2",
        target="vdb",
        bus="virtio",
    )
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    attach_calls = [
        c for c in runner.calls
        if c["args"][:1] == [_VIRSH_BIN] and "attach-disk" in c["args"]
    ]
    testbase.assert_equal(len(attach_calls), 1)
    argv = attach_calls[0]["args"]
    testbase.assert_equal("/Users/me/vm/extra.qcow2" in argv, True)
    testbase.assert_equal("vdb" in argv, True)
    testbase.assert_equal("--persistent" in argv, True)
    testbase.assert_equal("--targetbus" in argv, True)
    testbase.assert_equal(attach_calls[0]["elevated"], True)
    return


def test_attach_disk_skipped_when_attached():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_WITH_DISK, stderr="",
    )

    manager = _make_manager(runner=runner)
    disk = MacosVmDiskSpec(
        path="/Users/me/vm/webvm.qcow2",
        target="vda",
    )
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    return


def test_detach_disk_invokes_virsh_detach_disk():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_WITH_DISK, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.detach_disk(vm="webvm", disk_id="vda")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    detach_calls = [
        c for c in runner.calls if "detach-disk" in c["args"]
    ]
    testbase.assert_equal(len(detach_calls), 1)
    testbase.assert_equal("--persistent" in detach_calls[0]["args"], True)
    testbase.assert_equal(detach_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("detached" in result.message, True)
    return


def test_detach_disk_skipped_when_not_attached():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )

    manager = _make_manager(runner=runner)
    result = manager.detach_disk(vm="webvm", disk_id="vdz")
    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------


def test_snapshot_vm_invokes_virsh_snapshot_create_as():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_EMPTY, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    create_calls = [
        c for c in runner.calls if "snapshot-create-as" in c["args"]
    ]
    testbase.assert_equal(len(create_calls), 1)
    testbase.assert_equal(create_calls[0]["elevated"], True)
    testbase.assert_equal(result.snapshot_name, "preupgrade")
    return


def test_snapshot_vm_skipped_when_named_snapshot_exists():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")
    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SKIPPED)
    return


def test_restore_snapshot_invokes_virsh_snapshot_revert():
    runner = _MockCommandRunner()

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.restore_snapshot(
        vm="webvm", snapshot_name="preupgrade",
    )

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.SUCCESS)
    revert_calls = [
        c for c in runner.calls if "snapshot-revert" in c["args"]
    ]
    testbase.assert_equal(len(revert_calls), 1)
    testbase.assert_equal(revert_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("reverted" in result.message, True)
    return


def test_list_snapshots_parses_virsh_snapshot_list_output():
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_snapshots(vm="webvm")

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(
        by_name["preupgrade"].vm_state, MacosVmState.STOPPED,
    )
    testbase.assert_equal(
        by_name["checkpoint"].vm_state, MacosVmState.RUNNING,
    )
    return


# ----------------------------------------------------------------------
# Backend / virsh discovery / failure surfacing / elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_EMPTY, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    manager.start_vm(name="webvm")
    manager.attach_disk(
        vm="webvm",
        disk_spec=MacosVmDiskSpec(path="/tmp/x.qcow2", target="vdb"),
    )
    manager.snapshot_vm(vm="webvm", snapshot_name="snap1")

    write_calls = [
        c for c in runner.calls
        if any(
            token in c["args"]
            for token in ("start", "attach-disk", "snapshot-create-as")
        )
    ]
    testbase.assert_equal(len(write_calls) > 0, True)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_failed_command_surfaces_stderr():
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")
    runner.results[_virsh_argv("start", "webvm")] = RunResult(
        exit_code=1, stdout="", stderr="error: failed to start domain webvm",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr, "error: failed to start domain webvm",
    )
    return


def test_hyperkit_backend_returns_failed():
    """
        HYPERKIT backend short-circuits with
        ``message="hyperkit_deprecated"`` on every verb.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(
        runner=runner, backend=MacosVirtBackend.HYPERKIT,
    )
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.message, "hyperkit_deprecated")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_virsh_not_found_returns_failed():
    """
        When ``virsh_path`` is None and ``which virsh`` reports no
        result, every verb returns FAILED with
        ``message="virsh_not_found"``.
    """
    runner = _MockCommandRunner()
    runner.results[("which", "virsh")] = RunResult(
        exit_code=1, stdout="", stderr="virsh not found",
    )
    manager = _make_manager(
        runner=runner,
        virsh_path=None,
        virt_install_path=_VIRT_INSTALL_BIN,
    )
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, MacosVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.message, "virsh_not_found")
    return


def test_virsh_path_resolved_via_which():
    """
        When ``virsh_path`` is None and ``which`` reports a path, the
        manager caches it and uses it on every call.
    """
    runner = _MockCommandRunner()
    runner.results[("which", "virsh")] = RunResult(
        exit_code=0, stdout="{}\n".format(_VIRSH_BIN), stderr="",
    )
    runner.results[_virsh_argv("dominfo", "webvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )

    manager = _make_manager(
        runner=runner,
        virsh_path=None,
        virt_install_path=_VIRT_INSTALL_BIN,
    )
    info = manager.get_vm_info(name="webvm")
    testbase.assert_equal(info is not None, True)
    testbase.assert_equal(info.state, MacosVmState.RUNNING)
    which_calls = [c for c in runner.calls if c["args"][0] == "which"]
    testbase.assert_equal(len(which_calls), 1)
    return
