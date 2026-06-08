# Design

## Concept Mapping (LVM ↔ APFS ↔ Storage Spaces)

The cross-OS storage-management contract talks about *disks*, *pools*,
and *volumes*.  Each OS's native storage stack carries a different
vocabulary; the table below grounds the abstract contract in concrete
LVM / APFS / Storage-Spaces terms so a reader of this spec can compare
all three implementations side-by-side.

| Contract concept | macOS (this spec) | Ubuntu | Windows |
|---|---|---|---|
| Disk | Whole-disk node (`disk0`) | Block device of `type=disk` (`/dev/sda`) | `Get-Disk` row keyed by `Number` |
| Pool | APFS **Container** (`diskutil apfs createContainer / deleteContainer`) | LVM **Volume Group** | **Storage Pool** (`New-StoragePool`) |
| Pool member | APFS **Physical Store** (a partition on a disk) | LVM **Physical Volume** | `PhysicalDisk` flagged `CanPool=True` |
| Volume | APFS **Volume** (`diskutil apfs addVolume / deleteVolume`) | LVM **Logical Volume** | **Virtual Disk** (Storage Spaces) **or** raw partition |
| Volume identifier | APFS volume BSD device id (`disk1s2`) | `<vg>/<lv>` | Drive letter or volume id |
| Free space | Container free space — **shared by every volume in the container** | VG free PE × PE size | Pool / Virtual-Disk free bytes |
| Resiliency level | Apple-managed; not exposed by `diskutil apfs createContainer` | None natively (layer `mdadm` underneath) | Pool `ResiliencySettingName` (Mirror / Parity / Simple) |

The macOS branch differs from the Linux LVM branch in one structurally
important way: APFS volumes share their container's free space
**dynamically**.  There is no per-volume hard allocation; every
volume's effective ceiling moves with the container's free pool.  The
implementation makes this explicit by having `resize_volume` resize the
*container* (`diskutil apfs resizeContainer`) for APFS volumes,
documenting that every sibling volume's effective ceiling moves with
the call.  See § "APFS Shared Sizing" below.

---

## Overview

The storage-management feature is implemented as a single module,
`ccai.sysman.macos.storagemanagement`, that exposes a
`MacosStorageManager` class.  The coordinator instantiates
`MacosStorageManager` and calls its methods to perform discrete
block-level storage actions: disk enumeration, APFS volume create /
delete / resize, APFS container create / delete / extend / reduce, and
detailed disk and volume queries.

This feature **complements — and does not duplicate** — the mount,
unmount, and `/etc/fstab` management provided by the
`filesystem-management` feature.  The `MacosStorageManager` brings
disks, APFS containers, and APFS volumes into the desired shape; the
`MacosFilesystemManager` is then responsible for mounting those
volumes.

All command execution is delegated to a command runner implementing the
`ICommandRunner` interface.  The `MacosStorageManager` never directly
calls `subprocess`, shell commands, or `open()`; it only constructs
argument lists and hands them to the injected runner.  The plist-to-JSON
conversion runs through the runner too: every `diskutil ... -plist`
output is piped via `stdin=` to a separate `plutil -convert json -o
- -` invocation.  This keeps business logic separate from execution and
makes the feature fully testable via mock dependencies without root
access or a live system.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosStorageManager                  │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              MacosStorageManager                            │
│   ccai.sysman.macos.storagemanagement                   │
│                                                             │
│  list_disks()           list_volumes()                      │
│  get_disk_info()        get_volume_info()                   │
│  create_volume()        delete_volume()                     │
│  resize_volume()                                            │
│  create_pool()          delete_pool()                       │
│  add_to_pool()          remove_from_pool()                  │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│           LocalCommandRunner / SSHCommandRunner             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosStorageManager`

**Location:**
`source/packages/ccai/sysman/macos/storagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_disks` | `() → list[MacosDiskInfo]` | `diskutil list -plist physical \| plutil -convert json` |
| `list_volumes` | `() → list[MacosVolumeInfo]` | `diskutil apfs list -plist` (APFS) merged with `diskutil list -plist` (HFS+ / MS-DOS) |
| `create_volume` | `(spec) → MacosStorageActionResult` | `diskutil apfs addVolume <container> <fs> <name> -size <bytes>B` |
| `delete_volume` | `(volume_id) → MacosStorageActionResult` | `diskutil apfs deleteVolume <volume_id>` with data-loss advisory |
| `resize_volume` | `(volume_id, size_bytes) → MacosStorageActionResult` | `diskutil apfs resizeContainer <container> <bytes>B` for APFS; `diskutil resizeVolume <volume_id> <bytes>B` for HFS+ |
| `create_pool` | `(spec) → MacosStorageActionResult` | `diskutil apfs createContainer <disks...>` |
| `delete_pool` | `(pool_name) → MacosStorageActionResult` | `diskutil apfs deleteContainer <pool_name>` with data-loss advisory |
| `add_to_pool` | `(pool_name, disk_id) → MacosStorageActionResult` | `diskutil apfs resizeContainer <pool> 0 <disk>` |
| `remove_from_pool` | `(pool_name, disk_id) → MacosStorageActionResult` | `diskutil apfs resizeContainer <pool> 0 -none <disk>` with data-loss advisory |
| `get_volume_info` | `(volume_id) → MacosVolumeInfo \| None` | indexed lookup over `list_volumes()` |
| `get_disk_info` | `(disk_id) → MacosDiskInfo \| None` | indexed lookup over `list_disks()` |

### `MacosStorageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `default_filesystem` | `str` | `"apfs"` | Filesystem hint recorded onto `MacosVolumeSpec` |
| `default_container_id` | `str \| None` | `None` | Optional default container substituted when a `MacosVolumeSpec` omits `pool` |

