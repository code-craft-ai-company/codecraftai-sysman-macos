# Requirements

## Introduction

The virtualization-management feature provides the
`automojo-sysman-macos` package with the ability to manage local
virtual machines on macOS hosts via Homebrew-installed QEMU and
libvirt (`virsh`, `virt-install`).  This spec implements the
cross-OS virtualization-management contract defined at
`automojo-sysman-common/specs/contracts/virtualization-management/contract.md`
for macOS.

A maintenance coordinator running in an external package drives every
operation by calling into this feature's public interfaces.  The
feature does not decide *which* VMs should exist, *what shape* their
disks should take, or *when* snapshots should be created — it
provides the capability to manipulate libvirt domain state reliably,
idempotently, and with full auditability.  All system commands are
executed exclusively through the `automojo-interfaces` command-runner
abstraction.

Apple's Virtualization framework, HyperKit, Hyper-V, and any
cloud-provider hypervisor surface are NOT in scope here.  Apple's
**Virtualization framework** is Swift-only and binding it from Python
via `PyObjC` would introduce a runtime dependency that does not match
the rest of this package; callers needing it should use a separate,
Swift-driven package.  HyperKit is accepted as a legacy backend
selector but every verb returns a typed FAILED result with
`message="hyperkit_deprecated"` because Apple removed HyperKit's
hypervisor support in macOS 12+ and the Docker Desktop fork has been
retired.

This feature stops at the libvirt-domain boundary: VM lifecycle, disk
attach / detach, and snapshot create / revert / list.  Network and
storage-pool definition (the libvirt-network and libvirt-pool surfaces
beyond an attached path) is intentionally excluded — it lives in the
firewall and storage feature areas of this package.

---

## Requirements

### Requirement 1: Backend Selection

**User Story:** As a maintenance coordinator, I want the manager to
expose an explicit virtualization-backend selector, so that the
HyperKit-deprecated and (future) Apple-Virtualization branches can
fail fast without surprising the caller.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `MacosVirtBackend` as an `Enum` with
   `LIBVIRT` (default) and `HYPERKIT`.
2. THE SYSTEM SHALL store the selected backend on
   `MacosVirtualizationManagerConfig.backend`.
3. WHEN the configured backend is `HYPERKIT` THE SYSTEM SHALL return
   `MacosVirtualizationActionResult(status=FAILED,
   message="hyperkit_deprecated")` from every verb without invoking
   the runner.
4. THE SYSTEM SHALL document Apple's Virtualization framework as
   explicitly out of scope (Swift-only).

---

### Requirement 2: virsh / virt-install Discovery

**User Story:** As a maintenance coordinator, I want the manager to
auto-detect the Homebrew-installed `virsh` and `virt-install` binaries
without hard-coding their paths, so that the feature works on both
Apple-Silicon (`/opt/homebrew/bin/`) and Intel
(`/usr/local/bin/`) hosts.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept an explicit
   `MacosVirtualizationManagerConfig.virsh_path` (default `None`)
   and `MacosVirtualizationManagerConfig.virt_install_path` (default
   `None`).
2. WHEN `virsh_path` is `None` THE SYSTEM SHALL probe `which virsh`
   through the runner once at first use and cache the resolved path.
3. WHEN `virt_install_path` is `None` THE SYSTEM SHALL probe
   `which virt-install` through the runner once at first use and
   cache the resolved path.
4. WHEN a probe returns no result (non-zero exit or empty stdout) THE
   SYSTEM SHALL return
   `MacosVirtualizationActionResult(status=FAILED,
   message="virsh_not_found")` (or `virt_install_not_found` for the
   `create_vm` verb) from every verb until the configuration is
   updated.

---

### Requirement 3: Connection URI

**User Story:** As a maintenance coordinator, I want every `virsh` and
`virt-install` invocation to carry the libvirt connection URI, so
that operations target the intended hypervisor connection.

#### Acceptance Criteria

1. THE SYSTEM SHALL default
   `MacosVirtualizationManagerConfig.default_connection` to
   `"qemu:///system"`.
2. WHEN dispatching a `virsh` command THE SYSTEM SHALL prepend
   `--connect <uri>` to the argv when the configured URI is non-empty.
3. WHEN the configured URI is the empty string THE SYSTEM SHALL omit
   the `--connect` flag entirely.
4. THE SYSTEM SHALL apply the same rule on `virt-install` invocations.

---

### Requirement 4: List VMs

**User Story:** As a maintenance coordinator, I want to enumerate
every defined VM with its power state and resource configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_vms()` THE SYSTEM SHALL dispatch
   `virsh --connect <uri> list --all --name` with `elevated=False`.
2. THE SYSTEM SHALL parse the line-per-name output into a list of VM
   names.
3. FOR each name THE SYSTEM SHALL dispatch `virsh dominfo <name>`
   without elevation and merge the parsed fields into a
   `MacosVmInfo`.
4. THE SYSTEM SHALL map libvirt's reported State string onto
   `MacosVmState` (`running`, `shut off`, `paused`, `pmsuspended` ->
   `RUNNING` / `STOPPED` / `PAUSED` / `SUSPENDED`; everything else ->
   `OTHER`).
5. WHEN `virsh list` exits non-zero THE SYSTEM SHALL raise
   `MacosVirtualizationQueryError`.

---

### Requirement 5: Get VM Info

**User Story:** As a maintenance coordinator, I want to look up a
single VM by name, so that I can branch on its power state without
parsing a full enumeration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_vm_info(name)` THE SYSTEM SHALL
   dispatch `virsh dominfo <name>` with `elevated=False`.
