# Design

## Concept Mapping (Homebrew libvirt vs Hyper-V vs Apple Virtualization)

The cross-OS virtualization-management contract talks about *VMs*,
*disks*, and *snapshots*.  Each OS's native hypervisor stack carries
a different vocabulary; the table below grounds the abstract contract
in concrete terms so a reader of this spec can compare all three
implementations side-by-side.

| Contract concept | macOS (this spec) | Ubuntu (KVM/libvirt) | Windows (Hyper-V) | Apple Virtualization (out of scope) |
|---|---|---|---|---|
| Hypervisor host | Homebrew `libvirtd` over QEMU on Hypervisor.framework | Native `libvirtd` over KVM | Hyper-V hypervisor (Windows-only) | macOS `Virtualization.framework` |
| VM | libvirt **domain** | libvirt **domain** | Hyper-V VM | `VZVirtualMachine` (Swift only) |
| VM identifier | Domain name | Domain name | VM name | Object reference |
| Power state | `running` / `shut off` / `paused` / `pmsuspended` | Same | `Running` / `Off` / `Paused` / `Saved` | `state` enum |
| Create | `virt-install --name --memory --vcpus --disk ...` | Same | `New-VM ...` | `start()` after configuration |
| Start | `virsh start` | `virsh start` | `Start-VM` | `start()` |
| Graceful stop | `virsh shutdown` | `virsh shutdown` | `Stop-VM` (default) | `requestStop()` |
| Force stop | `virsh destroy` | `virsh destroy` | `Stop-VM -Force -TurnOff` | `stop()` |
| Pause | `virsh suspend` | `virsh suspend` | `Suspend-VM` | `pause()` |
| Resume | `virsh resume` | `virsh resume` | `Resume-VM` | `resume()` |
| Delete | `virsh undefine --remove-all-storage` | Same | `Stop-VM -Force; Remove-VM -Force` | n/a |
| Attach disk | `virsh attach-disk --persistent` | Same | `Add-VMHardDiskDrive` | `addStorageDevice` (config-time) |
| Detach disk | `virsh detach-disk --persistent` | Same | `Remove-VMHardDiskDrive` | n/a |
| Snapshot create | `virsh snapshot-create-as` | Same | `Checkpoint-VM` | n/a |
| Snapshot restore | `virsh snapshot-revert` | Same | `Restore-VMSnapshot -Confirm:$false` | n/a |
| Snapshot list | `virsh snapshot-list` | Same | `Get-VMSnapshot` | n/a |
| Required runtime | `brew install libvirt` + running `libvirtd` | libvirt + qemu-kvm | Hyper-V role | macOS 11+; Swift |

The macOS implementation reuses libvirt's `virsh` / `virt-install` CLI
identically to the Ubuntu implementation; the only practical
difference is the binary path (Homebrew installs at
`/opt/homebrew/bin/` on Apple-Silicon and at `/usr/local/bin/` on
Intel hosts) and the libvirt URI (`qemu:///system` requires root;
`qemu:///session` runs in user space).

Apple's Virtualization framework is **explicitly out of scope** —
its API is Swift-only and binding it from Python via PyObjC would
introduce a runtime dependency that does not match the rest of this
package.  Callers needing Apple Virtualization should use a separate,
Swift-driven package.

HyperKit is a legacy hypervisor built atop the deprecated
`Hypervisor.framework` low-level API.  Its Docker Desktop fork has
been retired and Apple removed kernel-level support in macOS 12+.
The `MacosVirtBackend.HYPERKIT` selector is recognised but
short-circuits with `FAILED message="hyperkit_deprecated"` from every
verb so coordinators get a clear typed signal.

---

## Overview

The virtualization-management feature is implemented as a single
module,
`automojo.sysman.macos.virtualizationmanagement`, that exposes a
`MacosVirtualizationManager` class.  The coordinator instantiates
`MacosVirtualizationManager` and calls its methods to perform
discrete VM-lifecycle, disk, and snapshot actions.