---

## Data Models

### Result Types

```python
class MacosStorageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosStorageActionResult:
    status: MacosStorageActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    target: str | None             # volume id, container id, or disk node
    pool: str | None               # container id when applicable
    volume: str | None             # volume id when applicable
    message: str | None            # SKIPPED reason or data-loss advisory
```

### Disk / Volume Types

```python
@dataclass
class MacosDiskInfo:
    id: str                        # "disk0"
    name: str                      # display name
    size_bytes: int
    model: str | None
    serial: str | None
    partition_style: str | None    # "GUID_partition_scheme" / etc.
    health_status: str | None
    mount_points: list[str]        # mounts of every descendant volume
    is_pool_member: bool           # True when any partition is "Apple_APFS"

@dataclass
class MacosVolumeInfo:
    id: str                        # "disk1s2"
    name: str                      # APFS volume name
    size_bytes: int                # CapacityCeiling
    used_bytes: int | None         # CapacityInUse
    filesystem: str | None         # "apfs", "hfs", ...
    mount_point: str | None
    pool: str | None               # ContainerReference for APFS
    health_status: str | None

@dataclass
class MacosPoolSpec:
    name: str                      # audit-log only; not consumed by diskutil
    disks: list[str]
    resiliency: str | None = None  # ignored on macOS

@dataclass
class MacosVolumeSpec:
    name: str
    size_bytes: int
    pool: str | None = None        # container reference
    filesystem: str = "apfs"
```

### Exceptions

```python
class MacosStorageQueryError(Exception):
    """Raised when a diskutil / plutil query fails unexpectedly."""
```

---

## Pool / Volume Mapping

| Contract verb | macOS command surface | Idempotency probe |
|---|---|---|
| `list_disks` | `diskutil list -plist physical` + `plutil -convert json` | n/a |
| `list_volumes` | `diskutil apfs list -plist` + `diskutil list -plist` (both via plutil) | n/a |
| `create_volume` | `diskutil apfs addVolume <container> <fs> <name> -size <bytes>B` | APFS volume name in container |
| `delete_volume` | `diskutil apfs deleteVolume <volume_id>` | volume id absence |
| `resize_volume` (APFS) | `diskutil apfs resizeContainer <container> <bytes>B` | size match within 1 MiB |
| `resize_volume` (HFS+) | `diskutil resizeVolume <volume_id> <bytes>B` | size match within 1 MiB |
| `create_pool` | `diskutil apfs createContainer <disks...>` | any disk already a Physical Store |
| `delete_pool` | `diskutil apfs deleteContainer <pool>` | container reference absence |
| `add_to_pool` | `diskutil apfs resizeContainer <pool> 0 <disk>` | disk already a Physical Store of the container |
| `remove_from_pool` | `diskutil apfs resizeContainer <pool> 0 -none <disk>` | disk not a Physical Store of the container |
| `get_volume_info` | `list_volumes` lookup | n/a |
| `get_disk_info` | `list_disks` lookup | n/a |