2. WHEN the VM is not defined THE SYSTEM SHALL return `None` (the
   runner's "domain not found" stderr is recognised heuristically).
3. WHEN `virsh dominfo` fails for any other reason THE SYSTEM SHALL
   raise `MacosVirtualizationQueryError`.

---

### Requirement 6: Create VM

**User Story:** As a maintenance coordinator, I want to provision a
fresh QEMU/KVM VM via `virt-install`, so that bringing a guest online
is a single call.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_vm(spec)` THE SYSTEM SHALL build
   the `virt-install` argv from `spec` (`--name`, `--memory`,
   `--vcpus`, `--disk`, optional `--os-variant`, optional `--network`,
   optional `--cdrom`, `--graphics <value>`, `--noautoconsole`).
2. WHEN `spec.disk_path` is set THE SYSTEM SHALL pass
   `--disk path=<disk_path>`; otherwise WHEN `spec.disk_size_gb` is
   set THE SYSTEM SHALL pass `--disk size=<gb>`; otherwise THE SYSTEM
   SHALL omit `--disk` entirely.
3. WHEN a VM with `spec.name` already exists THE SYSTEM SHALL return
   `SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `DRY_RUN` carrying the
   would-have-been argv.

---

### Requirement 7: Delete VM

**User Story:** As a maintenance coordinator, I want to undefine a VM
and remove its backing storage in a single call.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_vm(name)` THE SYSTEM SHALL
   dispatch `virsh undefine <name> --remove-all-storage`.
2. WHEN no VM with `name` is defined THE SYSTEM SHALL return
   `SKIPPED`.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `MacosVirtualizationActionResult.message` with a data-loss advisory.

---

### Requirement 8: Power-State Verbs

**User Story:** As a maintenance coordinator, I want to start, stop,
pause, and resume VMs.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_vm(name)` THE SYSTEM SHALL
   dispatch `virsh start <name>`; SKIPPED when already `RUNNING`.
2. WHEN the coordinator calls `stop_vm(name, force=False)` THE SYSTEM
   SHALL dispatch `virsh shutdown <name>`; SKIPPED when already
   `STOPPED`.
3. WHEN the coordinator calls `stop_vm(name, force=True)` THE SYSTEM
   SHALL dispatch `virsh destroy <name>`; SKIPPED when already
   `STOPPED`.
4. WHEN the coordinator calls `pause_vm(name)` THE SYSTEM SHALL
   dispatch `virsh suspend <name>`; SKIPPED when already `PAUSED`.
5. WHEN the coordinator calls `resume_vm(name)` THE SYSTEM SHALL
   dispatch `virsh resume <name>`; SKIPPED when already `RUNNING`.
6. WHEN `dry_run=True` for any of these verbs THE SYSTEM SHALL
   return `DRY_RUN`.

---

### Requirement 9: Attach Disk

**User Story:** As a maintenance coordinator, I want to attach an
existing disk image to a VM persistently.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_disk(vm, disk_spec)` THE SYSTEM
   SHALL dispatch `virsh attach-disk <vm> <disk_spec.path> <target>
   --persistent` (target defaults to `"vdb"` when not supplied).
2. WHEN `disk_spec.bus` is set THE SYSTEM SHALL append
   `--targetbus <bus>` to the argv.
3. WHEN the active VM XML (`virsh dumpxml <vm>`) already references
   `disk_spec.path` in a `<source file=...>` element THE SYSTEM SHALL
   return `SKIPPED`.

---

### Requirement 10: Detach Disk

**User Story:** As a maintenance coordinator, I want to detach a disk
from a VM persistently.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_disk(vm, disk_id)` THE SYSTEM
   SHALL dispatch `virsh detach-disk <vm> <disk_id> --persistent`.
2. WHEN no `<target dev=...>` and no `<source file=...>` matching
   `disk_id` is present in the active VM XML THE SYSTEM SHALL return
   `SKIPPED`.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `MacosVirtualizationActionResult.message` with a data-loss advisory
   noting that the backing file itself was NOT removed.

---

### Requirement 11: Snapshot Lifecycle

**User Story:** As a maintenance coordinator, I want to create, list,
and revert snapshots on a VM.

#### Acceptance Criteria

1. WHEN the coordinator calls `snapshot_vm(vm, snapshot_name)` THE
   SYSTEM SHALL dispatch
   `virsh snapshot-create-as <vm> <snapshot_name>`.
2. WHEN a snapshot with that name already exists for `vm` (probed via
   `virsh snapshot-list <vm>`) THE SYSTEM SHALL return `SKIPPED`.
3. WHEN the coordinator calls `restore_snapshot(vm, snapshot_name)`
   THE SYSTEM SHALL dispatch
   `virsh snapshot-revert <vm> <snapshot_name>` UNCONDITIONALLY and
   SHALL populate `message` with a data-loss advisory on `SUCCESS`.
4. WHEN the coordinator calls `list_snapshots(vm)` THE SYSTEM SHALL
   dispatch `virsh snapshot-list <vm>` and parse the tabular output.

---

### Requirement 12: Elevation, Dry-Run, and Result Shape

**User Story:** As a maintenance coordinator, I want every mutating
operation to run with root privilege when configured, respect dry-run,
and surface failures structurally.

#### Acceptance Criteria

1. WHEN `config.elevated=True` THE SYSTEM SHALL pass `elevated=True`
   to the runner for every write verb.
2. WHEN performing read verbs and idempotency probes THE SYSTEM SHALL
   NEVER pass `elevated=True`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return `DRY_RUN` carrying the
   would-have-been argv.
4. WHEN the runner returns non-zero THE SYSTEM SHALL return `FAILED`
   carrying `exit_code`, `stdout`, and `stderr`.
5. THE SYSTEM SHALL populate `vm_name` on every result and
   `snapshot_name` on snapshot-related results.