This feature **complements — and does not duplicate** — the storage
manager in `3-storage-management` (which manages APFS containers that
may back libvirt volumes), the firewall manager in
`3-firewall-management` (which opens ports for libvirt's virtual
networks), and the service manager in `3-service-management` (which
brings `libvirtd` up before any verb here is called).

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosVirtualizationManager           │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        MacosVirtualizationManager                           │
│  automojo.sysman.macos.virtualizationmanagement             │
│                                                             │
│  list_vms()              get_vm_info()                      │
│  create_vm()             delete_vm()                        │
│  start_vm()              stop_vm()                          │
│  pause_vm()              resume_vm()                        │
│  attach_disk()           detach_disk()                      │
│  snapshot_vm()           restore_snapshot()                 │
│  list_snapshots()                                           │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│           LocalCommandRunner / SSHCommandRunner             │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosVirtualizationManager`

**Location:**
`source/packages/automojo/sysman/macos/virtualizationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_vms` | `() → list[MacosVmInfo]` | `virsh list --all --name` + per-name `virsh dominfo` |
| `get_vm_info` | `(name) → MacosVmInfo \| None` | `virsh dominfo <name>` parsed |
| `create_vm` | `(spec) → MacosVirtualizationActionResult` | `virt-install` with rendered argv |
| `delete_vm` | `(name) → MacosVirtualizationActionResult` | `virsh undefine <name> --remove-all-storage` with data-loss advisory |
| `start_vm` | `(name) → MacosVirtualizationActionResult` | `virsh start <name>` |
| `stop_vm` | `(name, force=False) → MacosVirtualizationActionResult` | `virsh shutdown` (graceful) or `virsh destroy` (force) |
| `pause_vm` | `(name) → MacosVirtualizationActionResult` | `virsh suspend <name>` |
| `resume_vm` | `(name) → MacosVirtualizationActionResult` | `virsh resume <name>` |
| `attach_disk` | `(vm, disk_spec) → MacosVirtualizationActionResult` | `virsh attach-disk <vm> <path> <target> --persistent [--targetbus]` |
| `detach_disk` | `(vm, disk_id) → MacosVirtualizationActionResult` | `virsh detach-disk <vm> <disk_id> --persistent` with data-loss advisory |
| `snapshot_vm` | `(vm, snapshot_name) → MacosVirtualizationActionResult` | `virsh snapshot-create-as <vm> <name>` |
| `restore_snapshot` | `(vm, snapshot_name) → MacosVirtualizationActionResult` | `virsh snapshot-revert <vm> <name>` (unconditional) |
| `list_snapshots` | `(vm) → list[MacosSnapshotInfo]` | `virsh snapshot-list <vm>` parsed |

### `MacosVirtualizationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Mutations short-circuit when `True` |
| `elevated` | `bool` | `False` | Writes pass `elevated=True` when `True` |
| `backend` | `MacosVirtBackend` | `LIBVIRT` | Backend selector (HYPERKIT short-circuits) |
| `virsh_path` | `str \| None` | `None` | Auto-detect via `which virsh` when `None` |
| `virt_install_path` | `str \| None` | `None` | Auto-detect via `which virt-install` when `None` |
| `default_connection` | `str` | `"qemu:///system"` | libvirt URI passed via `--connect`; empty string suppresses |

---

## Data Models

```python
class MacosVirtualizationActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosVirtualizationActionResult(FeatureActionResult):
    vm_name: str | None = None
    snapshot_name: str | None = None
    message: str | None = None

class MacosVirtBackend(Enum):
    LIBVIRT  = "libvirt"
    HYPERKIT = "hyperkit"

class MacosVmState(Enum):
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"
    OTHER     = "other"

@dataclass
class MacosVmSpec:
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: int | None = None
    disk_path: str | None = None
    os_variant: str | None = None
    network: str | None = None
    iso_path: str | None = None
    graphics: str = "none"
    boot_kind: int = 1            # cross-OS-spec parity (Windows generation)

@dataclass
class MacosVmInfo:
    name: str
    state: MacosVmState
    cpu_count: int | None = None
    memory_mb: int | None = None
    uptime_seconds: int | None = None
    os_type: str | None = None
    vm_id: str | None = None

@dataclass
class MacosVmDiskSpec:
    path: str
    target: str | None = None
    controller_type: str | None = None
    bus: str | None = None

@dataclass
class MacosSnapshotInfo:
    name: str
    created: str
    description: str | None = None
    parent: str | None = None
    vm_state: MacosVmState | None = None

class MacosVirtualizationQueryError(Exception): ...
```

---

## virsh / virt-install Discovery

```python
def _resolve_virsh(self) -> Optional[str]:
    config = self._typed_config()
    if config.virsh_path is not None and len(config.virsh_path) > 0:
        return config.virsh_path
    if self._resolved_virsh is not None:
        return None if not self._resolved_virsh else self._resolved_virsh
    path = self._which(binary="virsh")
    self._resolved_virsh = path or ""
    return path
```

* When `virsh_path` is explicitly set, it is used verbatim and the
  cache is bypassed.
* When `virsh_path` is `None`, the manager runs `which virsh` once and
  caches the result.  Subsequent calls reuse the cached value.
* When the cache holds the empty string, every verb returns `FAILED`
  with `message="virsh_not_found"` until the caller updates the
  config.

The same logic applies to `virt-install`.

---

## Command Construction

### `virsh` argv prefix

```python
def _virsh_argv(self, *, args: list) -> list:
    config = self._typed_config()
    path = self._resolve_virsh() or "virsh"
    prefix = [path]
    if len(config.default_connection) > 0:
        prefix += ["--connect", config.default_connection]
    return prefix + list(args)
```

### `virt-install` argv

Mirrors the Ubuntu implementation:

