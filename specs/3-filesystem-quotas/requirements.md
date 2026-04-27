# Requirements

## Introduction

The filesystem-quotas feature provides the
`automojo-sysman-macos` package with the ability to manage disk
quotas on macOS systems for the legacy HFS+ filesystem family
through the BSD-flavoured `quota` tooling: `quotacheck`,
`quotaon`, `quotaoff`, `setquota`, `quota`, and `repquota`. It
covers per-user and per-group block / inode quotas with
idempotent enable / disable / set semantics and read-only query
verbs for current usage and limits.

A maintenance coordinator uses this feature to declare and
enforce per-principal disk-usage policy on a mountpoint, to
report the current posture of that mountpoint, and to confirm
whether quota enforcement is currently active. Every system
command is dispatched through the `automojo-interfaces`
`ICommandRunner` abstraction so the manager works identically
under local and SSH execution. All mutating operations honour
dry-run.

## Major Limitation: APFS Has No Quota Subsystem

Modern macOS systems (10.13 High Sierra and later) use APFS by
default. **APFS has no quota subsystem at all** — Apple removed
classic per-user / per-group quota tracking when introducing
APFS and replaced it with per-volume container space management
(via `diskutil apfs addVolume` size limits and reserve / quota
properties on a per-volume basis, NOT per principal). The BSD
`quota` family of tools is still installed on macOS, but issuing
`quotaon` against an APFS mountpoint fails with a kernel-level
error.

This feature therefore detects the mountpoint's filesystem type
via `mount` and short-circuits every write verb with `FAILED`
and `detail="apfs_no_quota_support"` when the target is APFS.
HFS+ (still found on Time Machine backup volumes, legacy
external drives, and a small number of installations) remains
the only supported surface. Read verbs against APFS return empty
results rather than fail, since reporting "no quotas defined" is
a truthful statement about an APFS volume.

This feature does NOT manage mount options. Quota mount options
are an HFS+ legacy that callers must arrange via
`filesystem-management` or system tooling before this feature
can enforce quotas. Project quotas, grace-period configuration,
and per-target enable / disable distinctions are out of scope;
the macOS pilot mirrors the Ubuntu pilot scope.

---

## Requirements

### Requirement 1: Detect Filesystem Type and Refuse APFS Writes

**User Story:** As a maintenance coordinator, I want the manager
to detect that a mountpoint is APFS and refuse write operations
loudly, so that the tooling gap is surfaced as a typed FAILED
result rather than a confusing kernel error.

#### Acceptance Criteria

1. WHEN the coordinator invokes any write verb on a mountpoint
   whose filesystem type (per `mount | grep <mountpoint>`) is
   `apfs` THE SYSTEM SHALL return
   `MacosQuotaActionResult(status=FAILED,
   detail="apfs_no_quota_support", ...)` without invoking any
   quota command.
2. WHEN the coordinator invokes any read verb on an APFS
   mountpoint THE SYSTEM SHALL return an empty result (None for
   single-principal queries; an empty list for `list_quotas`;
   `MacosQuotaStatus(enabled=False, ...)` for
   `get_quota_status`) without raising.
3. WHEN `mount` reports a filesystem type listed in
   `config.quota_aware_filesystems` (defaults `["hfs", "ufs"]`)
   THE SYSTEM SHALL proceed with the regular quota tooling.

---

### Requirement 2: Enable Quota Enforcement (HFS+)

**User Story:** As a maintenance coordinator, I want to enable
quota enforcement on an HFS+ mountpoint, so that subsequent
`setquota` calls take effect.

#### Acceptance Criteria

1. WHEN the coordinator enables quotas on an HFS+ mountpoint
   THE SYSTEM SHALL invoke `quotacheck -ug <mountpoint>` to
   (re)build the on-disk accounting database, then `quotaon -ug
   <mountpoint>` to begin enforcement.
