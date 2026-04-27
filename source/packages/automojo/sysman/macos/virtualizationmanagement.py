"""
    macOS virtualization-management feature.

    Implements the cross-OS virtualization-management contract for
    macOS using Homebrew-installed QEMU / libvirt through ``virsh`` and
    ``virt-install``.  Every operation is dispatched via the injected
    :class:`ICommandRunner`; this module never constructs its own
    subprocess.

    Backend selection:
        :class:`MacosVirtBackend` is the explicit selector recorded on
        :class:`MacosVirtualizationManagerConfig`.  The supported value
        is ``LIBVIRT`` (Homebrew QEMU + libvirt).  ``HYPERKIT`` is
        accepted as a legacy / deprecated selector — it returns a typed
        ``FAILED`` result with ``message="hyperkit_deprecated"`` from
        every verb because Apple removed HyperKit's hypervisor support
        in macOS 12+ and the Docker Desktop fork has been retired.

        Apple's **Virtualization framework** (``VZ`` family) is
        deliberately **out of scope**: its API is Swift-only and
        binding it from Python via ``PyObjC`` would introduce a
        runtime dependency that does not match the rest of this
        package.  Callers that need Apple Virtualization should use a
        separate, Swift-driven package.

    Required runtime:
        ``brew install libvirt`` plus a running ``libvirtd`` daemon
        (``brew services start libvirt``).  Homebrew installs libvirt
        at ``/opt/homebrew/bin/virsh`` on Apple-Silicon hosts and at
        ``/usr/local/bin/virsh`` on Intel hosts.  When
        :attr:`MacosVirtualizationManagerConfig.virsh_path` is
        ``None``, the manager probes ``which virsh`` once via the
        runner; when ``which`` reports nothing, every verb returns a
        typed FAILED result with ``message="virsh_not_found"``.

    Connection URI:
        Every ``virsh`` invocation is prefixed with ``--connect <uri>``
        sourced from
        :attr:`MacosVirtualizationManagerConfig.default_connection`
        (default ``"qemu:///system"``).  An empty string suppresses the
        flag so libvirt's own URI-resolution rules apply.

    Elevation policy:
        Every write verb honours :attr:`FeatureConfig.elevated`.  Read
        verbs and idempotency probes never elevate.  On macOS, libvirt
        runs as the local user when configured to use the per-user
        ``qemu:///session`` URI; ``qemu:///system`` typically requires
        root.  Both situations are represented by ``elevated=True``.

    Stop semantics:
        ``stop_vm(force=False)`` issues ``virsh shutdown``;
        ``stop_vm(force=True)`` issues ``virsh destroy`` (a hard
        power-off that leaves the persistent definition intact).
"""

import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level command tokens and defaults
# ----------------------------------------------------------------------


_VIRSH_FALLBACK = "virsh"
_VIRT_INSTALL_FALLBACK = "virt-install"
_WHICH = "which"

_DEFAULT_CONNECTION_URI = "qemu:///system"

# Common libvirt domain-state strings.  ``virsh dominfo`` uses the
# human form ``shut off``; ``virsh snapshot-list`` collapses the
# space to ``shutoff``.  Both variants map onto
# :class:`MacosVmState.STOPPED`.
_LIBVIRT_RUNNING = "running"
_LIBVIRT_PAUSED = "paused"
_LIBVIRT_SHUT_OFF = "shut off"
_LIBVIRT_SHUT_OFF_NOSPACE = "shut-off"
_LIBVIRT_SHUTOFF_COLLAPSED = "shutoff"
_LIBVIRT_PMSUSPENDED = "pmsuspended"


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


MacosVirtualizationActionStatus = ActionStatus


class MacosVirtBackend(Enum):
    """
        Selector for the virtualization backend driving the manager.

        ``LIBVIRT`` is the supported value (Homebrew QEMU + libvirt).
        ``HYPERKIT`` is accepted as a legacy selector but returns a
        typed FAILED for every verb because Apple's HyperKit fork is
        deprecated.  Apple's Virtualization framework (Swift-only) is
        intentionally absent from this enum and out of scope.
    """
    LIBVIRT  = "libvirt"
    HYPERKIT = "hyperkit"


