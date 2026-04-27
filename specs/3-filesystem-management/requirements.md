# Requirements

## Introduction

The filesystem management feature provides the `automojo-sysman-macos` package with
the ability to mount and unmount devices, manage persistent mount-table entries,
create and remove directories and symbolic links, change ownership and permissions,
and inspect file / disk metadata on macOS systems.  This spec implements the
filesystem-management contract defined at
`automojo-sysman-common/specs/contracts/filesystem-management/contract.md` for
macOS using the BSD toolchain (`mount`, `umount`, `diskutil`, `stat -f`, `df`,
`mkdir`, `rm`, `ln`, `chmod`, `chown`) together with an `IFileOperator` for
transactional edits to `/etc/fstab`.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
paths should be mounted or which permissions are correct — it provides the
capability to *manipulate filesystem state* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction; all file reads and writes against
`/etc/fstab` flow through an `IFileOperator` so that dry-run simulation and test
mocking do not require a live system.  macOS's `/etc/fstab` is advisory — the boot
mount set is primarily driven by `diskutil` / Disk Arbitration — so the spec also
documents when a caller should reach for `diskutil`-owned mounts instead.

---

## Requirements

### Requirement 1: Mount a Device

**User Story:** As a maintenance coordinator, I want to mount a block device, image,
or remote share at a mount point, so that the mounted filesystem is available to the
running system.

#### Acceptance Criteria

1. WHEN the coordinator provides a `device`, `mountpoint`, optional `fstype`, and
   optional `options` THE SYSTEM SHALL construct the argv `mount` with
   `-t <fstype>` when `fstype` is set (recognised macOS types: `hfs`, `apfs`,
   `exfat`, `msdos`, `smbfs`, `nfs`, `webdav`), `-o <options>` when `options` is
   set, and `<device> <mountpoint>` as the trailing positional arguments, dispatched
   through the command runner with `elevated=True` when configured.
2. WHEN the mount point is already mounted with the same device (detected via
   `mount` output parsing) THE SYSTEM SHALL skip execution and return a
   `MacosFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the mount succeeds THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=SUCCESS`.
4. WHEN the mount fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED` and the captured exit code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 2: Unmount a Device

**User Story:** As a maintenance coordinator, I want to unmount a mount point,
optionally lazily (via `diskutil unmount force`), so that the device can be safely
detached.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmount(mountpoint, lazy=False)` THE SYSTEM SHALL
   dispatch `umount <mountpoint>` through the command runner with `elevated=True`
   when configured.
2. WHEN `lazy=True` THE SYSTEM SHALL dispatch `diskutil unmount force <mountpoint>`
   instead (BSD `umount -l` has no macOS equivalent).
3. WHEN the mount point is not currently mounted THE SYSTEM SHALL skip execution
   and return a `MacosFilesystemActionResult` with `status=SKIPPED`.
4. WHEN the unmount succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the unmount fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED` and the captured exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Add a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want to add an entry to `/etc/fstab`
so that the mount persists across reboots even though macOS treats the file as
advisory.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosFstabEntry` THE SYSTEM SHALL read
   `/etc/fstab` via the file operator, check for an existing entry whose `device`
   or `mountpoint` matches the supplied entry, and (when no match is found) append
   a line `<device>\t<mountpoint>\t<fstype>\t<options>\t<dump>\t<passno>` to the
   file's contents and write the result back via the file operator with
   `elevated=True` when configured.
2. WHEN an existing entry already matches the new entry exactly (all six fields)
   THE SYSTEM SHALL skip execution and return a `MacosFilesystemActionResult`
   with `status=SKIPPED`.
3. WHEN an existing entry matches `device` or `mountpoint` but not all fields THE
   SYSTEM SHALL replace the existing line with the new one.
4. WHEN the write succeeds THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=SUCCESS` and a `detail` noting that `/etc/fstab` on macOS is
   advisory — Disk Arbitration may override the entry for boot-time mounts.
5. WHEN the write fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file and SHALL return
   a `MacosFilesystemActionResult` with `status=DRY_RUN` and a unified diff of the
   intended change in the result's `detail`.

---

### Requirement 4: Remove a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want to remove an entry from
`/etc/fstab` so that the mount no longer persists across reboots.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_fstab_entry(device_or_mountpoint)` THE
   SYSTEM SHALL read `/etc/fstab`, filter out any line whose first or second
   whitespace-separated field matches the supplied token, and write the result
   back via the file operator.
2. WHEN no matching line is found THE SYSTEM SHALL skip execution and return a
   `MacosFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 5: Query Current Mounts

**User Story:** As a maintenance coordinator, I want to list every active mount,
so that idempotency guards and reporting workflows can reason about what is
currently mounted.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mounts()` THE SYSTEM SHALL dispatch `mount`
   through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line of the BSD `mount`
   output (format: `<device> on <mountpoint> (<fstype>, <options>)`) into a list
   of `MacosMountInfo` records.
3. WHEN the command fails THE SYSTEM SHALL raise a `MacosFilesystemQueryError`
   carrying the exit code and stderr.
4. WHEN callers need APFS / Core Storage structural information THE SYSTEM SHALL
   additionally offer a future-extension hook that dispatches
   `diskutil list -plist` — this extension is documented in the design but not
   required for contract conformance.

---

### Requirement 6: Create a Directory

**User Story:** As a maintenance coordinator, I want to create a directory with
specific mode, owner, and group, so that application data paths exist before the
application runs.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_directory(path, mode, owner, group)` THE
   SYSTEM SHALL dispatch `mkdir -p <path>` through the command runner with
   `elevated=True` when configured, then (when `mode` is not `None`) dispatch
   `chmod <mode> <path>`, and (when `owner` or `group` is not `None`) dispatch
   `chown <owner>:<group> <path>`.
2. WHEN the path already exists as a directory with matching mode, owner, and
   group THE SYSTEM SHALL skip execution and return a
   `MacosFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