```
virt-install --connect <uri> --name <n> --memory <mib> --vcpus <c> \
  [--disk path=<p>|size=<gb>] \
  [--os-variant <v>] [--network <n>] [--cdrom <iso>] \
  --graphics <g> --noautoconsole
```

`--noautoconsole` is mandatory; without it `virt-install` blocks
waiting for the serial console.

### Disk argument resolution (`_build_disk_argument`)

| Field | Effect on `--disk` |
|---|---|
| `disk_path` is set | `path=<disk_path>` |
| `disk_path` is None and `disk_size_gb` is set | `size=<gb>` |
| Both are None | `--disk` is omitted |

---

## Idempotency Probes

| Verb | Probe | SKIPPED condition |
|---|---|---|
| `create_vm` | `virsh dominfo <name>` | Domain exists |
| `delete_vm` | `virsh dominfo <name>` | Domain does not exist |
| `start_vm` | `virsh dominfo <name>` | `state == RUNNING` |
| `stop_vm` | `virsh dominfo <name>` | `state == STOPPED` |
| `pause_vm` | `virsh dominfo <name>` | `state == PAUSED` |
| `resume_vm` | `virsh dominfo <name>` | `state == RUNNING` |
| `attach_disk` | `virsh dumpxml <vm>` | `<source file="<path>"/>` present |
| `detach_disk` | `virsh dumpxml <vm>` | No matching `<target dev=...>` and no matching `<source file=...>` |
| `snapshot_vm` | `virsh snapshot-list <vm>` | A snapshot with the same name exists |
| `restore_snapshot` | — (always reverts) | Never |

The XML probes use string matching rather than a full XML parse.

---

## Stop Semantics

| `force` | Verb | Argv |
|---|---|---|
| `False` | Graceful | `virsh shutdown <name>` |
| `True` | Hard power-off | `virsh destroy <name>` |

`virsh destroy` powers the domain off but leaves the persistent
definition intact.  `delete_vm` is the verb for full removal.

---

## Dry-Run Behaviour

Dry-run disables every mutating entry point and returns
`DRY_RUN` with the would-have-been argv recorded on the result.
Read verbs and idempotency probes run freely.

---

## Elevation Policy

Every write verb pulls `elevated` from the config; reads and probes
always pass `elevated=False`.

The libvirt connection URI matters here too: `qemu:///system` typically
requires root (so `elevated=True` is normal); `qemu:///session` runs
as the local user (so `elevated=False` is correct).  The manager
itself does not branch on the URI — the contract treats elevation as
a host-level decision the coordinator makes.

---

## Correctness Properties

1. **Idempotency.**  Every lifecycle transition returns `SKIPPED`
   when the resource is already in the requested state.  The contract
   exempts `restore_snapshot`.
2. **No raises from writes.**  Failures from the runner surface as
   `FAILED` results with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
3. **Typed read errors.**  `list_vms`, `get_vm_info`, and
   `list_snapshots` raise `MacosVirtualizationQueryError` for
   unexpected failures.  `get_vm_info` returns `None` for the
   well-defined "unknown VM" case.
4. **Backend-aware fail-fast.**  HYPERKIT short-circuits with
   `FAILED message="hyperkit_deprecated"`.
5. **Discovery-aware fail-fast.**  When `virsh` (or `virt-install` for
   `create_vm`) is not on PATH, every verb returns `FAILED` with
   `message="virsh_not_found"` (or `virt_install_not_found`) without
   invoking the runner against an unknown-binary argv.

---

## Testing Strategy

- `_MockCommandRunner` queues `RunResult` objects keyed by argv tuple.
- Unit tests per public method covering happy / SKIPPED / FAILED /
  DRY_RUN branches.
- Parser tests for `virsh list --all --name`, `virsh dominfo`, and
  `virsh snapshot-list`.
- XML-probe tests asserting the source-file and target-dev matchers.
- Stop-semantics test asserting `force=True` and `force=False`
  dispatch different argv.
- Elevation test asserting writes pass `elevated=True` and reads pass
  `elevated=False`.
- HyperKit test asserting `FAILED message="hyperkit_deprecated"`.
- `which virsh` discovery test asserting that the resolved path is
  cached and that a missing binary produces
  `FAILED message="virsh_not_found"`.
- No live libvirt is required.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| `libvirtd` lifecycle | `3-service-management` | Coordinator brings `libvirtd` up via `brew services` before any verb here |
| Disk-image storage | `3-storage-management` | Coordinator places qcow2 files; this feature attaches them |
| Network firewall holes | `3-firewall-management` | Coordinator opens libvirt-network ports |

---

## Out of Scope (this iteration)

- **Apple Virtualization framework** (Swift-only API).
- HyperKit (deprecated; selector recognised but every verb FAILS).
- Live migration between hosts.
- libvirt virtual-network create/destroy verbs.
- libvirt storage-pool create/destroy verbs.
- Guest agent installation / RPC.

These extensions are tracked under `tasks.md` follow-ups.