class MacosVmState(Enum):
    """
        Coarse power / lifecycle state for a macOS-hosted libvirt VM.
    """
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"
    OTHER     = "other"


@dataclass
class MacosVirtualizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosVirtualizationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backend: Virtualization backend selector.  Defaults to
                        :attr:`MacosVirtBackend.LIBVIRT`.  Anything else
                        causes every verb to return a typed FAILED
                        result with ``message="hyperkit_deprecated"``.
        :param virsh_path: Absolute path to ``virsh``.  ``None`` (the
                           default) triggers a one-shot ``which virsh``
                           probe at first use; ``virsh`` reachable via
                           PATH satisfies the probe.  When the probe
                           comes up empty, every verb returns FAILED
                           with ``message="virsh_not_found"``.
        :param virt_install_path: Absolute path to ``virt-install``.
                                  ``None`` triggers the same
                                  ``which`` probe.
        :param default_connection: libvirt URI passed via
                                   ``--connect``.  Defaults to
                                   ``"qemu:///system"``.  An empty
                                   string suppresses the flag so
                                   libvirt's default URI-resolution
                                   logic applies.
    """
    backend: MacosVirtBackend = MacosVirtBackend.LIBVIRT
    virsh_path: Optional[str] = None
    virt_install_path: Optional[str] = None
    default_connection: str = _DEFAULT_CONNECTION_URI


@dataclass
class MacosVirtualizationActionResult(FeatureActionResult):
    """
        Result of a virtualization-management write operation.

        :param vm_name: VM (libvirt domain) name targeted by the
                        operation, when applicable.
        :param snapshot_name: Snapshot name targeted by the operation,
                              when applicable.
        :param message: Human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        or backend-availability messages).
    """
    vm_name: Optional[str] = None
    snapshot_name: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosVmSpec:
    """
        Specification for :meth:`MacosVirtualizationManager.create_vm`.

        :param name: Domain name.  Becomes ``virt-install --name``.
        :param cpu_count: Number of vCPUs (``--vcpus``).  Defaults to 1.
        :param memory_mb: Allocated memory in MiB (``--memory``).
                          Defaults to 1024.
        :param disk_size_gb: When set and ``disk_path`` is None,
                             ``virt-install`` is asked to create a
                             qcow2 of this size in the default storage
                             pool (``--disk size=<gb>``).
        :param disk_path: When set, used as ``--disk path=<disk_path>``
                          to attach an existing disk image.
        :param os_variant: ``--os-variant`` token.  Optional.
        :param network: ``--network`` argument (e.g.
                        ``"network=default"``).
        :param iso_path: When set, used as ``--cdrom`` to boot from an
                         installation ISO.
        :param graphics: ``--graphics`` argument.  Defaults to
                         ``"none"`` for headless installs.
        :param boot_kind: Reserved for cross-OS-spec parity (Windows
                          calls this *generation*; macOS / Linux ignore
                          it).  Defaults to 1.
    """
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: Optional[int] = None
    disk_path: Optional[str] = None
    os_variant: Optional[str] = None
    network: Optional[str] = None
    iso_path: Optional[str] = None
    graphics: str = "none"
    boot_kind: int = 1


@dataclass
class MacosVmInfo:
    """
        Snapshot of a single VM's configuration and state.

        :param name: Domain name.
        :param state: Coarse power state.
        :param cpu_count: Configured vCPUs (``CPU(s)`` from
                          ``virsh dominfo``).  ``None`` when unparseable.
        :param memory_mb: Configured memory in MiB.  ``None`` when
                          unparseable.
        :param uptime_seconds: Reserved.  libvirt does not report a
                               numeric uptime; this is None unless a
                               richer probe is added.
        :param os_type: ``OS Type`` from ``virsh dominfo`` when
                        reported.
        :param vm_id: Domain UUID from ``virsh dominfo`` when reported.
    """
    name: str
    state: MacosVmState
    cpu_count: Optional[int] = None
    memory_mb: Optional[int] = None
    uptime_seconds: Optional[int] = None
    os_type: Optional[str] = None
    vm_id: Optional[str] = None


@dataclass
class MacosVmDiskSpec:
    """
        Specification for :meth:`MacosVirtualizationManager.attach_disk`.

        :param path: Backing-file path on the host that libvirt will
                     attach.
        :param target: Guest-side device label (``"vdb"``,
                       ``"sdc"``).  Defaults to ``"vdb"`` when the
                       caller leaves it None.
        :param controller_type: Reserved for cross-OS-spec parity
                                (Windows uses ``ControllerType``);
                                ignored on macOS.
        :param bus: ``--targetbus`` argument (``"virtio"``,
                    ``"sata"``).  Optional.
    """
    path: str
    target: Optional[str] = None
    controller_type: Optional[str] = None
    bus: Optional[str] = None


@dataclass
class MacosSnapshotInfo:
    """
        Single snapshot entry returned by :meth:`list_snapshots`.

        :param name: Snapshot name.
        :param created: Creation timestamp string as reported by
                        ``virsh snapshot-list``.  ``"-"`` is normalised
                        to an empty string.
        :param description: Optional description (currently always
                            ``None``; the basic ``snapshot-list`` form
                            does not carry it).
        :param parent: Parent snapshot name when reported.  ``None``
                       for top-level snapshots.
        :param vm_state: Domain state captured by the snapshot when
                         reported.
    """
    name: str
    created: str
    description: Optional[str] = None
    parent: Optional[str] = None
    vm_state: Optional[MacosVmState] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosVirtualizationQueryError(Exception):
    """
        Raised when a virtualization read fails for a reason other than
        the target VM / snapshot simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Virtualization query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# MacosVirtualizationManager
