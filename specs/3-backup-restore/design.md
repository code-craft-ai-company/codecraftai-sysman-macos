# Design

## Overview

The macOS backup-restore feature is implemented as a single module,
`ccai.sysman.macos.backuprestore`, exposing a `MacosBackupRestoreManager`
class and its associated dataclasses and enum. The manager wraps three
backend tools — `tmutil` (Time Machine, default), `rsync`, and `restic` —
behind a single verb surface defined by the cross-OS contract at
`codecraftai-sysman-common/specs/contracts/backup-restore/contract.md`.

macOS ships a first-party backup framework (Time Machine, driven by
`tmutil`) that is the default for every backup workflow on the platform.
However, Time Machine has constraints that make it unsuitable for some
maintenance use cases: the destination MUST be an APFS volume formatted for
Time Machine; the destination CANNOT be a remote-mounted SMB share without
auto-mount configuration; and Time Machine cannot back up paths to a
non-Time-Machine target directory. The `MacosBackupKind` enum therefore
exposes `RSYNC` and `RESTIC` as opt-in alternatives for cases where the
operator needs more flexibility.

All system commands (`tmutil`, `rsync`, `restic`, `tar`, `tee`, `cat`, `ls`,
`rm`) are executed through the injected `ICommandRunner`. The module never
directly calls `subprocess`, `os`, or `pathlib` write APIs; it constructs
argv lists and optional stdin payloads, then forwards them to the runner.
This keeps the business logic separate from execution and makes the feature
fully testable via mocks without root access or live `tmutil` / `restic` /
`rsync` binaries.

The feature is strictly scoped to **file-tree backup and restore**. It does
not capture APFS snapshots (that is the `storage-management` feature's
responsibility, which can call `tmutil localsnapshot`); it does not back up
databases; it does not perform FileVault key escrow. Schedule wiring is
delegated to the `scheduled-tasks` feature.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`). Aspect
composition (persistence, retry, timeout) is transparent to the manager.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                      │
│  - Configures aspects (optional)                           │
│  - Injects runner into MacosBackupRestoreManager           │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│             MacosBackupRestoreManager                       │
│  ccai.sysman.macos.backuprestore                        │
│                                                             │
│  create_backup_job()      run_backup_now()                  │
│  delete_backup_job()      restore_from_backup()             │
│  list_backups()           verify_backup()                   │
│                          get_job_status()                   │
└──────────┬──────────────────────────────────────────────────┘
           │ delegates to
           ▼
┌────────────────────────────┐
│      ICommandRunner        │
│  run(args, elevated, ...)  │
│  → RunResult               │
└────────────┬───────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
┌─────────────┐  ┌─────────────┐
│ LocalRunner │  │ SSHRunner   │
│ subprocess  │  │ SSH channel │
│ sudo elev.  │  │ reliability │
└─────────────┘  └─────────────┘
```

The manager NEVER constructs a subprocess directly and NEVER reaches into
`pathlib`, `os.makedirs`, or `os.remove`.

---

## Verb / Tool Mapping

| Verb                  | TIME_MACHINE backend                                 | RSYNC backend                              | RESTIC backend                                   |
|-----------------------|------------------------------------------------------|--------------------------------------------|--------------------------------------------------|
| `create_backup_job`   | `tee <job-path>` + `tmutil setdestination <dst>`     | `tee <job-path>`                           | `tee <job-path>`                                 |
| `delete_backup_job`   | `rm -f <job-path>` + `tmutil removedestination <dst>`| `rm -f <job-path>`                         | `rm -f <job-path>`                               |
| `run_backup_now`      | `tmutil startbackup --auto`                          | `rsync -aAXv --delete <src> <dst>`         | `restic backup --password-file=/dev/stdin`       |
| `list_backups`        | `ls -1` + `cat` + `tmutil listbackups`               | `ls -1` + `cat`                            | `ls -1` + `cat`                                  |
| `restore_from_backup` | `tmutil restore <backup_path> <target>`              | `rsync -aAXv --delete <dst> <target>`      | `restic restore latest --target <target>`        |
| `verify_backup`       | `tmutil compare`                                     | best-effort `rsync -an --checksum`         | `restic check`                                   |
| `get_job_status`      | read spec; report `last_run_status`                  | same                                       | same                                             |

The `tee` / `rm` / `cat` / `ls` invocations all run through the same
`ICommandRunner`. There is no `IFileOperator` dependency.

---

## Backend Dispatch

The dispatch decision is made in three places: `run_backup_now`,
`restore_from_backup`, and `verify_backup`. Each verb resolves the spec from
disk (via `cat`), parses the persisted JSON, and chooses the backend by the
spec's `kind` field. The dispatch is implemented as a small `if / elif`
ladder rather than a registry, in line with this codebase's "flat is better
than nested" convention.

For `TIME_MACHINE`:

* `run_backup_now` → `tmutil startbackup --auto`. The `--auto` flag tells
  Time Machine to use the configured destination rather than requiring an
  explicit `--destination` flag, which makes the dispatch tolerant of
  destinations that have already been registered via
  `tmutil setdestination`.
* `restore_from_backup` → `tmutil restore <backup_path> <target>`. The
  `<backup_path>` is the path to a specific Time Machine snapshot directory
  (e.g. `/Volumes/Time Machine Backups/Backups.backupdb/host/2026-04-24-120000`).
  The manager defaults to the most-recent snapshot reported by
  `tmutil listbackups`.
* `verify_backup` → `tmutil compare` (best-effort).

For `RSYNC` and `RESTIC` the dispatch is identical to the Ubuntu manager;
see `../../codecraftai-sysman-linux-ubuntu/specs/3-backup-restore/design.md`
for the rationale.

---

## Time Machine Constraints

Time Machine imposes constraints that the manager does NOT pre-validate:

1. The destination MUST be an APFS volume formatted for Time Machine.
   Pre-formatting is the operator's responsibility.
2. The destination MUST be writable by `root` (Time Machine writes via the
   `backupd` daemon, which runs as `root`).
3. Time Machine ignores the spec's `excludes` field. macOS exposes
   excludes via `tmutil addexclusion` / `tmutil removeexclusion`, which
   this feature does not surface (the `scheduled-tasks` and
   `configuration-management` features can be composed to manage
   excludes if needed).
4. `tmutil compare` is a best-effort verification — it reports per-file
   differences against the source, which is operationally useful but does
   not match the round-trip checksum semantics of `restic check`.

Each constraint surfaces as a `tmutil` non-zero exit if the operator
violates it; the manager passes the captured stderr through verbatim on
the returned `MacosBackupActionResult`.

---

## Encryption Passphrase Handling

Identical to the Ubuntu manager: only the `RESTIC` backend supports
encryption; the passphrase is delivered via the runner's stdin channel and
the argv carries `--password-file=/dev/stdin`. The persisted on-disk spec
replaces the passphrase with the sentinel `"***REDACTED***"`.

Time Machine itself supports volume-level encryption (FileVault on the
destination volume), but that is configured at the volume layer rather than
per-job. This feature does NOT manage Time Machine encryption settings; the
operator pre-encrypts the destination volume.

---

## Job Persistence

Backup-job specs are persisted as JSON files at
`/var/db/ccai/backups/jobs/<job_id>.json`. macOS uses `/var/db/` rather
than `/var/lib/` for system-level state, matching the convention used by
`launchd`, `cups`, and other platform daemons.

Idempotency for `create_backup_job` is implemented by reading the existing
spec via `cat`, comparing its content byte-for-byte to the proposed
serialisation, and returning `SKIPPED` when they match.

The job directory itself is created lazily by the first `create_backup_job`
call's `tee` invocation — `tee` will fail if the parent directory does
not exist. The manager does NOT pre-create the directory; instead, it
documents that operators MUST `mkdir -p /var/db/ccai/backups/jobs` once
at host provisioning time.

---

## Module Layout

```
backuprestore.py
├── module-level constants
│   ├── _TMUTIL, _RSYNC, _RESTIC, _TEE, _CAT, _RM, _LS, _TEST
│   └── _DEFAULT_JOB_DIR
├── enums
│   └── MacosBackupKind (TIME_MACHINE, RSYNC, RESTIC)
├── data classes
│   ├── MacosBackupManagerConfig (extends FeatureConfig)
│   ├── MacosBackupActionResult  (extends FeatureActionResult)
│   ├── MacosBackupJobSpec       (alias MacosBackupSpec)
│   ├── MacosBackupInfo
│   ├── MacosBackupVerifyResult
│   └── MacosBackupJobStatus
├── exceptions
│   ├── MacosBackupQueryError
│   ├── MacosBackupNotFoundError
│   └── MacosBackupVerifyError
├── manager
│   └── MacosBackupRestoreManager
└── module helpers
    ├── _serialise_spec
    ├── _deserialise_spec
    ├── _redact_passphrase
    ├── _build_tmutil_argv
    ├── _build_rsync_argv
    ├── _build_restic_argv
    └── _job_path
```

---

## Backup-Kind Mapping

| Abstract `BackupKind` | macOS `MacosBackupKind` | Notes                                               |
|-----------------------|-------------------------|-----------------------------------------------------|
| `ARCHIVE`             | not directly supported  | Use `RSYNC` (mirror) or wrap `restic`.              |
| `MIRROR`              | `RSYNC`                 | `rsync -aAXv --delete` mirror.                      |
| (deduplicating archive)| `RESTIC`               | Restic deduplicating snapshot store.                |
| `TIME_MACHINE`        | `TIME_MACHINE`          | Native; the dominant macOS backup pattern.          |
| `SYSTEM_IMAGE`        | not supported           | Windows-only.                                       |