### Volume Identifier Syntax

Volumes are identified by the BSD device id (`disk1s2`).  `diskutil`
also accepts the `/dev/diskNsM` form and the volume's mount path; this
implementation passes the bare id for cross-OS-contract parity with the
Windows `DriveLetter` convention.  Coordinators that have a mount path
should resolve to the BSD id themselves (typically via `diskutil info
<path>`).

### Pool Identifier Syntax

Containers are identified by their APFS reference: the BSD id of the
container's underlying `disk` node (`disk1` for a container whose
`/dev/disk1` is the container reference).  This is the value
`diskutil apfs list -plist` reports as `ContainerReference`.

### APFS Shared Sizing

APFS volumes share their container's free space dynamically.  There is
NO per-volume hard allocation.  This has three observable consequences
for callers of this feature:

1. `MacosVolumeInfo.size_bytes` is the volume's *ceiling*
   (``CapacityCeiling``), not its hard allocation.  It is the maximum
   size the volume could grow to before the container runs out of free
   space.  Two sibling volumes can both report a 100 GB ceiling on a
   100 GB container — they share the same 100 GB.

2. `resize_volume` on an APFS volume resizes the **container**, not
   the volume.  The implementation looks up the volume's container id
   and dispatches `diskutil apfs resizeContainer` instead of any
   per-volume cmdlet.  Every sibling volume's effective ceiling moves
   with the call.  Callers that mean "shrink this volume's allocation
   so other volumes have more room" cannot express that on APFS — the
   abstraction is wrong-shaped for it.  Use `delete_volume` then
   `create_volume` with a smaller size hint instead.

3. `create_volume`'s `size_bytes` is interpreted as a *quota hint*
   (`-size <bytes>B`).  APFS will not provision the volume beyond this
   ceiling, but the underlying allocation grows on demand.

### Shrink-Support Matrix

| Contained filesystem | Shrink support |
|---|---|
| APFS | Container shrink supported via `diskutil apfs resizeContainer` (the volume ceiling does not shrink — the container shrinks; volumes that have grown beyond the new container size will fail) |
| HFS+ | Supported via `diskutil resizeVolume <volume_id> <size>` |
| MS-DOS / FAT32 / exFAT | NOT supported by `diskutil resizeVolume`; the implementation surfaces the failure as `FAILED` |
| Apple_APFS partition | Resize via the container; do NOT call `diskutil resizeVolume` on a `disk0sN` whose Content is `Apple_APFS` |

---

## Command Mappings

### Write Commands (elevation honoured)

| Operation | Argv |
|---|---|
| `create_volume` | `diskutil apfs addVolume <container> <fs> <name> -size <bytes>B` |
| `delete_volume` | `diskutil apfs deleteVolume <volume_id>` |
| `resize_volume` (APFS) | `diskutil apfs resizeContainer <container> <bytes>B` |
| `resize_volume` (HFS+) | `diskutil resizeVolume <volume_id> <bytes>B` |
| `create_pool` | `diskutil apfs createContainer <disks...>` |
| `delete_pool` | `diskutil apfs deleteContainer <pool>` |
| `add_to_pool` | `diskutil apfs resizeContainer <pool> 0 <disk>` |
| `remove_from_pool` | `diskutil apfs resizeContainer <pool> 0 -none <disk>` |

### Read Commands (no elevation)

| Operation | Argv |
|---|---|
| `list_disks` (diskutil) | `diskutil list -plist physical` |
| `list_disks` (plist→json) | `plutil -convert json -o - -` (with `-plist` output piped via stdin) |
| `list_volumes` (APFS) | `diskutil apfs list -plist` |
| `list_volumes` (general) | `diskutil list -plist` |

All writes pass `elevated=True` when the config enables it.  Reads
never elevate.  The `plutil` conversion step also never elevates.

---

## Output Parsing

### `diskutil list -plist [physical]`

The plist-to-JSON conversion produces a dict with two interesting
arrays:

* `AllDisks`: a flat list of every BSD device id.
* `AllDisksAndPartitions`: a list of dicts, one per whole-disk node,
  each carrying:
    * `DeviceIdentifier` (`"disk0"`).
    * `Size` (integer bytes).
    * `Content` (the partition scheme — `"GUID_partition_scheme"` etc.).
    * `Partitions` (list of dicts with `DeviceIdentifier`, `Content`,
      `Size`, `MountPoint`, `VolumeName`).