2. WHEN `quotaon -p <mountpoint>` already reports both user and
   group quotas as ON THE SYSTEM SHALL return `SKIPPED` without
   dispatching either command.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke any
   runner command and SHALL return
   `MacosQuotaActionResult(status=DRY_RUN)` with the would-have-
   been argv recorded on `command`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN any of the dispatched commands exits non-zero THE
   SYSTEM SHALL return `FAILED` with the runner's exit code,
   captured stdout, captured stderr, and the failing argv.
6. WHEN the supplied `mountpoint` is not currently mounted THE
   SYSTEM SHALL raise `MacosQuotaFilesystemNotMountedError`
   before invoking any quota command.

---

### Requirement 3: Disable Quota Enforcement (HFS+)

**User Story:** As a maintenance coordinator, I want to disable
quota enforcement on an HFS+ mountpoint without remounting.

#### Acceptance Criteria

1. WHEN the coordinator disables quotas THE SYSTEM SHALL invoke
   `quotaoff -ug <mountpoint>`.
2. WHEN `quotaon -p <mountpoint>` already reports both user and
   group quotas as OFF THE SYSTEM SHALL return `SKIPPED` without
   dispatching `quotaoff`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `quotaoff` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `quotaoff` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 4: Set User Quota (HFS+)

**User Story:** As a maintenance coordinator, I want to set
soft / hard block limits for a specific user on an HFS+
mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator sets a user quota on a quota-aware
   mountpoint THE SYSTEM SHALL invoke `setquota -u <user>
   <soft_kb> <hard_kb> 0 0 <mountpoint>` where the inode
   arguments are zero (no inode enforcement) and the block
   limits are in 1-KiB blocks.
2. WHEN `quota -u <user>` already reports the requested
   `<soft_kb>` and `<hard_kb>` for `<mountpoint>` THE SYSTEM
   SHALL return `SKIPPED` without dispatching `setquota`.
3. WHEN the mountpoint is APFS THE SYSTEM SHALL return `FAILED`
   with `detail="apfs_no_quota_support"` and shall NOT invoke
   `setquota`.
4. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `setquota` and SHALL return `DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. WHEN `setquota` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 5: Set Group Quota (HFS+)

**User Story:** As a maintenance coordinator, I want to set
soft / hard block limits for a group on an HFS+ mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator sets a group quota on a quota-aware
   mountpoint THE SYSTEM SHALL invoke `setquota -g <group>
   <soft_kb> <hard_kb> 0 0 <mountpoint>`.
2. WHEN `quota -g <group>` already reports the requested
   limits THE SYSTEM SHALL return `SKIPPED`.
3. WHEN the mountpoint is APFS THE SYSTEM SHALL return `FAILED`
   with `detail="apfs_no_quota_support"`.
4. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `setquota` and SHALL return `DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. WHEN `setquota` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 6: Query a User's Current Quota

**User Story:** As a maintenance coordinator, I want to read
the current usage and limits for a specific user on a
mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator queries a user quota on a quota-aware
   mountpoint THE SYSTEM SHALL invoke `quota -u <user>` (without
   elevation) and parse the per-mountpoint row into a
   `MacosQuotaInfo` carrying `used_bytes`, `soft_bytes`,
   `hard_bytes`, `used_inodes`, `soft_inodes`, `hard_inodes`,
   and `grace_period_seconds`.
2. WHEN no row matches `<mountpoint>` in the parsed output THE
   SYSTEM SHALL return `None`.
3. WHEN the mountpoint is APFS THE SYSTEM SHALL return `None`
   without invoking `quota`.
4. WHEN `quota -u` exits with a status other than 0, 1, or 2
   THE SYSTEM SHALL raise `MacosQuotaQueryError`.

---

### Requirement 7: Query a Group's Current Quota

**User Story:** As a maintenance coordinator, I want to read
the current usage and limits for a group on a mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator queries a group quota on a quota-aware
   mountpoint THE SYSTEM SHALL invoke `quota -g <group>` and
   parse the row into a `MacosQuotaInfo`.
