# Requirements

## Introduction

The backup-restore feature provides the `automojo-sysman-macos` package with
the ability to define, persist, run, list, restore, and verify backup jobs on
macOS hosts. macOS ships a first-party backup framework (Time Machine, driven
by the `tmutil` CLI) that occupies the dominant share of operator workflows;
however, Time Machine has constraints that make it unsuitable for some
maintenance use cases (it requires a dedicated APFS volume; it cannot back up
arbitrary remote paths into a non-Time-Machine directory). This feature
therefore exposes a `MacosBackupRestoreManager` whose backend dispatches to
one of three tools: Time Machine via `tmutil` (default), `rsync` (for
arbitrary mirrors), or `restic` (when installed by the operator).

A maintenance coordinator uses this feature to capture known-good snapshots
of specified paths before destructive or risky changes, to restore from those
snapshots on demand, and to verify the integrity of an archive. All system
commands (`tmutil`, `rsync`, `restic`, `tar`, `tee`, `cat`, `ls`, `rm`) are
executed through the `automojo-interfaces` command-runner abstraction. All
mutating operations respect dry-run mode.

This feature does not perform APFS snapshot capture, FileVault key escrow, or
database-consistent dumps. APFS snapshots are the responsibility of the
`storage-management` feature (which calls `tmutil localsnapshot` for
volume-scoped snapshots), and database dumps remain the responsibility of
application-specific tooling. Schedule wiring is delegated to the
`scheduled-tasks` feature; this feature persists the `schedule` field but
does not itself install `launchd` plists.

---

## Requirements

### Requirement 1: Persist a Backup Job Specification

**User Story:** As a maintenance coordinator, I want to declare a named
backup job from a declarative spec so that subsequent runs can dispatch the
correct backend tool without re-supplying its parameters.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `MacosBackupJobSpec` THE SYSTEM SHALL
   serialise the spec to JSON and write it to
   `/var/db/automojo/backups/jobs/<job_id>.json` via `tee` with the JSON
   payload supplied on stdin.
2. WHEN the spec's `kind=TIME_MACHINE` THE SYSTEM SHALL ALSO invoke
   `tmutil setdestination <destination>` so that Time Machine knows the
   destination volume.
3. THE SYSTEM SHALL pass `elevated=True` to the runner so that `tee` and
   `tmutil setdestination` can write under root-owned directories.
4. WHEN a job with the same `job_id` already exists on disk AND the
   persisted JSON is byte-equivalent to the proposed JSON, THE SYSTEM SHALL
   return `MacosBackupActionStatus.SKIPPED` without invoking `tee` or
   `tmutil setdestination`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any runner
   command and SHALL return a `MacosBackupActionResult` with status
   `DRY_RUN` and the would-have-been argv recorded on `command`.
6. WHEN any underlying command exits non-zero THE SYSTEM SHALL return
   `FAILED` with the runner's exit code, captured stdout, captured stderr,
   and the argv populated on the result's `command` field.

---

### Requirement 2: Delete a Persisted Backup Job

**User Story:** As a maintenance coordinator, I want to delete a previously-
defined backup job so that the host stops carrying its declarative footprint.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` THE SYSTEM SHALL invoke
   `rm -f /var/db/automojo/backups/jobs/<job_id>.json` through the runner
   with `elevated=True`.
2. WHEN the persisted spec carries `kind=TIME_MACHINE` THE SYSTEM SHALL
   ALSO invoke `tmutil removedestination <destination>` so the
   destination volume is unregistered.
3. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `MacosBackupActionStatus.SKIPPED` without invoking `rm` or
   `tmutil removedestination`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any runner
   command.

---

### Requirement 3: Run a Backup On Demand

**User Story:** As a maintenance coordinator, I want to run a previously-
defined job on demand so that a fresh archive is captured before an
immediate risky change.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` and the spec's `kind` is
   `TIME_MACHINE` THE SYSTEM SHALL invoke `tmutil startbackup --auto`
   through the runner with `elevated=True`.
