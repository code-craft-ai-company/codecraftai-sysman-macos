# Implementation Plan — macOS Encryption Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.encryptionmanagement`
  module.

- [x] 2. Define `MacosEncryptionActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `MacosEncryptionVolumeKind` enum with `BOOT` and
  `DATA`.

- [x] 4. Define `MacosEncryptionManagerConfig` dataclass with
  `allow_destructive`, `default_cipher` extras on top of the base
  `dry_run` / `elevated`.

- [x] 5. Define `MacosEncryptionActionResult` dataclass with
  `volume`, `device`, `requires_reboot` extras.

- [x] 6. Define `MacosEncryptionSpec`, `MacosEncryptionStatus`,
  `MacosEncryptedVolumeInfo` dataclasses.

- [x] 7. Define `MacosEncryptionQueryError` exception.

- [x] 8. Define module-level `_DESTRUCTIVE_GUARD_DETAIL`,
  `_DEFAULT_CIPHER`, `_BOOT_VOLUME_PATHS` constants.

## MacosEncryptionManager — Implemented

- [x] 9. `__init__` accepting runner + optional config (inherited
  from `BaseFeatureManager`).

- [x] 10. `enable_encryption(volume, spec)` — routes to
  `fdesetup enable -inputplist` (boot) or
  `diskutil apfs encryptVolume` (data); passphrase via stdin
  (direct or plist-wrapped); idempotent via boot/data status probe.

- [x] 11. `disable_encryption(volume)` — routes to
  `fdesetup disable` (boot) or `diskutil apfs decryptVolume` (data);
  destructive; gated by `config.allow_destructive`.

- [x] 12. `get_encryption_status(volume)` — routes to
  `fdesetup status` or `diskutil info -plist`; returns a
  `MacosEncryptionStatus` carrying cipher / progress /
  protector_types.

- [x] 13. `list_encrypted_volumes()` — `diskutil apfs list -plist`
  filtered to encrypted volumes.

- [x] 14. `rotate_key(volume)` — `fdesetup changerecovery -personal`
  (boot only); data-volume calls return FAILED with a descriptive
  detail.

- [x] 15. `backup_recovery_key(volume, path)` — `fdesetup
  showrecoverykey` then `tee <path>` with the recovery key on
  stdin; boot only.

- [x] 16. `unlock_volume(volume, passphrase)` — `diskutil apfs
  unlockVolume <volume>` with passphrase via stdin; idempotent via
  `diskutil info -plist`'s `Locked` key; boot-volume calls return
  SKIPPED.

- [x] 17. `_classify_volume` helper — `BOOT` for `/`, `/System`,
  `boot`, `system`; `DATA` otherwise.

- [x] 18. `_is_encrypted` helper — fdesetup-or-diskutil probe.

- [x] 19. `_diskutil_info` helper — `diskutil info -plist <volume>`
  with plist parsing.

- [x] 20. `_dispatch_encryption_write` and `_build_result` helpers
  that thread `volume` / `device` / `requires_reboot` through
  `BaseFeatureManager._dispatch`.

## Module-level helpers — Implemented

- [x] 21. `_build_fdesetup_inputplist` — emits the canonical
  `<plist><dict><key>Username</key>...</dict></plist>` payload.

- [x] 22. `_parse_fdesetup_status` — detects FileVault on/off and
  the in-flight progress percent.

- [x] 23. `_parse_plist_dict` — tolerant `plistlib.loads` wrapper
  that returns ``None`` on malformed input.

- [x] 24. `_parse_apfs_list_plist` — walks the
  `Containers[].Volumes[]` tree and emits one
  `MacosEncryptedVolumeInfo` per encrypted volume.

## MacosEncryptionManager — Gaps (specified but not implemented)

- [ ] 25. APFS data-volume rotate-key support (today's surface
  rejects with a descriptive FAILED detail).

- [ ] 26. APFS data-volume recovery-key backup (today's surface
  rejects with a descriptive FAILED detail).

- [ ] 27. Volume-UUID resolver so callers can pass an APFS UUID
  instead of a mount point or device node.

- [ ] 28. Strict-read mode for `get_encryption_status` /
  `list_encrypted_volumes` that raises
  `MacosEncryptionQueryError` instead of returning empty / plain
  records.

- [ ] 29. Institutional-recovery-key (`fdesetup
  changerecovery -institutional`) support.

- [ ] 30. iCloud-escrowed FileVault binding (a macOS-specific
  extension).

## Tests — Implemented

- [x] 31. `test_enable_encryption_invokes_fdesetup_for_boot_volume`
- [x] 32. `test_enable_encryption_invokes_diskutil_for_data_volume`
- [x] 33. `test_enable_encryption_skipped_when_already_encrypted`
- [x] 34. `test_enable_encryption_dry_run_short_circuits`
- [x] 35. `test_disable_encryption_requires_allow_destructive`
- [x] 36. `test_disable_encryption_invokes_fdesetup_when_allowed`
- [x] 37. `test_get_encryption_status_parses_fdesetup_for_boot`
- [x] 38. `test_get_encryption_status_parses_diskutil_for_data`
- [x] 39. `test_list_encrypted_volumes_filters_correctly`
- [x] 40. `test_rotate_key_invokes_fdesetup_changerecovery_for_boot`
- [x] 41. `test_backup_recovery_key_writes_to_path`
- [x] 42. `test_unlock_volume_passes_passphrase_via_stdin`
- [x] 43. `test_unlock_volume_skipped_when_already_unlocked`
- [x] 44. `test_passphrase_redacted_in_recorded_command`
- [x] 45. `test_writes_elevate`
- [x] 46. `test_reads_never_elevate`
- [x] 47. `test_failed_command_surfaces_stderr`
- [x] 48. `test_parse_fdesetup_status_detects_on_and_progress`
- [x] 49. `test_parse_plist_dict_round_trips_xml`
- [x] 50. `test_parse_apfs_list_emits_only_encrypted_volumes`

## Tests — Gaps

- [ ] 51. `rotate_key` data-volume rejection test (today the
  manager returns FAILED; a test would lock the contract).
- [ ] 52. `backup_recovery_key` data-volume rejection test.
- [ ] 53. `unlock_volume` boot-volume SKIPPED test.
- [ ] 54. `enable_encryption` missing-user rejection test.

## Follow-ups

- [ ] 55. **Cross-OS contract conformance review**: verify
  `MacosEncryptionActionResult` field shapes match the cross-OS
  contract.

- [ ] 56. **codecraftai-* reuse check**: confirm that no other
  feature in `codecraftai-sysman-macos` invokes `fdesetup` or
  `diskutil apfs` directly; all such calls should go through
  `MacosEncryptionManager`.

## Spec deviations from this implementation

- **No data-volume rotate-key.**  macOS native tooling does not
  expose an APFS data-volume passphrase rotation verb that does
  not require both old and new passphrases on a single
  interactive prompt.  The manager rejects data-volume rotate-key
  calls with `detail="rotate_key_unsupported_for_data_volume_on_macos"`.

- **No data-volume recovery-key backup.**  APFS data volumes do
  not have a separate recovery-key concept distinct from the
  user's passphrase.  The manager rejects data-volume
  backup-recovery-key calls.

- **`unlock_volume` is data-only.**  Boot-volume unlock is implicit
  in the macOS boot sequence; the manager returns SKIPPED with
  `detail="boot_volume_unlock_is_implicit"` for boot-volume calls.

- **Cipher / key-size are advisory.**  FileVault and APFS
  encryption use a fixed cipher (XTS-AES); the manager records
  `cipher="aes-xts"` on every result regardless of the spec's
  `cipher` field.

- **No volume-UUID resolver.**  Callers MUST pass a mount point
  (`/Volumes/Data`) or a device node (`/dev/disk2s5`); APFS UUIDs
  are not resolved into either form today.

- **Boot-volume disable is gated despite being non-destructive.**
  `fdesetup disable` preserves data in plaintext; the destructive
  guard fires anyway so cross-OS behaviour stays uniform with the
  Ubuntu and Windows siblings.