2. WHEN no row matches `<mountpoint>` in the parsed output THE
   SYSTEM SHALL return `None`.
3. WHEN the mountpoint is APFS THE SYSTEM SHALL return `None`.
4. WHEN `quota -g` exits unexpectedly THE SYSTEM SHALL raise
   `MacosQuotaQueryError`.

---

### Requirement 8: List All Quotas on a Mountpoint

**User Story:** As a maintenance coordinator, I want to
enumerate every user and group quota active on a mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator lists quotas on a quota-aware
   mountpoint THE SYSTEM SHALL invoke `repquota -ug
   <mountpoint>` (without elevation) and parse the output into
   a `list[MacosQuotaInfo]`.
2. WHEN the mountpoint is APFS THE SYSTEM SHALL return an empty
   list.
3. WHEN `repquota` reports an empty body THE SYSTEM SHALL
   return an empty list.
4. WHEN `repquota` exits with a non-zero status THE SYSTEM
   SHALL raise `MacosQuotaQueryError`.

---

### Requirement 9: Query Quota Enforcement Status

**User Story:** As a maintenance coordinator, I want to query
whether quota enforcement is currently active on a mountpoint.

#### Acceptance Criteria

1. WHEN the coordinator queries the enforcement status on a
   quota-aware mountpoint THE SYSTEM SHALL invoke `quotaon -p
   <mountpoint>` (without elevation) and parse the output into a
   `MacosQuotaStatus`.
2. WHEN the mountpoint is APFS THE SYSTEM SHALL return
   `MacosQuotaStatus(enabled=False, enforcing=False,
   user_quotas_active=False, group_quotas_active=False,
   filesystem_type="apfs")` without invoking `quotaon`.
3. WHEN `quotaon -p` reports both user and group quotas as
   "off" THE SYSTEM SHALL return
   `MacosQuotaStatus(enabled=False, enforcing=False, ...)`.
4. WHEN `quotaon -p` exits with a non-zero status THE SYSTEM
   SHALL raise `MacosQuotaQueryError`.

---

### Requirement 10: Mount Validation Before Writes

**User Story:** As a maintenance coordinator, I want write
verbs to refuse a mountpoint that is not currently mounted.

#### Acceptance Criteria

1. WHEN any write verb is invoked with a mountpoint that is NOT
   currently mounted THE SYSTEM SHALL raise
   `MacosQuotaFilesystemNotMountedError` before invoking any
   quota command.
2. WHEN dry-run mode is active THE SYSTEM SHALL NOT perform the
   mount-validation probe.

---

### Requirement 11: Dry-Run, Elevation, and Error Reporting

**User Story:** Standard dry-run / elevation / failure
semantics across every write verb.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return
   `MacosQuotaActionResult(status=DRY_RUN)`.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every
   write command with `elevated=True`. Reads MUST NEVER pass
   `elevated=True` to the runner, regardless of
   `config.elevated`.
3. WHEN the runner returns non-zero from a write command THE
   SYSTEM SHALL return `MacosQuotaActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.

---

### Requirement 12: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeated
invocations of the same write verb to be no-ops when the
filesystem already matches the requested state.

#### Acceptance Criteria

1. WHEN `enable_quotas` is invoked on a mountpoint already
   reporting both user and group quotas as ON THE SYSTEM SHALL
   return `SKIPPED`.
2. WHEN `disable_quotas` is invoked on a mountpoint already
   reporting both kinds OFF THE SYSTEM SHALL return `SKIPPED`.
3. WHEN `set_user_quota` is invoked with values that already
   match the user's current record THE SYSTEM SHALL return
   `SKIPPED`.
4. WHEN `set_group_quota` is invoked with values that already
   match the group's current record THE SYSTEM SHALL return
   `SKIPPED`.