---

## Idempotency Decision Table

| Verb                  | Idempotent? | Skip condition                                         |
|-----------------------|-------------|--------------------------------------------------------|
| `create_backup_job`   | yes         | spec on disk byte-equivalent to proposed JSON          |
| `delete_backup_job`   | yes         | spec file absent                                       |
| `run_backup_now`      | no          | always dispatches                                      |
| `restore_from_backup` | no          | always dispatches                                      |
| `verify_backup`       | no          | always dispatches                                      |
| `list_backups`        | n/a         | read verb                                              |
| `get_job_status`      | n/a         | read verb                                              |

---

## Elevation Decision Table

| Verb                  | `elevated=` argument forwarded   |
|-----------------------|----------------------------------|
| `create_backup_job`   | `self._config.elevated`          |
| `delete_backup_job`   | `self._config.elevated`          |
| `run_backup_now`      | `self._config.elevated`          |
| `restore_from_backup` | `self._config.elevated`          |
| `verify_backup`       | `self._config.elevated`          |
| `list_backups`        | always `False`                   |
| `get_job_status`      | always `False`                   |

`tmutil` requires elevation for every write verb except `tmutil listbackups`
and `tmutil destinationinfo`, which are free for any user that can read
the destination volume. The manager honours this distinction by passing
`elevated=False` for the read verbs unconditionally, regardless of the
config value.

---

## Error Surface

`MacosBackupQueryError` is raised only by `list_backups` and
`get_job_status`, and only when the underlying read fails unexpectedly.

`MacosBackupNotFoundError` is reserved for the case where a `job_id` or
`backup_id` is supplied to `restore_from_backup`, `verify_backup`, or
`get_job_status` and the spec does not exist on disk.

`MacosBackupVerifyError` is reserved for structural errors during
verification (e.g. an archive header that cannot be parsed at all).

---

## Test Strategy

The test suite at
`source/testroots/testbase/ccai/tests/sysman/macos/test_backuprestore.py`
covers, at minimum, the following surface:

* Command construction: one test per write verb per backend.
* Idempotency: `create_backup_job` returns `SKIPPED` on matching state;
  `delete_backup_job` returns `SKIPPED` when absent.
* Dry-run: every write verb returns `DRY_RUN`.
* `list_backups` parsing.
* Time Machine dispatch: explicit test that `tmutil startbackup` is the
  argv for a `TIME_MACHINE` job.
* Credential safety: a `RESTIC` job with a passphrase produces an argv
  that does NOT contain the passphrase.
* Elevation: writes pass `elevated=True` and reads do not.
* Failure: the runner's stderr surfaces unchanged on `FAILED` results.

The tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple.

---

## Cross-OS Concept Mapping

| Concept                | macOS                                          | Ubuntu                                    | Windows                                                |
|------------------------|------------------------------------------------|-------------------------------------------|--------------------------------------------------------|
| Default backup tool    | Time Machine via `tmutil`                      | `rsync` (selected via enum)               | `wbadmin` (Windows Server) / Robocopy (client SKUs)    |
| Job persistence        | JSON spec at `/var/db/ccai/backups/`       | JSON spec at `/var/lib/ccai/backups/` | JSON spec at `%ProgramData%\CodeCraftAI\backup\`          |
| Repository layout      | TM volume + `Backups.backupdb`                 | tool-specific                             | `wbadmin` catalogue on a backup volume                 |
| Encryption native?     | Volume-level (FileVault on destination)        | only for `restic`                         | none (relies on volume-level)                          |
| Snapshot model         | block-level (HFS+/APFS hard-link snapshot)     | none                                      | block-level VHDX                                       |
| Schedule wiring        | partly built in via `tmutil` cadence           | delegated to `scheduled-tasks`            | partly built in via Task Scheduler                     |

The dominant divergence on macOS is that Time Machine is a system-managed
service rather than a user-invoked tool: the OS's `backupd` daemon
performs the actual backup, and `tmutil` merely instructs the daemon. As
a result, `tmutil startbackup --auto` returns immediately with the
backup queued — there is no synchronous completion signal in the
return code. The manager surfaces this as a `SUCCESS` result indicating
the START succeeded; verifying that the BACKUP itself succeeded requires
a follow-up call to `verify_backup`.

---

## Aspect Composition

* `RetryAspect` may transparently flip a `FAILED` result to `SUCCESS` on
  retry. Be aware that retrying `tmutil startbackup --auto` is generally
  safe (Time Machine deduplicates) but may queue a duplicate run.
* `PersistenceAspect` MUST NOT log the stdin payload from a `RESTIC` run.
* `TimeoutAspect` may abort a long `restic check`.

---

## Out of Scope

* APFS snapshot capture (delegated to `storage-management`).
* Database-consistent dumps.
* Time Machine excludes management.
* FileVault key escrow.
* Cross-host replication beyond what `tmutil` itself supports.
