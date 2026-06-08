# Implementation Plan — macOS Filesystem Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  This batch lands the initial
``MacosFilesystemManager`` along with its data models, parsers, and a
unit-test suite that runs entirely against an in-process command-runner
mock.  All file I/O is performed via the runner (``cat`` / ``tee``);
the manager does NOT consume an :class:`IFileOperator`.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.filesystemmanagement` module

- [x] 2. Define `MacosFilesystemActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN) — re-exports `featurebase.ActionStatus`.

- [x] 3. Define `MacosFilesystemManagerConfig` dataclass (dry_run,
  elevated, fstab_path)

- [x] 4. Define `MacosFilesystemActionResult` dataclass (path,
  device, mountpoint, message)

- [x] 5. Define `MacosMountInfo`, `MacosFstabEntry`, `MacosFileInfo`
  dataclasses.  ``MacosDiskUsageInfo`` from the spec is parked under
  the follow-ups; this batch does not implement ``query_disk_usage``.

- [x] 6. Define `MacosFilesystemQueryError` exception.

## MacosFilesystemManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`).  No file-operator parameter
  per the established macOS pattern — file I/O is done via ``cat`` /
  ``tee`` through the runner.

- [x] 8. Implement `mount(device, mountpoint, fstype, options)` —
  idempotent via BSD `mount` parser.  Generic enough to accept the
  spec's full fstype set (``hfs`` / ``apfs`` / ``smbfs`` / ``nfs`` /
  ``exfat`` / ``msdos`` / ``webdav``); the manager passes ``-t
  <fstype>`` through verbatim.

- [x] 9. Implement `unmount(mountpoint)` — `umount <mountpoint>`.  The
  ``lazy=True`` toggle (spec item 9) that maps to
  ``diskutil unmount force`` is parked under the follow-ups; the
  base verb returns the canonical BSD ``umount`` argv for cross-OS
  parity.

- [x] 10. Implement `add_fstab_entry(entry)` — read-modify-write
  ``/etc/fstab`` via ``cat`` (read) / ``tee -a`` (append) through the
  runner.  Skips when an exact match (device + mountpoint + fstype +
  options) is already present.  The spec's "update existing entry"
  semantics (replace when device/mountpoint match but other fields
  differ) is parked as a follow-up; this batch only appends.

- [x] 11. Implement `remove_fstab_entry(mountpoint)` — filter-based
  rewrite via ``tee <fstab_path>``.  Comments and unrelated rows are
  preserved verbatim.  The spec verb signature lists
  ``device_or_mountpoint``; this batch filters by mountpoint only,
  matching the cross-OS contract's
  ``remove_fstab_entry(mountpoint)``.

- [x] 12. Implement `get_mounts()` — BSD `mount` output parser
  (anchors on the LAST `` on `` so device names containing the
  literal substring still parse correctly).

- [x] 17. Implement `chown(path, user, group, recursive)` /
  `chmod(path, mode, recursive)` — idempotent via current ``stat``
  for the non-recursive path; recursive invocations always run.