2. WHEN the spec's `kind` is `RSYNC` THE SYSTEM SHALL invoke
   `rsync -aAXv --delete <sources> <destination>` (with `--exclude
   <pattern>` per exclude entry).
3. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic --repo <destination> --password-file=/dev/stdin backup
   <sources>` and forward the encryption passphrase via runner stdin.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend
   tool.
5. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `FAILED` with `detail="job not found"`.
6. WHEN the backend tool exits zero THE SYSTEM SHALL return `SUCCESS`
   with the captured stdout, stderr, exit code, and argv.
7. WHEN the backend tool exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured stderr available on the result.

---

### Requirement 4: List Persisted Backup Jobs

**User Story:** As a maintenance coordinator, I want to enumerate every
persisted backup job (and, for Time-Machine jobs, every snapshot Time
Machine has on the destination volume) so that audit reports can list the
host's backup footprint.

#### Acceptance Criteria

1. WHEN the coordinator requests the list THE SYSTEM SHALL invoke
   `ls -1 /var/db/automojo/backups/jobs` through the runner with
   `elevated=False`.
2. THE SYSTEM SHALL filter the directory listing to entries whose suffix
   is `.json`, derive each `job_id` by stripping the suffix, and read each
   spec via `cat`.
3. THE SYSTEM SHALL parse each JSON spec and emit one `MacosBackupInfo`
   per spec, with `id`, `kind`, `created`, `size_bytes`, `source_count`,
   and `destination`.
4. WHEN any persisted spec carries `kind=TIME_MACHINE` THE SYSTEM SHALL
   ALSO invoke `tmutil listbackups` and `tmutil destinationinfo` to
   harvest the live Time Machine snapshot list. Live snapshots SHALL NOT
   override persisted spec rows; they SHALL be appended.
5. WHEN the job directory does not exist on disk THE SYSTEM SHALL return
   only the live Time-Machine rows (when present) or an empty list.
6. WHEN any individual spec fails to parse THE SYSTEM SHALL skip that
   spec and continue with the rest.

---

### Requirement 5: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a
previously-taken backup into a target path so that a prior state can be
recovered.

#### Acceptance Criteria

1. WHEN the spec's `kind` is `TIME_MACHINE` THE SYSTEM SHALL invoke
   `tmutil restore <backup_path> <target>` through the runner with
   `elevated=True`. The `<backup_path>` is resolved by parsing
   `tmutil listbackups`; the most-recent snapshot is the default.
2. WHEN the spec's `kind` is `RSYNC` THE SYSTEM SHALL invoke
   `rsync -aAXv --delete <destination> <target>` (reverse direction).
3. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic --repo <destination> --password-file=/dev/stdin restore
   latest --target <target>` and forward the passphrase via stdin.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend
   tool.
5. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `FAILED` with `detail="job not found"`.
6. WHEN the restore command exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured stderr available on the result.

---

### Requirement 6: Verify a Backup

**User Story:** As a maintenance coordinator, I want to verify that an
archive is intact so that corrupted archives can be detected before being
relied on.

#### Acceptance Criteria

1. WHEN the spec's `kind` is `TIME_MACHINE` THE SYSTEM SHALL invoke
   `tmutil compare` (best-effort; Time Machine reports per-file
   differences against the source).
2. WHEN the spec's `kind` is `RESTIC` THE SYSTEM SHALL invoke
   `restic --repo <destination> --password-file=/dev/stdin check`.
3. WHEN the spec's `kind` is `RSYNC` THE SYSTEM SHALL invoke a
   best-effort `rsync -an --checksum <sources> <destination>` and treat
   a zero-exit as success.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend
   tool.

---

### Requirement 7: Encryption Passphrase Safety