The disk parser walks `AllDisksAndPartitions` and surfaces one
`MacosDiskInfo` per top-level entry; partitions are walked only to
collect descendant mountpoints and detect Apple_APFS membership.

### `diskutil apfs list -plist`

The JSON shape carries a `Containers` array.  Each container dict has:

* `ContainerReference` (`"disk1"`) — the cross-OS-contract pool id.
* `Volumes` — list of dicts with `DeviceIdentifier`, `Name`,
  `CapacityCeiling`, `CapacityInUse`, `MountPoint`, `MountedStatus`,
  `FilesystemType`.
* `PhysicalStores` — list of dicts with `DeviceIdentifier` (typically
  the partition that backs the container, e.g. `"disk0s2"`).

The volume parser walks every container's `Volumes` array; the
disk-membership probes walk every container's `PhysicalStores` array
and match by stripping the trailing `sN` suffix from each store's
device id (`disk0s2` -> `disk0`).

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_volume` | APFS volume with same name in container with matching size | `diskutil apfs list` |
| `delete_volume` | Volume id absence | `diskutil apfs list` + `diskutil list` |
| `resize_volume` | Size matches within 1 MiB | `diskutil apfs list` |
| `create_pool` | Any listed disk already a Physical Store of an existing container | `diskutil apfs list` |
| `delete_pool` | Container reference absence | `diskutil apfs list` |
| `add_to_pool` | Disk already a Physical Store of the container | `diskutil apfs list` |
| `remove_from_pool` | Disk not a Physical Store of the container | `diskutil apfs list` |

The 1-MiB tolerance on `create_volume` and `resize_volume` mirrors the
Ubuntu spec's tolerance choice; APFS allocates in 4 KiB blocks but
caller-friendly granularity coarser than that is appropriate to avoid
churn on tiny rounding deltas.

---

## Data-Loss Advisories

| Verb | Advisory text |
|---|---|
| `delete_volume` | `"APFS volume <id> was deleted; all data on the volume has been destroyed"` |
| `delete_pool` | `"APFS container <id> was deleted; every volume in the container has been destroyed"` |
| `remove_from_pool` | `"disk <id> was removed from APFS container <pool>; the physical-store association has been wiped"` |

Advisories surface on `SUCCESS` results in both `message` and
`detail` so callers that already index on `detail` continue to see
them.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosStorageQueryError` | Reserved for stricter read paths added in a follow-up batch.  Wave 1 reads return empty / `None` on probe failure to mirror the established macOS pattern (`get_mounts`, `stat`). |

All write failures are surfaced as
`MacosStorageActionResult(status=FAILED, ...)` carrying the runner's
exit code, stdout, and stderr.  Writes never raise for routine I/O.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system.*

### Property 1: list_disks Argv

*For any* call to `list_disks()`, the runner sees exactly
`["diskutil", "list", "-plist", "physical"]` followed by a `plutil`
conversion call carrying the diskutil stdout as `stdin`.

**Validates: Requirements 1.1**

### Property 2: list_disks Pool-Member Detection

*For any* disk whose partitions include at least one `Content =
"Apple_APFS"` entry, `is_pool_member` MUST be True.

**Validates: Requirements 1.3**

### Property 3: list_volumes Composition

*For any* call to `list_volumes()`, APFS volumes come first; non-APFS
volumes follow, excluding partitions whose `Content` is
`"Apple_APFS"` (those are containers, not volumes).

**Validates: Requirements 2.1, 2.2**

### Property 4: create_volume Argv

*For any* `MacosVolumeSpec(name=N, pool=P, size_bytes=S,
filesystem=F)`, `create_volume()` MUST dispatch
`["diskutil", "apfs", "addVolume", P, F, N, "-size", f"{S}B"]`.

**Validates: Requirements 3.1**

### Property 5: create_volume Idempotency

*For any* APFS volume with the same name in the same container with
matching size (within 1 MiB), `create_volume()` MUST return SKIPPED
without dispatching `addVolume`.

**Validates: Requirements 3.4**

### Property 6: create_volume Missing Pool

*For any* `MacosVolumeSpec(pool=None)` whose config also has
`default_container_id=None`, `create_volume()` MUST return FAILED
without dispatching any command.

**Validates: Requirements 3.2**