- [x] 18. Implement `stat(path)` — BSD `stat -f
  '%Su:%Sg:%Lp:%z:%Sm'` parser, ``None`` on non-zero exit (matches
  the contract's "missing path -> None" rule).

- [x] 20. Implement `_dispatch_filesystem_write` /
  ``_build_result`` helpers — dry-run short-circuit, runner-result
  mapping, elevation decision (writes elevate, reads do not).

## MacosFilesystemManager — Gaps (specified but not implemented)

- [ ] 13. ``create_directory(path, mode, owner, group)`` —
  ``mkdir -p`` + conditional ``chmod`` + ``chown``.  Parked: the
  primitives (``chmod`` + ``chown``) are present; orchestration not
  yet provided.

- [ ] 14. ``remove_directory(path, recursive)`` — ``rmdir`` /
  ``rm -rf``.  Parked.

- [ ] 15. ``create_symlink(link, target)`` — ``ln -s`` / ``ln -snf``.
  Parked.

- [ ] 16. ``remove_symlink(link)``.  Parked.

- [ ] 19. ``query_disk_usage(path)`` — ``df -b`` parser with
  512-byte block -> byte conversion.  Parked; ``MacosDiskUsageInfo``
  not yet defined.

- [ ] 21. Disk-Arbitration persistence extension
  (``persist_via_launchd``).  Parked.

- [ ] 22. APFS volume creation / deletion (``diskutil apfs
  addVolume``) — not in base contract.

- [ ] 23. FileVault / encrypted-volume operations — not in base
  contract.

- [ ] 24. Hard-link creation — not in base contract.

- [ ] 24a. ``unmount(mountpoint, lazy=True)`` — ``diskutil unmount
  force`` fallback.

- [ ] 24b. ``add_fstab_entry`` "update on partial match" semantics.

- [ ] 24c. ``add_fstab_entry`` dry-run unified diff emission.

## Tests — Implemented

- [x] 25. `test_mount_constructs_argv_with_optional_options`
- [x] 26. `test_mount_skipped_when_already_mounted`
- [x] 27. `test_unmount_invokes_umount`
- [x] 28. `test_unmount_skipped_when_not_mounted`
- [x] 29. `test_add_fstab_entry_appends_via_tee`
- [x] 30. `test_add_fstab_entry_skipped_when_match_present`
- [x] 31. `test_remove_fstab_entry_filters_lines_via_tee`
- [x] 32. `test_remove_fstab_entry_skipped_when_no_match`
- [x] 33. `test_chown_skipped_when_owner_and_group_match`
- [x] 34. `test_chown_dispatches_user_group_token`
- [x] 35. `test_chown_recursive_emits_minus_r_and_skips_idempotency`
- [x] 36. `test_chmod_skipped_when_mode_matches`
- [x] 37. `test_chmod_dispatches_when_mode_differs`
- [x] 38. `test_get_mounts_parses_bsd_mount_output`
- [x] 39. `test_stat_parses_bsd_format_output`
- [x] 40. `test_stat_returns_none_for_missing_path`
- [x] 41. `test_dry_run_short_circuits_mount`
- [x] 42. `test_dry_run_short_circuits_chmod`
- [x] 43. `test_writes_pass_elevated_when_configured`
- [x] 44. `test_stat_call_never_elevates`
- [x] 45. `test_parse_bsd_mount_output_handles_complex_options`
- [x] 46. `test_parse_bsd_mount_output_anchors_on_last_on_token`
- [x] 47. `test_parse_bsd_stat_output_preserves_modification_string`
- [x] 48. `test_render_fstab_line_uses_tab_separators`
- [x] 49. `test_fstab_contains_match_ignores_comments`
- [x] 49a. `test_fstab_filter_out_mountpoint_preserves_comments`
- [x] 49b. `test_mode_matches_handles_leading_zero`
- [x] 49c. `test_file_info_and_mount_info_round_trip`

## Tests — Gaps

- [ ] 50. ``test_create_directory_orchestrates_mkdir_chmod_chown`` —
  blocked on follow-up 13.

- [ ] 51. ``test_create_directory_skipped_when_matching`` — blocked
  on follow-up 13.

- [ ] 52. ``test_create_symlink_*`` — blocked on follow-up 15.

- [ ] 53. ``test_query_disk_usage_converts_512_byte_blocks_to_bytes``
  — blocked on follow-up 19.

- [ ] 54. ``test_unmount_lazy_uses_diskutil_force`` — blocked on
  follow-up 24a.

- [ ] 55. Property-based test for `/etc/fstab` round-trip on macOS
  (arbitrary valid fstab files parse, mutate, and re-emit losslessly)

## Follow-ups

- [ ] 56. **Cross-OS contract conformance review**: verify this
  implementation's `MacosFilesystemActionResult` field shapes match
  the cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/filesystem-management/contract.md`.

- [ ] 57. **codecraftai-* reuse check**: confirm that no other feature in
  `codecraftai-sysman-macos` invokes `mount` / `umount` / `chmod` /
  `chown` / ``stat`` directly; all such calls should go through
  `MacosFilesystemManager`.

- [ ] 58. **fstab on macOS is rare**: while ``/etc/fstab`` is honoured
  by macOS at boot, most automation flows use ``diskutil`` plist
  configuration instead.  Document the expected workflow and consider
  whether this manager should validate that ``fstab_path`` is
  writable before attempting ``tee``-based edits, or whether the
  contract should expose a ``persist_via_diskutil`` knob for the
  modern path.

- [ ] 59. **BSD ``stat`` format**: the format string ``%Su:%Sg:%Lp:%z:%Sm``
  differs from GNU's ``%U:%G:%a:%s:%y``.  Add a documented mapping
  table in the cross-OS contract reference so coordinators that
  consume :class:`MacosFileInfo` and the Linux peers' info dataclass
  can normalise the timestamp string consistently (BSD reports a
  ctime-style human-readable form by default while GNU reports an
  ISO-ish form).
