# Tasks

The following implementation tasks are derived from the requirements and
design documents.

---

## Module Skeleton

- [ ] T1. Create `source/packages/ccai/sysman/macos/backuprestore.py`
      with the module docstring describing the verb-to-tool mapping, the
      Time-Machine constraint notes, and the schedule-delegation note.
- [ ] T2. Add the module-level constants for the backend tool names
      (`_TMUTIL`, `_RSYNC`, `_RESTIC`, `_TAR`), the helper tools
      (`_TEE`, `_CAT`, `_RM`, `_LS`, `_TEST`), and the default job
      directory path (`_DEFAULT_JOB_DIR = "/var/db/ccai/backups/jobs"`).
- [ ] T3. Add the `MacosBackupActionStatus = ActionStatus` re-export.

---

## Data Models

- [ ] T4. Define the `MacosBackupKind` enum with values `TIME_MACHINE`,
      `RSYNC`, `RESTIC`. Document each value's backend in the docstring.
- [ ] T5. Define the `MacosBackupManagerConfig` dataclass extending
      `FeatureConfig`, with fields
      `default_kind: MacosBackupKind = TIME_MACHINE` and
      `job_dir: str = _DEFAULT_JOB_DIR`.
- [ ] T6. Define the `MacosBackupActionResult` dataclass extending
      `FeatureActionResult`, adding `job_id: Optional[str]` and
      `target_path: Optional[str]`.
- [ ] T7. Define the `MacosBackupJobSpec` dataclass with the standard
      fields. Add the alias `MacosBackupSpec = MacosBackupJobSpec`.
- [ ] T8. Define the `MacosBackupInfo`, `MacosBackupVerifyResult`, and
      `MacosBackupJobStatus` dataclasses per the contract.
- [ ] T9. Define the `MacosBackupQueryError`, `MacosBackupNotFoundError`,
      and `MacosBackupVerifyError` exception classes.

---

## Module Helpers

- [ ] T10. Implement `_serialise_spec(spec)` returning a JSON string
      with passphrase redacted.
- [ ] T11. Implement `_deserialise_spec(text)`.
- [ ] T12. Implement `_redact_passphrase(spec)`.
- [ ] T13. Implement `_build_tmutil_argv(spec, *, mode, target)` for
      the run / restore / verify / setdestination / removedestination
      modes.
- [ ] T14. Implement `_build_rsync_argv(spec, *, mode, target)`.
- [ ] T15. Implement `_build_restic_argv(spec, *, mode, target)`.
- [ ] T16. Implement `_job_path(*, job_dir, job_id)`.

---

## Manager Class

- [ ] T17. Implement `MacosBackupRestoreManager.__init__`.
- [ ] T18. Implement `create_backup_job(spec)` per Requirements 1, 7.
      For `TIME_MACHINE` specs, invoke `tmutil setdestination` after
      the spec has been persisted.
- [ ] T19. Implement `delete_backup_job(job_id)` per Requirement 2.
      For `TIME_MACHINE` specs, invoke `tmutil removedestination`
      before removing the spec.
- [ ] T20. Implement `run_backup_now(job_id)` per Requirement 3.
- [ ] T21. Implement `list_backups()` per Requirement 4. When any
      persisted spec is `TIME_MACHINE`, also invoke
      `tmutil listbackups`.
- [ ] T22. Implement `restore_from_backup(job_id, target)` per
      Requirement 5.
- [ ] T23. Implement `verify_backup(job_id)` per Requirement 6.
- [ ] T24. Implement `get_job_status(job_id)` returning a
      `MacosBackupJobStatus`.
- [ ] T25. Implement the helper methods `_typed_config`,
      `_read_spec_text`, `_path_exists`, `_build_result`,
      `_wrap_run_result`.

---

## Tests

- [ ] T26. Create
      `source/testroots/testbase/ccai/tests/sysman/macos/test_backuprestore.py`
      with a `_MockCommandRunner` matching the prior-wave pattern.
- [ ] T27. `test_create_backup_job_persists_spec` (also asserts
      `tmutil setdestination` for TIME_MACHINE specs).
- [ ] T28. `test_create_backup_job_skipped_when_present`.
- [ ] T29. `test_create_backup_job_dry_run`.
- [ ] T30. `test_delete_backup_job_invokes_remove`.
- [ ] T31. `test_run_backup_now_dispatches_tmutil`.
- [ ] T32. `test_run_backup_now_dispatches_rsync`.
- [ ] T33. `test_run_backup_now_dispatches_restic`.
- [ ] T34. `test_list_backups_parses_ls_output`.
- [ ] T35. `test_restore_from_backup_invokes_tmutil`.
- [ ] T36. `test_restore_from_backup_invokes_rsync`.
- [ ] T37. `test_verify_backup_invokes_tmutil_compare`.
- [ ] T38. `test_verify_backup_invokes_restic_check`.
- [ ] T39. `test_writes_elevate`.
- [ ] T40. `test_reads_never_elevate`.
- [ ] T41. `test_failed_command_surfaces_stderr`.
- [ ] T42. `test_passphrase_redacted_when_restic_encrypted`.
- [ ] T43. `test_time_machine_dispatch_uses_tmutil` (one canonical
      test pinning the `tmutil` argv shape).

---

## Documentation

- [ ] T44. Update the package-level `Agents.md` entry for
      `codecraftai-sysman-macos` to point at the
      `specs/3-backup-restore/` directory.
- [ ] T45. Note in the manager's module docstring that schedule
      wiring is delegated to `scheduled-tasks`, that the
      `encryption_passphrase` field is redacted from on-disk spec
      files, and that `TIME_MACHINE` jobs ignore the spec's
      `excludes` field.