4. WHEN every dispatched command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
5. WHEN any dispatched command fails THE SYSTEM SHALL stop and return a
   `MacosFilesystemActionResult` with `status=FAILED` naming the failing step.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Remove a Directory

**User Story:** As a maintenance coordinator, I want to remove a directory,
optionally recursively, so that obsolete directories are cleaned up.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_directory(path, recursive=False)` THE
   SYSTEM SHALL dispatch `rmdir <path>` through the command runner with
   `elevated=True` when configured.
2. WHEN `recursive=True` THE SYSTEM SHALL dispatch `rm -rf <path>` instead.
3. WHEN the path does not exist THE SYSTEM SHALL skip execution and return a
   `MacosFilesystemActionResult` with `status=SKIPPED`.
4. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Create a Symbolic Link

**User Story:** As a maintenance coordinator, I want to create a symbolic link
from `link` to `target`, so that path indirections can be established reliably.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_symlink(link, target)` THE SYSTEM SHALL
   dispatch `ln -s <target> <link>` through the command runner with
   `elevated=True` when configured.
2. WHEN `link` already exists as a symlink pointing to `target` THE SYSTEM SHALL
   skip execution and return a `MacosFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN `link` already exists as a symlink pointing to a different target THE
   SYSTEM SHALL dispatch `ln -snf <target> <link>` to atomically replace the
   link (macOS BSD `ln` supports `-snf`).
4. WHEN `link` exists but is NOT a symlink THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Remove a Symbolic Link

**User Story:** As a maintenance coordinator, I want to remove a symbolic link so
that an obsolete indirection is undone.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_symlink(link)` THE SYSTEM SHALL dispatch
   `rm <link>` through the command runner with `elevated=True` when configured,
   but only after verifying via `stat -f` that the path is a symbolic link.
2. WHEN the link does not exist THE SYSTEM SHALL skip execution and return a
   `MacosFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path exists but is NOT a symbolic link THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Change Ownership

**User Story:** As a maintenance coordinator, I want to change the owner and
group of a path, so that file-system ACLs match the running service's identity.

#### Acceptance Criteria

1. WHEN the coordinator calls `chown(path, user, group)` THE SYSTEM SHALL
   dispatch `chown <user>:<group> <path>` through the command runner (with
   `:<group>` omitted when `group` is `None`), with `elevated=True` when
   configured.
2. WHEN the current owner and group already match the requested values THE SYSTEM
   SHALL skip execution and return a `MacosFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Change Mode

**User Story:** As a maintenance coordinator, I want to change the POSIX mode of
a path, so that file-system permissions match the policy for the managed service.

#### Acceptance Criteria

1. WHEN the coordinator calls `chmod(path, mode)` THE SYSTEM SHALL dispatch
   `chmod <octal> <path>` through the command runner with `elevated=True` when
   configured, where `<octal>` is the 3- or 4-digit octal string formatted from
   the integer argument.
2. WHEN the current mode already equals the requested mode THE SYSTEM SHALL skip
   execution and return a `MacosFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 12: Stat a Path

**User Story:** As a maintenance coordinator, I want to retrieve metadata about
a path, so that downstream logic can branch on type, mode, owner, group, and
size without having to parse raw `stat -f` output.

#### Acceptance Criteria

1. WHEN the coordinator calls `stat(path)` THE SYSTEM SHALL dispatch
   `stat -f '%HT|%Lp|%Su|%Sg|%z|%N' <path>` through the command runner without
   elevation (BSD `stat`; `%HT` = human-readable type, `%Lp` = lower octal mode,
   `%Su` = owner name, `%Sg` = group name, `%z` = size, `%N` = name).
2. WHEN the command succeeds THE SYSTEM SHALL parse the pipe-separated output
   into a `MacosFileInfo` with `path_type`, `mode`, `owner`, `group`,
   `size_bytes`, and `path` populated.
3. WHEN `stat` exits non-zero with stderr mentioning `No such file or directory`
   THE SYSTEM SHALL return `None`.
4. WHEN `stat` exits non-zero for any other reason THE SYSTEM SHALL raise a
   `MacosFilesystemQueryError`.

---

### Requirement 13: Query Disk Usage

**User Story:** As a maintenance coordinator, I want to query disk-usage for a
path's filesystem, so that capacity-planning workflows can branch on current
utilisation.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_disk_usage(path)` THE SYSTEM SHALL dispatch
   `df -b <path>` through the command runner without elevation (`-b` prints
   512-byte blocks; the manager multiplies by 512 to obtain bytes).
2. WHEN the command succeeds THE SYSTEM SHALL parse the header-plus-one-line
   output into a `MacosDiskUsageInfo` with `path`, `total_bytes`, `used_bytes`,
   `available_bytes`, and `use_percent`.
3. WHEN the command fails THE SYSTEM SHALL raise a `MacosFilesystemQueryError`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all filesystem write
operations to run with root privilege via `sudo`, so that mount / unmount /
chmod / chown / fstab edits succeed on a standard macOS system where the
process is not running as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner and the file operator for every write.
2. WHEN performing read-only operations (get_mounts, stat, query_disk_usage) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of configuration.
3. WHEN the process is already running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner or file operator to surface the
   resulting permission error as a `MacosFilesystemActionResult` with
   `status=FAILED` for writes or as a `MacosFilesystemQueryError` for reads.