# ----------------------------------------------------------------------


class MacosVirtualizationManager(BaseFeatureManager):
    """
        macOS Homebrew-libvirt virtualization manager.  Every verb is
        dispatched through the injected :class:`ICommandRunner`.  See
        the module docstring for backend, virsh-discovery, connection-
        URI, and elevation rules.
    """

    result_class = MacosVirtualizationActionResult
    status_class = MacosVirtualizationActionStatus
    feature_name = "virtualization-management"

    def __init__(self, runner, config=None) -> None:
        """
            :param runner: Injected command runner.
            :param config: Optional
                           :class:`MacosVirtualizationManagerConfig`.
        """
        super().__init__(runner=runner, config=config)
        # Cached resolved virsh / virt-install paths.  ``None`` means
        # "not yet probed"; the empty string means "probed and missing".
        self._resolved_virsh: Optional[str] = None
        self._resolved_virt_install: Optional[str] = None
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_vms(self) -> list:
        """
            Enumerate every defined VM via ``virsh list --all --name``
            plus per-name ``virsh dominfo``.

            Reads do not elevate.

            :returns: One :class:`MacosVmInfo` per defined VM.  Empty
                      list when no VMs are defined or when the backend
                      / virsh path is not available.
            :raises MacosVirtualizationQueryError: When ``virsh list``
                                                   fails for an
                                                   unexpected reason.
        """
        if not self._backend_supported():
            rtnval: list = []
            return rtnval
        if self._resolve_virsh() is None:
            rtnval = []
            return rtnval

        list_command = self._virsh_argv(args=["list", "--all", "--name"])
        list_run = self._runner.run(list(list_command), elevated=False)
        if list_run.exit_code != 0:
            raise MacosVirtualizationQueryError(
                command=list_command,
                exit_code=list_run.exit_code,
                stderr=list_run.stderr,
            )

        names = _parse_virsh_list_names(stdout=list_run.stdout)
        rtnval = []
        for name in names:
            info = self._read_dominfo(name=name)
            if info is not None:
                rtnval.append(info)
        return rtnval

    def get_vm_info(self, name: str) -> Optional[MacosVmInfo]:
        """
            Return the :class:`MacosVmInfo` for ``name``, or ``None``
            when the VM is not defined.
        """
        if not self._backend_supported():
            rtnval: Optional[MacosVmInfo] = None
            return rtnval
        if self._resolve_virsh() is None:
            rtnval = None
            return rtnval

        info = self._read_dominfo(name=name, raise_on_unknown=True)
        rtnval = info
        return rtnval

    def list_snapshots(self, vm: str) -> list:
        """
            Enumerate snapshots for ``vm`` via ``virsh snapshot-list``.
        """
        if not self._backend_supported():
            rtnval: list = []
            return rtnval
        if self._resolve_virsh() is None:
            rtnval = []
            return rtnval

        command = self._virsh_argv(args=["snapshot-list", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosVirtualizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_virsh_snapshot_list(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — VM lifecycle
    # ------------------------------------------------------------------

    def create_vm(
        self,
        spec: MacosVmSpec,
    ) -> MacosVirtualizationActionResult:
        """
            Provision a fresh VM via ``virt-install``.  Idempotent on
            VM name.
        """
        backend_failure = self._guard_backend(
            vm_name=spec.name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virt_install_failure = self._guard_virt_install(
            vm_name=spec.name, snapshot_name=None,
        )
        if virt_install_failure is not None:
            return virt_install_failure

        command = self._build_virt_install_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=spec.name,
            )
            return dry

        existing = self._read_dominfo(name=spec.name)
        if existing is not None:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=spec.name,
                message="VM {!r} already exists".format(spec.name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=spec.name,
        )
        return rtnval

    def delete_vm(self, name: str) -> MacosVirtualizationActionResult:
        """
            Undefine the VM and remove its backing storage.
        """
        backend_failure = self._guard_backend(
            vm_name=name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=name, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(
            args=["undefine", name, "--remove-all-storage"],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is None:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is not defined".format(name),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        if result.status == MacosVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was undefined; --remove-all-storage destroyed "
                "every backing volume associated with the domain"
            ).format(name)
            result.message = advisory
            result.detail = advisory
        return result

    def start_vm(self, name: str) -> MacosVirtualizationActionResult:
        """
            ``virsh start <name>``.  SKIPPED when already running.
        """
        backend_failure = self._guard_backend(
            vm_name=name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=name, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(args=["start", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == MacosVmState.RUNNING:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already running".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def stop_vm(
        self,
        name: str,
        force: bool = False,
    ) -> MacosVirtualizationActionResult:
        """
            ``virsh shutdown`` (graceful) or ``virsh destroy`` (force).
            SKIPPED when the VM is already stopped.
        """
        backend_failure = self._guard_backend(
            vm_name=name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=name, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        if force:
            verb = "destroy"
        else:
            verb = "shutdown"
        command = self._virsh_argv(args=[verb, name])

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == MacosVmState.STOPPED:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already stopped".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def pause_vm(self, name: str) -> MacosVirtualizationActionResult:
        """
            ``virsh suspend <name>``.  SKIPPED when already paused.
        """
        backend_failure = self._guard_backend(
            vm_name=name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=name, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(args=["suspend", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == MacosVmState.PAUSED:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already paused".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    def resume_vm(self, name: str) -> MacosVirtualizationActionResult:
        """
            ``virsh resume <name>``.  SKIPPED when already running.
        """
        backend_failure = self._guard_backend(
            vm_name=name, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=name, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(args=["resume", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == MacosVmState.RUNNING:
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already running".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — disks
    # ------------------------------------------------------------------

    def attach_disk(
        self,
        vm: str,
        disk_spec: MacosVmDiskSpec,
    ) -> MacosVirtualizationActionResult:
        """
            ``virsh attach-disk <vm> <source> <target> --persistent``.
            SKIPPED when the backing path is already in the VM XML.
        """
        backend_failure = self._guard_backend(
            vm_name=vm, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=vm, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        if disk_spec.target is None or len(disk_spec.target) == 0:
            target = "vdb"
        else:
            target = disk_spec.target

        attach_args = [
            "attach-disk", vm, disk_spec.path, target, "--persistent",
        ]
        if disk_spec.bus is not None and len(disk_spec.bus) > 0:
            attach_args.extend(["--targetbus", disk_spec.bus])
        command = self._virsh_argv(args=attach_args)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if self._disk_path_in_xml(vm=vm, path=disk_spec.path):
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is already attached to VM {!r}".format(
                        disk_spec.path, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command, vm_name=vm,
        )
        return rtnval

    def detach_disk(
        self,
        vm: str,
        disk_id: str,
    ) -> MacosVirtualizationActionResult:
        """
            ``virsh detach-disk <vm> <disk_id> --persistent``.
            SKIPPED when the disk is not attached.
        """
        backend_failure = self._guard_backend(
            vm_name=vm, snapshot_name=None,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=vm, snapshot_name=None,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(
            args=["detach-disk", vm, disk_id, "--persistent"],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if not self._disk_attached(vm=vm, disk_id=disk_id):
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is not attached to VM {!r}".format(
                        disk_id, vm,
                    )
                ),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command, vm_name=vm,
        )
        if result.status == MacosVirtualizationActionStatus.SUCCESS:
            advisory = (
                "disk {!r} was detached from VM {!r}; the backing "
                "file was NOT removed".format(disk_id, vm)
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Write verbs — snapshots
    # ------------------------------------------------------------------

    def snapshot_vm(
        self,
        vm: str,
        snapshot_name: str,
    ) -> MacosVirtualizationActionResult:
        """
            ``virsh snapshot-create-as <vm> <snapshot_name>``.  SKIPPED
            when a snapshot with the same name already exists.
        """
        backend_failure = self._guard_backend(
            vm_name=vm, snapshot_name=snapshot_name,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=vm, snapshot_name=snapshot_name,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(
            args=["snapshot-create-as", vm, snapshot_name],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        if self._snapshot_exists(vm=vm, snapshot_name=snapshot_name):
            skipped = self._build_result(
                status=MacosVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
                message=(
                    "snapshot {!r} already exists for VM {!r}".format(
                        snapshot_name, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def restore_snapshot(
        self,
        vm: str,
        snapshot_name: str,
    ) -> MacosVirtualizationActionResult:
        """
            ``virsh snapshot-revert <vm> <snapshot_name>``.
            Unconditional per the contract; populates a data-loss
            advisory on SUCCESS.
        """
        backend_failure = self._guard_backend(
            vm_name=vm, snapshot_name=snapshot_name,
        )
        if backend_failure is not None:
            return backend_failure
        virsh_failure = self._guard_virsh(
            vm_name=vm, snapshot_name=snapshot_name,
        )
        if virsh_failure is not None:
            return virsh_failure

        command = self._virsh_argv(
            args=["snapshot-revert", vm, snapshot_name],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        result = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        if result.status == MacosVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was reverted to snapshot {!r}; any state "
                "written after the snapshot was discarded".format(
                    vm, snapshot_name,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosVirtualizationManagerConfig:
        """
            Return the manager's config typed.  Falls back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, MacosVirtualizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosVirtualizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _backend_supported(self) -> bool:
        """
            Return True when the configured backend is supported
            (``LIBVIRT`` only).
        """
        backend = self._typed_config().backend
        rtnval = (backend == MacosVirtBackend.LIBVIRT)
        return rtnval

    def _guard_backend(
        self,
        *,
        vm_name: Optional[str],
        snapshot_name: Optional[str],
    ) -> Optional[MacosVirtualizationActionResult]:
        """
            Build a typed FAILED result for a non-LIBVIRT backend.

            :returns: The failure result, or ``None`` when the backend
                      is supported.
        """
        if self._backend_supported():
            rtnval: Optional[MacosVirtualizationActionResult] = None
            return rtnval
        backend = self._typed_config().backend
        if backend == MacosVirtBackend.HYPERKIT:
            err_message = "hyperkit_deprecated"
        else:
            err_message = "unsupported_backend"
        rtnval = self._build_result(
            status=MacosVirtualizationActionStatus.FAILED,
            command=[],
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message=err_message,
        )
        return rtnval

    def _guard_virsh(
        self,
        *,
        vm_name: Optional[str],
        snapshot_name: Optional[str],
    ) -> Optional[MacosVirtualizationActionResult]:
        """
            Resolve ``virsh`` and short-circuit with a FAILED result
            when the binary is not on PATH.
        """
        path = self._resolve_virsh()
        if path is not None:
            rtnval: Optional[MacosVirtualizationActionResult] = None
            return rtnval
        rtnval = self._build_result(
            status=MacosVirtualizationActionStatus.FAILED,
            command=[],
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message="virsh_not_found",
        )
        return rtnval

    def _guard_virt_install(
        self,
        *,
        vm_name: Optional[str],
        snapshot_name: Optional[str],
    ) -> Optional[MacosVirtualizationActionResult]:
        """
            Resolve ``virt-install`` and short-circuit with a FAILED
            result when the binary is not on PATH.
        """
        path = self._resolve_virt_install()
        if path is not None:
            rtnval: Optional[MacosVirtualizationActionResult] = None
            return rtnval
        rtnval = self._build_result(
            status=MacosVirtualizationActionStatus.FAILED,
            command=[],
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message="virt_install_not_found",
        )
        return rtnval

    def _resolve_virsh(self) -> Optional[str]:
        """
            Resolve the absolute path to ``virsh``, caching the result.

            :returns: The path string, or ``None`` when not found.
        """
        config = self._typed_config()
        if config.virsh_path is not None and len(config.virsh_path) > 0:
            rtnval: Optional[str] = config.virsh_path
            return rtnval
        if self._resolved_virsh is not None:
            if len(self._resolved_virsh) == 0:
                rtnval = None
                return rtnval
            rtnval = self._resolved_virsh
            return rtnval
        path = self._which(binary="virsh")
        if path is None:
            self._resolved_virsh = ""
            rtnval = None
            return rtnval
        self._resolved_virsh = path
        rtnval = path
        return rtnval

    def _resolve_virt_install(self) -> Optional[str]:
        """
            Resolve the absolute path to ``virt-install``, caching the
            result.
        """
        config = self._typed_config()
        if (
            config.virt_install_path is not None
            and len(config.virt_install_path) > 0
        ):
            rtnval: Optional[str] = config.virt_install_path
            return rtnval
        if self._resolved_virt_install is not None:
            if len(self._resolved_virt_install) == 0:
                rtnval = None
                return rtnval
            rtnval = self._resolved_virt_install
            return rtnval
        path = self._which(binary="virt-install")
        if path is None:
            self._resolved_virt_install = ""
            rtnval = None
            return rtnval
        self._resolved_virt_install = path
        rtnval = path
        return rtnval

    def _which(self, *, binary: str) -> Optional[str]:
        """
            Run ``which <binary>`` through the runner without elevation
            and return the first non-empty stdout line.

            :param binary: The binary name to resolve.

            :returns: The absolute path, or ``None`` when not found.
        """
        command = [_WHICH, binary]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        for raw in run_result.stdout.splitlines():
            line = raw.strip()
            if len(line) > 0:
                rtnval = line
                return rtnval
        rtnval = None
        return rtnval

    def _virsh_argv(self, *, args: list) -> list:
        """
            Build the ``virsh`` argv prefix and append ``args``.
        """
        config = self._typed_config()
        path = self._resolve_virsh()
        if path is None:
            path = _VIRSH_FALLBACK
        prefix = [path]
        if len(config.default_connection) > 0:
            prefix.append("--connect")
            prefix.append(config.default_connection)
        rtnval = prefix + list(args)
        return rtnval

    def _build_virt_install_argv(
        self,
        *,
        spec: MacosVmSpec,
    ) -> list:
        """
            Render the ``virt-install`` argv for ``spec``.
        """
        config = self._typed_config()
        path = self._resolve_virt_install()
        if path is None:
            path = _VIRT_INSTALL_FALLBACK
        argv = [path]
        if len(config.default_connection) > 0:
            argv.append("--connect")
            argv.append(config.default_connection)

        argv.append("--name")
        argv.append(spec.name)
        argv.append("--memory")
        argv.append(str(spec.memory_mb))
        argv.append("--vcpus")
        argv.append(str(spec.cpu_count))

        disk_arg = _build_disk_argument(spec=spec)
        if disk_arg is not None:
            argv.append("--disk")
            argv.append(disk_arg)

        if spec.os_variant is not None and len(spec.os_variant) > 0:
            argv.append("--os-variant")
            argv.append(spec.os_variant)

        if spec.network is not None and len(spec.network) > 0:
            argv.append("--network")
            argv.append(spec.network)

        if spec.iso_path is not None and len(spec.iso_path) > 0:
            argv.append("--cdrom")
            argv.append(spec.iso_path)

        argv.append("--graphics")
        argv.append(spec.graphics)

        argv.append("--noautoconsole")

        rtnval = argv
        return rtnval

    def _read_dominfo(
        self,
        *,
        name: str,
        raise_on_unknown: bool = False,
    ) -> Optional[MacosVmInfo]:
        """
            Probe ``virsh dominfo <name>`` and parse the output into
            :class:`MacosVmInfo`.
        """
        command = self._virsh_argv(args=["dominfo", name])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if _stderr_looks_unknown_domain(stderr=run_result.stderr or ""):
                rtnval: Optional[MacosVmInfo] = None
                return rtnval
            if raise_on_unknown:
                raise MacosVirtualizationQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = None
            return rtnval

        rtnval = _parse_virsh_dominfo(
            stdout=run_result.stdout, name=name,
        )
        return rtnval

    def _disk_path_in_xml(self, *, vm: str, path: str) -> bool:
        """
            Return True when ``virsh dumpxml <vm>`` references ``path``
            in a ``<source file=...>`` element.
        """
        command = self._virsh_argv(args=["dumpxml", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _xml_contains_source_file(
            xml_text=run_result.stdout, path=path,
        )
        return rtnval

    def _disk_attached(self, *, vm: str, disk_id: str) -> bool:
        """
            Return True when ``disk_id`` matches a ``<target dev=...>``
            or a ``<source file=...>`` value in the VM's active XML.
        """
        command = self._virsh_argv(args=["dumpxml", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _xml_contains_disk_id(
            xml_text=run_result.stdout, disk_id=disk_id,
        )
        return rtnval

    def _snapshot_exists(
        self,
        *,
        vm: str,
        snapshot_name: str,
    ) -> bool:
        """
            Return True when ``virsh snapshot-list <vm>`` reports a
            snapshot whose name matches ``snapshot_name``.
        """
        command = self._virsh_argv(args=["snapshot-list", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        snapshots = _parse_virsh_snapshot_list(stdout=run_result.stdout)
        rtnval = False
        for entry in snapshots:
            if entry.name == snapshot_name:
                rtnval = True
                break
        return rtnval

    def _dispatch_virt_write(
        self,
        *,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
    ) -> MacosVirtualizationActionResult:
        """
            Dispatch a write command and wrap the result with the
            virtualization-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=vm_name,
            operation="virtualization_write",
            vm_name=vm_name,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosVirtualizationActionStatus,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosVirtualizationActionResult:
        """
            Construct a :class:`MacosVirtualizationActionResult`
            without invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = vm_name
        rtnval = MacosVirtualizationActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers / helpers
# ----------------------------------------------------------------------


def _build_disk_argument(*, spec: MacosVmSpec) -> Optional[str]:
    """
        Render the value for ``virt-install --disk`` from a spec.
    """
    if spec.disk_path is not None and len(spec.disk_path) > 0:
        rtnval: Optional[str] = "path={}".format(spec.disk_path)
        return rtnval
    if spec.disk_size_gb is not None and spec.disk_size_gb > 0:
        rtnval = "size={}".format(spec.disk_size_gb)
        return rtnval
    rtnval = None
    return rtnval


def _stderr_looks_unknown_domain(*, stderr: str) -> bool:
    """
        Heuristically detect a libvirt "domain not found" stderr.
    """
    lower = stderr.lower()
    if "failed to get domain" in lower:
        rtnval = True
        return rtnval
    if "domain not found" in lower:
        rtnval = True
        return rtnval
    if "no such domain" in lower:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _normalise_libvirt_state(*, state_text: str) -> MacosVmState:
    """
        Map a raw libvirt domain-state string onto :class:`MacosVmState`.
    """
    text = state_text.strip().lower()
    if text == _LIBVIRT_RUNNING:
        rtnval = MacosVmState.RUNNING
        return rtnval
    if text in (
        _LIBVIRT_SHUT_OFF,
        _LIBVIRT_SHUT_OFF_NOSPACE,
        _LIBVIRT_SHUTOFF_COLLAPSED,
    ):
        rtnval = MacosVmState.STOPPED
        return rtnval
    if text == _LIBVIRT_PAUSED:
        rtnval = MacosVmState.PAUSED
        return rtnval
    if text == _LIBVIRT_PMSUSPENDED:
        rtnval = MacosVmState.SUSPENDED
        return rtnval
    rtnval = MacosVmState.OTHER
    return rtnval


def _parse_virsh_list_names(*, stdout: str) -> list:
    """
        Parse ``virsh list --all --name`` output into a list of names.
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        rtnval.append(line)
    return rtnval


def _parse_virsh_dominfo(
    *,
    stdout: str,
    name: str,
) -> Optional[MacosVmInfo]:
    """
        Parse ``virsh dominfo`` output into :class:`MacosVmInfo`.
    """
    stripped = stdout.strip()
    if len(stripped) == 0:
        rtnval: Optional[MacosVmInfo] = None
        return rtnval

    fields: dict = {}
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    # Prefer the caller-supplied name as the canonical identifier;
    # libvirt's ``Name:`` field echoes the same value, but enumeration
    # paths feed each VM's name through here so the result must reflect
    # the caller's lookup key.
    if name is not None and len(name) > 0:
        info_name = name
    else:
        info_name = fields.get("Name", "")

    state_text = fields.get("State", "")
    state = _normalise_libvirt_state(state_text=state_text)

    cpu_text = fields.get("CPU(s)", "")
    if len(cpu_text) == 0:
        cpu_count: Optional[int] = None
    else:
        try:
            cpu_count = int(cpu_text)
        except ValueError:
            cpu_count = None

    memory_text = fields.get("Max memory", "")
    memory_mb: Optional[int] = None
    if len(memory_text) > 0:
        tokens = memory_text.split()
        if len(tokens) > 0:
            try:
                kib_value = int(tokens[0])
                memory_mb = int(kib_value / 1024)
            except ValueError:
                memory_mb = None

    os_type_raw = fields.get("OS Type", "")
    if len(os_type_raw) == 0:
        os_type: Optional[str] = None
    else:
        os_type = os_type_raw

    uuid_raw = fields.get("UUID", "")
    if len(uuid_raw) == 0:
        vm_id: Optional[str] = None
    else:
        vm_id = uuid_raw

    rtnval = MacosVmInfo(
        name=info_name,
        state=state,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        uptime_seconds=None,
        os_type=os_type,
        vm_id=vm_id,
    )
    return rtnval


def _parse_virsh_snapshot_list(*, stdout: str) -> list:
    """
        Parse ``virsh snapshot-list <vm>`` tabular output.

        Default output shape:

            Name                 Creation Time             State
            ------------------------------------------------------------
            snap1                2024-01-01 12:00:00 +0000 running
    """
    rtnval: list = []
    saw_separator = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue
        if line.lstrip().startswith("---"):
            saw_separator = True
            continue
        if not saw_separator:
            continue

        tokens = line.split()
        if len(tokens) < 2:
            continue

        snapshot_name = tokens[0]
        state_token = tokens[-1]
        timestamp_tokens = tokens[1:-1]
        if len(timestamp_tokens) == 0:
            timestamp = ""
        else:
            timestamp = " ".join(timestamp_tokens)
        if timestamp == "-":
            timestamp = ""

        if state_token in ("-", ""):
            vm_state: Optional[MacosVmState] = None
        else:
            vm_state = _normalise_libvirt_state(state_text=state_token)

        info = MacosSnapshotInfo(
            name=snapshot_name,
            created=timestamp,
            description=None,
            parent=None,
            vm_state=vm_state,
        )
        rtnval.append(info)
    return rtnval


def _xml_contains_source_file(*, xml_text: str, path: str) -> bool:
    """
        Return True when ``xml_text`` contains
        ``<source file="<path>"/>`` (single or double quoted).
    """
    if path is None or len(path) == 0:
        rtnval = False
        return rtnval
    needles = (
        'source file="{}"'.format(path),
        "source file='{}'".format(path),
    )
    rtnval = False
    for needle in needles:
        if needle in xml_text:
            rtnval = True
            break
    return rtnval


def _xml_contains_disk_id(*, xml_text: str, disk_id: str) -> bool:
    """
        Return True when ``xml_text`` carries a ``<source file=...>``
        or ``<target dev=...>`` whose value equals ``disk_id``.
    """
    if disk_id is None or len(disk_id) == 0:
        rtnval = False
        return rtnval
    needles = (
        'source file="{}"'.format(disk_id),
        "source file='{}'".format(disk_id),
        'target dev="{}"'.format(disk_id),
        "target dev='{}'".format(disk_id),
    )
    rtnval = False
    for needle in needles:
        if needle in xml_text:
            rtnval = True
            break
    return rtnval