### Property 7: delete_volume Argv

*For any* `volume_id=V`, `delete_volume()` MUST dispatch
`["diskutil", "apfs", "deleteVolume", V]`.

**Validates: Requirements 4.1**

### Property 8: delete_volume Advisory

*For any* successful `delete_volume()` call, the result's `message`
MUST contain the substring `"destroyed"`.

**Validates: Requirements 4.3**

### Property 9: resize_volume APFS Routes to Container

*For any* APFS volume id whose container reference is `C`,
`resize_volume(V, S)` MUST dispatch
`["diskutil", "apfs", "resizeContainer", C, f"{S}B"]` — NOT a
per-volume resize cmdlet.

**Validates: Requirements 5.1**

### Property 10: resize_volume HFS+ Falls Back to resizeVolume

*For any* volume id whose container reference is `None` (HFS+ or other
non-APFS), `resize_volume(V, S)` MUST dispatch
`["diskutil", "resizeVolume", V, f"{S}B"]`.

**Validates: Requirements 5.2**

### Property 11: resize_volume Idempotency

*For any* volume whose current size is within 1 MiB of the requested
size, `resize_volume()` MUST return SKIPPED.

**Validates: Requirements 5.3**

### Property 12: create_pool Argv

*For any* `MacosPoolSpec(disks=[D1, D2])`, `create_pool()` MUST
dispatch `["diskutil", "apfs", "createContainer", D1, D2]`.

**Validates: Requirements 6.1**

### Property 13: create_pool Idempotency

*For any* listed disk already a Physical Store of an existing
container `C`, `create_pool()` MUST return SKIPPED with
`target=C` and NOT dispatch `createContainer`.

**Validates: Requirements 6.2**

### Property 14: delete_pool Argv

*For any* `pool_name=P`, `delete_pool()` MUST dispatch
`["diskutil", "apfs", "deleteContainer", P]`.

**Validates: Requirements 7.1**

### Property 15: add_to_pool Argv

*For any* `(pool_name=P, disk_id=D)`, `add_to_pool()` MUST dispatch
`["diskutil", "apfs", "resizeContainer", P, "0", D]` (note the magic
`0`).

**Validates: Requirements 8.1**

### Property 16: remove_from_pool Argv

*For any* `(pool_name=P, disk_id=D)`, `remove_from_pool()` MUST
dispatch `["diskutil", "apfs", "resizeContainer", P, "0", "-none", D]`.

**Validates: Requirements 9.1**

### Property 17: remove_from_pool Advisory

*For any* successful `remove_from_pool()` call, the result's `message`
MUST contain the substring `"wiped"`.

**Validates: Requirements 9.3**

### Property 18: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST NOT
be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 3.7, 4.4, 5.5, 6.6, 7.4, 8.4, 9.5**

### Property 19: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every runner
invocation MUST receive `elevated=True`.

**Validates: Requirements 12.1**

### Property 20: No Elevation for Reads

*For any* read verb (and any idempotency probe, including the
`plutil` conversion step), the runner MUST NEVER be invoked with
`elevated=True`.

**Validates: Requirements 12.2**

### Property 21: Error Surface

*For any* non-zero exit from a write command, the returned
`MacosStorageActionResult` MUST have `status=FAILED` and carry the
runner's exit code and stderr.

**Validates: Requirements 3.6, 9.4**

---

## Module Layout

```
source/packages/ccai/sysman/macos/
├── __init__.py
├── featurebase.py
├── filesystemmanagement.py
└── storagemanagement.py        ← MacosStorageManager + types

source/testroots/testbase/ccai/tests/sysman/macos/
└── test_storagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.macos.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.macos.storagemanagement import (
    MacosPoolSpec,
    MacosStorageManager,
    MacosStorageManagerConfig,
    MacosVolumeSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosStorageManagerConfig(elevated=True, dry_run=False)
storage = MacosStorageManager(runner=runner, config=config)

# Discover an external disk
for disk in storage.list_disks():
    print(f"{disk.id}\t{disk.size_bytes}\tpool_member={disk.is_pool_member}")

# Build an APFS container on disk2 + disk3
pool_spec = MacosPoolSpec(name="data-container", disks=["disk2", "disk3"])
result = storage.create_pool(spec=pool_spec)
container_id = result.target  # actual disk# assigned by diskutil

# Add a volume
vol_spec = MacosVolumeSpec(
    name="Photos",
    pool=container_id,
    size_bytes=200 * 1024 * 1024 * 1024,
    filesystem="apfs",
)
storage.create_volume(spec=vol_spec)

# Grow the container later
storage.resize_volume(
    volume_id="disk1s2",
    size_bytes=400 * 1024 * 1024 * 1024,
)
```

