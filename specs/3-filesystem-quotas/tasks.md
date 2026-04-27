# Implementation Plan — Filesystem Quotas (macOS)

All tasks unchecked — this is a new feature with no existing
implementation in `automojo-sysman-macos`.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/macos/filesystemquotas.py`
  - _Requirements: All_

- [ ] 2. Define `MacosQuotaActionStatus` (alias `ActionStatus`)

- [ ] 3. Define `MacosFilesystemQuotasManagerConfig` dataclass
  with `quota_aware_filesystems: list[str]` defaulting via
  `field(default_factory=lambda: ["hfs", "ufs"])`
  - _Requirements: 1_

- [ ] 4. Define `MacosQuotaActionResult` (subclasses
  `FeatureActionResult`) with `mountpoint: str | None` and
  `principal: str | None`

- [ ] 5. Define `MacosQuotaPrincipalKind` enum
  - _Requirements: 4, 5, 8_

- [ ] 6. Define `MacosQuotaInfo` dataclass

- [ ] 7. Define `MacosQuotaStatus` dataclass

- [ ] 8. Define `MacosQuotaQueryError(Exception)` and
  `MacosQuotaFilesystemNotMountedError(Exception)`
  - _Requirements: 6, 7, 8, 9, 10_

## Helpers

- [ ] 9. Implement `__init__` accepting runner + optional
  config (delegate to `BaseFeatureManager`)

- [ ] 10. Implement `_detect_fstype(mountpoint)` — runs
  ``mount`` (no elevation), parses each line via the LAST
  ``" on "`` anchor, returns the fstype token or raises
  ``MacosQuotaFilesystemNotMountedError``
  - _Requirements: 10_

- [ ] 11. Implement `_best_effort_fstype(mountpoint)` — like
  ``_detect_fstype`` but returns ``None`` on miss; used by
  reads so they never raise on a stale mountpoint

- [ ] 12. Implement `_is_quota_aware(fstype)` — membership
  check against ``config.quota_aware_filesystems``

## Enable / Disable

- [ ] 13. Implement `enable_quotas(mountpoint)`:
  - Dry-run guard returning DRY_RUN with quotacheck argv
  - `_detect_fstype` for mount validation
  - APFS / unknown gate → FAILED detail="apfs_no_quota_support"
  - Probe `get_quota_status`; SKIPPED when both kinds active
  - Dispatch `quotacheck -ug <mountpoint>`; on failure return
  - Dispatch `quotaon -ug <mountpoint>`
  - _Requirements: 1, 2, 12.1_

- [ ] 14. Implement `disable_quotas(mountpoint)`:
  - Dry-run guard
  - `_detect_fstype`
  - APFS gate → FAILED
  - Probe; SKIPPED when both off
  - Dispatch `quotaoff -ug <mountpoint>`
  - _Requirements: 3, 12.2_

## Limits

- [ ] 15. Implement `set_user_quota(mountpoint, user,
  soft_bytes, hard_bytes)`:
  - Dry-run guard
  - `_detect_fstype`
  - APFS gate → FAILED
  - Probe `get_user_quota`; SKIPPED on match
  - Dispatch `setquota -u <user> <soft_kb> <hard_kb> 0 0
    <mountpoint>`
  - _Requirements: 4, 12.3_

- [ ] 16. Implement `set_group_quota(...)` mirroring `-g`
  - _Requirements: 5, 12.4_

## Queries

- [ ] 17. Implement `get_user_quota(mountpoint, user)`:
  - `_best_effort_fstype`
  - APFS / unknown → return None
  - Dispatch `quota -u <user>` (no elevation)
  - Exit 0/1/2 → parse; else raise `MacosQuotaQueryError`
  - Match the row; return None when no match
  - _Requirements: 6_

- [ ] 18. Implement `get_group_quota(...)` mirroring `-g`
  - _Requirements: 7_

- [ ] 19. Implement `list_quotas(mountpoint)`:
  - `_best_effort_fstype`; APFS → return []
  - Dispatch `repquota -ug <mountpoint>` (no elevation)
  - Non-zero → raise `MacosQuotaQueryError`
  - Parse the two-section output
  - _Requirements: 8_

- [ ] 20. Implement `get_quota_status(mountpoint)`:
  - `_best_effort_fstype`; APFS → return disabled status
  - Dispatch `quotaon -p <mountpoint>` (no elevation)
  - Non-zero → raise `MacosQuotaQueryError`
  - Parse user/group on/off lines
  - _Requirements: 9_

## Parsers

- [ ] 21. Implement `_parse_mount_output(stdout, mountpoint)`
  using a LAST-`" on "` anchor

- [ ] 22. Implement `_parse_quota_output(...)` for
  ``quota -u/-g``

- [ ] 23. Implement `_parse_repquota_output(...)`

- [ ] 24. Implement `_parse_quotaon_status(...)`

## Tests

- [ ] 25. Create `test_filesystemquotas.py` with `_MockCommandRunner`

- [ ] 26. Test `enable_quotas` invokes `quotacheck` then
  `quotaon`

- [ ] 27. Test `enable_quotas` returns SKIPPED when both kinds
  already active

- [ ] 28. Test `enable_quotas` returns DRY_RUN

- [ ] 29. Test `disable_quotas` invokes `quotaoff -ug`

- [ ] 30. Test `set_user_quota` invokes `setquota -u ...`

- [ ] 31. Test `set_user_quota` returns SKIPPED when current
  matches

- [ ] 32. Test `set_group_quota` invokes `setquota -g ...`

- [ ] 33. Test `get_user_quota` parses `quota -u` output;
  None for unknown

- [ ] 34. Test `get_group_quota` parses `quota -g` output

- [ ] 35. Test `list_quotas` parses `repquota -ug`

- [ ] 36. Test `get_quota_status` parses `quotaon -p`

- [ ] 37. Test writes pass `elevated=True` when configured

- [ ] 38. Test reads never elevate

- [ ] 39. Test failed `quotacheck` surfaces stderr

- [ ] 40. Test APFS write returns FAILED with
  `detail="apfs_no_quota_support"`
