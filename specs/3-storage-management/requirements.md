# Requirements

## Introduction

The storage management feature provides the `codecraftai-sysman-macos`
package with the ability to enumerate disks and volumes, create /
delete / resize APFS volumes, manage APFS containers (the cross-OS
contract's "pool"), add and remove physical disks from a container,
and query detailed disk and volume metadata on macOS systems.  This
spec implements the storage-management contract defined at
`codecraftai-sysman-common/specs/contracts/storage-management/contract.md`
for macOS using the `diskutil` family (`diskutil list`, `diskutil
info`, `diskutil apfs list / addVolume / deleteVolume / createContainer
/ deleteContainer / resizeContainer`) plus `plutil` to convert
``-plist`` output into JSON for parsing.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* volumes should exist, *which* disks
should back a container, or *what* size a volume should claim — it
provides the capability to *manipulate APFS state* reliably,
idempotently, and with full auditability.  All system commands are
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction.

Filesystem creation per se does not exist on macOS the way it does on
Linux: `diskutil apfs addVolume` creates an APFS volume *with* its
filesystem in one step.  Mount-point management lives in
`filesystem-management`; this feature stops at the volume boundary.

---

## Requirements

### Requirement 1: Enumerate Physical Disks

**User Story:** As a maintenance coordinator, I want to enumerate every
whole-disk node on the system with its size, partition style, and
APFS-store membership, so that I can decide which disks are eligible
for inclusion in an APFS container.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_disks()` THE SYSTEM SHALL dispatch
   `diskutil list -plist physical` through the command runner WITHOUT
   elevation, then pipe the resulting plist through `plutil -convert
   json -o - -` (also unelevated) for parsing.
2. THE SYSTEM SHALL return one `MacosDiskInfo` per ``DeviceIdentifier``
   entry in the ``AllDisksAndPartitions`` array.
3. THE SYSTEM SHALL set ``MacosDiskInfo.is_pool_member=True`` when the
   disk has at least one partition whose ``Content`` field is
   ``"Apple_APFS"``.
4. THE SYSTEM SHALL populate ``MacosDiskInfo.mount_points`` with every
   mountpoint reported on any descendant partition.
5. WHEN either ``diskutil`` or ``plutil`` exits non-zero THE SYSTEM
   SHALL return an empty list rather than raise — Wave 1 manager
   pattern treats absence as informational.

---

### Requirement 2: Enumerate Volumes

**User Story:** As a maintenance coordinator, I want to enumerate every
volume on the system (APFS volumes plus HFS+ / MS-DOS partitions
surfaced as volumes), so that I can compare actual storage state
against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_volumes()` THE SYSTEM SHALL
   dispatch `diskutil apfs list -plist` through the command runner
   WITHOUT elevation, pipe through `plutil`, and emit one
   `MacosVolumeInfo` per APFS volume.
2. THE SYSTEM SHALL also dispatch `diskutil list -plist` and emit one
   `MacosVolumeInfo` per partition NOT already surfaced by the APFS
   probe and whose ``Content`` is NOT ``"Apple_APFS"`` (those are
   container partitions, not volumes).
3. THE SYSTEM SHALL populate ``MacosVolumeInfo.size_bytes`` from the
   APFS volume's ``CapacityCeiling`` field, and
   ``MacosVolumeInfo.used_bytes`` from ``CapacityInUse`` when present.
4. THE SYSTEM SHALL populate ``MacosVolumeInfo.pool`` with the
   container's ``ContainerReference`` for APFS volumes; ``None``
   for non-APFS volumes.
5. WHEN either probe fails THE SYSTEM SHALL silently omit that source
   and return whatever the other source produced.

---

### Requirement 3: Create an APFS Volume

**User Story:** As a maintenance coordinator, I want to create an APFS
volume of a given size in a given container, so that an application
can claim it.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosVolumeSpec` with `name`,
   `pool` (the container reference), `size_bytes`, and `filesystem`
   THE SYSTEM SHALL dispatch `diskutil apfs addVolume <pool>
   <filesystem> <name> -size <size_bytes>B` through the command runner
   with `elevated=True` when configured.
2. WHEN the spec's `pool` is `None` AND the config's
   `default_container_id` is also `None` THE SYSTEM SHALL return
   `MacosStorageActionResult(status=FAILED)` with a descriptive
   `message`.
3. WHEN the spec's `pool` is `None` BUT the config's
   `default_container_id` is set THE SYSTEM SHALL substitute the
   default for `pool`.
4. WHEN an APFS volume with the same name already exists in the same
   container with a matching size (within 1 MiB) THE SYSTEM SHALL skip
   execution and return `MacosStorageActionResult(status=SKIPPED)`.
5. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)` populated with `target`,
   `pool`, and `volume`.
6. WHEN the operation fails THE SYSTEM SHALL return
   `MacosStorageActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 4: Delete an APFS Volume

**User Story:** As a maintenance coordinator, I want to delete an APFS
volume by id, so that decommissioned data is reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_volume(volume_id)` THE SYSTEM
   SHALL dispatch `diskutil apfs deleteVolume <volume_id>` through the
   command runner with `elevated=True` when configured.
2. WHEN no volume matching `volume_id` exists THE SYSTEM SHALL skip
   execution and return `MacosStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)` whose `message` carries
   a data-loss advisory.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 5: Resize a Volume (Container-Level Semantic)

**User Story:** As a maintenance coordinator, I want to grow or shrink
the storage available to a volume by id, so that capacity tracks
changing workload.

#### Acceptance Criteria

1. WHEN the coordinator calls `resize_volume(volume_id, size_bytes)`
   on an APFS volume THE SYSTEM SHALL dispatch `diskutil apfs
   resizeContainer <container> <size_bytes>B` — APFS volumes share
   the container's free space dynamically, so resizing the container
   is the closest analogue to a per-volume resize.  The result's
   `target` SHALL be the container id; `pool` and `volume` are also
   populated.
2. WHEN the coordinator calls `resize_volume(volume_id, size_bytes)`
   on an HFS+ volume THE SYSTEM SHALL dispatch `diskutil resizeVolume
   <volume_id> <size_bytes>B` instead.
3. WHEN the volume's current size is within 1 MiB of `size_bytes` THE
   SYSTEM SHALL skip execution and return
   `MacosStorageActionResult(status=SKIPPED)`.
4. WHEN no volume matching `volume_id` exists THE SYSTEM SHALL return
   `MacosStorageActionResult(status=FAILED)` with a descriptive
   `message`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 6: Create an APFS Container (Pool)

**User Story:** As a maintenance coordinator, I want to create an APFS
container that spans one or more disks, so that volumes can be carved
out of the resulting pool.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosPoolSpec` with `disks` THE
   SYSTEM SHALL dispatch `diskutil apfs createContainer <disks...>`
   through the command runner with `elevated=True` when configured.
2. WHEN any of the listed disks is already a Physical Store of an
   existing APFS container THE SYSTEM SHALL skip execution and return
   `MacosStorageActionResult(status=SKIPPED)` populated with the
   existing container's id on `target` and `pool`.
3. THE SYSTEM SHALL ignore `MacosPoolSpec.resiliency` because APFS
   container resiliency is Apple-managed.
4. THE SYSTEM SHALL ignore `MacosPoolSpec.name` for the actual command
   construction (`diskutil apfs createContainer` does not accept a
   caller-chosen name) but SHALL preserve the value on the result for
   audit logging.
5. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 7: Delete an APFS Container (Pool)

**User Story:** As a maintenance coordinator, I want to delete an APFS
container by reference, so that the underlying disks are reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_pool(pool_name)` THE SYSTEM
   SHALL dispatch `diskutil apfs deleteContainer <pool_name>` through
   the command runner with `elevated=True` when configured.
2. WHEN no container with that reference exists THE SYSTEM SHALL skip
   execution and return `MacosStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)` whose `message` carries
   a data-loss advisory.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 8: Add a Disk to an APFS Container

**User Story:** As a maintenance coordinator, I want to add another
physical disk to an existing APFS container, so that the container's
free pool grows.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_pool(pool_name, disk_id)` THE
   SYSTEM SHALL dispatch `diskutil apfs resizeContainer <pool_name> 0
   <disk_id>` through the command runner with `elevated=True` when
   configured (the `0` instructs `diskutil` to keep the current size
   while adding the new physical store).
2. WHEN `disk_id` is already a Physical Store of `pool_name` THE
   SYSTEM SHALL skip execution and return
   `MacosStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 9: Remove a Disk from an APFS Container

**User Story:** As a maintenance coordinator, I want to evict a
physical disk from an APFS container, so that the disk can be reused
or decommissioned.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_pool(pool_name, disk_id)`
   THE SYSTEM SHALL dispatch `diskutil apfs resizeContainer
   <pool_name> 0 -none <disk_id>` through the command runner with
   `elevated=True` when configured.
2. WHEN `disk_id` is not currently a Physical Store of `pool_name`
   THE SYSTEM SHALL skip execution and return
   `MacosStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `MacosStorageActionResult(status=SUCCESS)` whose `message` carries
   a data-loss advisory.
4. WHEN APFS rejects the eviction (e.g. because the container is in
   use) THE SYSTEM SHALL surface the runner's stderr verbatim on a
   `MacosStorageActionResult(status=FAILED)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosStorageActionResult(status=DRY_RUN)`.

---

### Requirement 10: Get Volume Info

**User Story:** As a maintenance coordinator, I want to look up a single
volume by id, so that I can branch on its size, filesystem, and
container without parsing `diskutil apfs list` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_volume_info(volume_id)` THE SYSTEM
   SHALL invoke `list_volumes()` and return the matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 11: Get Disk Info

**User Story:** As a maintenance coordinator, I want to look up a single
disk by id, so that I can branch on its size, partition style, and
APFS-store membership.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_disk_info(disk_id)` THE SYSTEM SHALL
   invoke `list_disks()` and return the matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 12: Elevation Policy

**User Story:** As a maintenance coordinator, I want every mutating
`diskutil` invocation to run with the privileges required by macOS, so
that `diskutil apfs addVolume` / `deleteVolume` / `createContainer` /
`deleteContainer` / `resizeContainer` succeed on a standard system
where the process does not run as root, while reads stay
unprivileged.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write verb
   (`create_volume`, `delete_volume`, `resize_volume`, `create_pool`,
   `delete_pool`, `add_to_pool`, `remove_from_pool`).
2. WHEN performing read-only operations (`list_disks`, `list_volumes`,
   `get_volume_info`, `get_disk_info`, all idempotency probes
   including the implicit `plutil` conversion step) THE SYSTEM SHALL
   NEVER pass `elevated=True`.
3. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL NEVER pass `elevated=True`.