### Dry-Run Mode

```python
config = MacosStorageManagerConfig(elevated=True, dry_run=True)
storage = MacosStorageManager(runner=runner, config=config)

result = storage.delete_volume(volume_id="disk1s2")
assert result.status == MacosStorageActionStatus.DRY_RUN
print(result.command)
# ["diskutil", "apfs", "deleteVolume", "disk1s2"]
```

### SSH Execution with Aspects

```python
from ccai.sysman.macos.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="mac-build", username="admin"),
)
storage = MacosStorageManager(
    runner=runner,
    config=MacosStorageManagerConfig(elevated=True),
)
for vol in storage.list_volumes():
    print(vol.id, vol.size_bytes, vol.pool)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` that handles both standard argv
  matching and the `plutil -convert json` pipe.  Test helpers seed a
  sentinel string as the diskutil stdout and pre-seed the `plutil`
  response on that exact stdin so the manager's plist→JSON pipeline
  works without invoking real binaries.
* Argv-construction tests assert the exact argv shape for every write
  verb, including the magic `0` and `-none` tokens for the
  `resizeContainer` add / remove dispatches.
* Idempotency tests cover every verb's SKIPPED path: APFS volume
  present / absent, container present / absent, disk-as-Physical-Store
  present / absent, size match within 1 MiB.
* Dry-run tests prove the runner is not invoked.
* Elevation tests cover every write verb passing `elevated=True` and
  every read verb (including the `plutil` step) passing
  `elevated=False`.
* Parser tests cover `diskutil list -plist`, `diskutil apfs list
  -plist`, and the helper functions
  (`_strip_partition_suffix`, `_container_in_apfs_payload`,
  `_disk_in_apfs_container`).
* Failure surface is tested by seeding the runner with a non-zero exit
  on the write step.
* No test requires a live `diskutil` invocation.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `list_disks` | `diskutil list -plist physical` + `plutil -convert json` |
| `list_volumes` | `diskutil apfs list -plist` + `diskutil list -plist` |
| `create_volume` | `diskutil apfs addVolume <container> <fs> <name> -size <bytes>B` |
| `delete_volume` | `diskutil apfs deleteVolume <volume_id>` |
| `resize_volume` | `diskutil apfs resizeContainer <container> <bytes>B` (APFS) / `diskutil resizeVolume <volume> <bytes>B` (HFS+) |
| `create_pool` | `diskutil apfs createContainer <disks...>` |
| `delete_pool` | `diskutil apfs deleteContainer <pool>` |
| `add_to_pool` | `diskutil apfs resizeContainer <pool> 0 <disk>` |
| `remove_from_pool` | `diskutil apfs resizeContainer <pool> 0 -none <disk>` |
| `get_volume_info` | `list_volumes` lookup by id |
| `get_disk_info` | `list_disks` lookup by id |

---

## Extensions Beyond the Contract

None in this macOS implementation.  Filesystem-as-such formatting
(beyond the implicit format performed by `diskutil apfs addVolume`),
mount-point management, encryption (FileVault), snapshot creation
(`tmutil`), and Fusion-Drive specific ops are intentionally deferred.
When added they will live behind their own feature flags so a
coordinator writing against the contract does not have to special-case
macOS.

The feature also intentionally omits a SMART resolver on
`MacosDiskInfo.health_status`; the field exists to satisfy the
cross-OS data shape but is left as `None` until a `smartctl` /
`diskutil info` SMART parser is added in a follow-up batch.

---

## Future Considerations

* Surface a strict-read mode that raises `MacosStorageQueryError`
  instead of returning empty lists / `None`.  The exception is defined;
  only the call sites need to elect the strict variant.
* Wire `MacosVolumeInfo.health_status` to the volume's
  `MountedStatus` field for unmounted-but-present volumes.
* Add a `partition_style` follow-up that exposes both the partition
  scheme and APFS encryption status.
* Extend `add_to_pool` and `remove_from_pool` with an optional
  `force` parameter that adds `-force` to the underlying
  `resizeContainer` call.