**User Story:** As a maintenance coordinator, I want any passphrase used
to encrypt or decrypt a `RESTIC` repository to never appear on argv so
that the passphrase does not leak to process listings, audit logs, or
aspect-level persistence stores.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER include `encryption_passphrase` as a positional
   or named argv token on any `restic` command.
2. THE SYSTEM SHALL deliver the passphrase via runner stdin and pass
   `--password-file=/dev/stdin` on argv.
3. THE SYSTEM SHALL exclude the passphrase from the recorded `command`
   field on every returned `MacosBackupActionResult`.
4. THE SYSTEM SHALL exclude the passphrase from any persisted on-disk
   spec by replacing the field with the sentinel string
   `"***REDACTED***"` before writing.

---

### Requirement 8: Backend Dispatch via `MacosBackupKind`

**User Story:** As a maintenance coordinator, I want to choose between
three backends (`TIME_MACHINE`, `RSYNC`, `RESTIC`) per job so that each
operator-defined job uses the right tool for its target paths.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept a `kind` field on every `MacosBackupJobSpec`
   of type `MacosBackupKind`.
2. WHEN `kind=TIME_MACHINE` THE SYSTEM SHALL route every backend
   operation to `tmutil`.
3. WHEN `kind=RSYNC` THE SYSTEM SHALL route every backend operation to
   `rsync`.
4. WHEN `kind=RESTIC` THE SYSTEM SHALL route every backend operation to
   `restic`.
5. WHEN `kind` is none of the above THE SYSTEM SHALL return `FAILED`
   with `detail` describing the unsupported kind.

---

### Requirement 9: Time Machine Constraints

**User Story:** As a maintenance coordinator, I want the Time Machine
backend to enforce the constraints Time Machine itself imposes so that
operators are not surprised when a spec that looks valid in JSON fails
at runtime.

#### Acceptance Criteria

1. THE SYSTEM SHALL document that a `TIME_MACHINE` job's `destination`
   field MUST be the mount-point of an APFS volume that has been
   formatted for Time Machine (`tmutil setdestination` will refuse a
   destination that is not in the correct format).
2. THE SYSTEM SHALL document that the `TIME_MACHINE` backend ignores
   the spec's `excludes` field — Time Machine excludes are managed via
   `tmutil addexclusion` / `tmutil removeexclusion` and are out of
   scope for this feature.
3. THE SYSTEM SHALL surface a `tmutil` non-zero exit verbatim on the
   returned `MacosBackupActionResult`'s `stderr` field so the operator
   can react to the constraint violation.

---

### Requirement 10: Schedule Field is Persisted but Not Wired

**User Story:** As a maintenance coordinator, I want the `schedule`
field on a backup spec to be persisted alongside the job so that the
`scheduled-tasks` feature can wire it up later.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `schedule` field THE SYSTEM SHALL
   serialise it into the persisted JSON.
2. THE SYSTEM SHALL NOT itself create a `launchd` plist, a cron
   entry, or any other scheduling primitive.
3. THE SYSTEM SHALL document this division of responsibility in the
   module docstring.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want every mutating
backup operation to run with the privileges required by the underlying
tools.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to every runner invocation in
   `create_backup_job`, `delete_backup_job`, `run_backup_now`,
   `restore_from_backup`, and `verify_backup`.
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL pass `elevated=False`.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs
   (`list_backups`, `get_job_status`).

---

### Requirement 12: Result Shape and Error Surface

**User Story:** As a maintenance coordinator, I want every action to
return a typed result so that audit-report generation can enumerate
every action with a predictable shape.

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `MacosBackupActionResult` from every
   write verb, carrying `status`, `detail`, `exit_code`, `stdout`,
   `stderr`, `command`, `job_id`, and `target_path`.
2. THE SYSTEM SHALL raise `MacosBackupQueryError` from `list_backups`
   only when the underlying read fails unexpectedly.
3. THE SYSTEM SHALL never include the encryption passphrase or any
   other secret material on the `command` field.
