# Implementation Plan — macOS SSH-Server-Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.sshserverconfiguration`
  module

- [x] 2. Define `MacosSshActionStatus` alias of the shared
  `ActionStatus` enum (SUCCESS / FAILED / SKIPPED / DRY_RUN)

- [x] 3. Define `MacosSshServerConfigurationManagerConfig` dataclass
  (`dry_run`, `elevated`, `sshd_config_path`,
  `authorized_keys_path_template`, `service_name`,
  `service_domain`)

- [x] 4. Define `MacosSshActionResult` dataclass (adds `keyword`,
  `path`, `user`, `message` fields over `FeatureActionResult`)

- [x] 5. Define `MacosSshSettingValue`, `MacosSshMatchBlock`, and
  `MacosSshAuthorizedKey` query dataclasses

- [x] 6. Define `MacosSshQueryError` exception

## MacosSshServerConfigurationManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 8. Implement `get_sshd_setting(keyword)` — `cat
  /etc/ssh/sshd_config` + parse + return first top-level match or
  `None`

- [x] 9. Implement `list_sshd_settings()` — `cat` + parse + return
  top-level dict

- [x] 10. Implement `set_sshd_setting(keyword, value)` — read, parse,
  upsert, render, idempotency-skip on byte-equal, dispatch
  `tee /etc/ssh/sshd_config`

- [x] 11. Implement `remove_sshd_setting(keyword)` — read, parse,
  drop top-level occurrences, render, idempotency-skip when absent
  or content-equal, dispatch `tee`

- [x] 12. Implement `add_match_block(criteria, settings)` — read,
  parse, upsert block (whitespace-collapsed criteria match),
  render, dispatch `tee`

- [x] 13. Implement `remove_match_block(criteria)` — read, parse,
  drop matching block, render, idempotency-skip when no match,
  dispatch `tee`

- [x] 14. Implement `list_match_blocks()` — `cat` + parse + return
  blocks in declaration order

- [x] 15. Implement `reload_sshd()` — dispatch `launchctl kickstart
  -k system/com.openssh.sshd` (with domain + name composed from
  config)

- [x] 16. Implement `test_sshd_config()` — dispatch `sshd -t`

- [x] 17. Implement `list_authorized_keys(user)` — resolve path from
  template (default `/Users/{user}/.ssh/authorized_keys`), `cat`,
  parse with options + comment + fingerprint

- [x] 18. Implement `add_authorized_key(user, key)` — parse, FAILED
  on parse failure, idempotency-skip by fingerprint, dispatch
  `tee`

- [x] 19. Implement `remove_authorized_key(user, fingerprint)` —
  drop every line whose computed fingerprint matches, dispatch
  `tee`

- [x] 20. Implement helper `_parse_sshd_config(content)` — returns
  `(top_settings, match_blocks)`

- [x] 21. Implement helper `_render_sshd_config(top_settings,
  match_blocks)` — deterministic rendering, four-space-indented
  Match-block bodies

- [x] 22. Implement helper `_parse_authorized_keys(content)` and
  `_parse_authorized_key_line(line)` — recognise common OpenSSH
  key types, capture options prefix and trailing comment

- [x] 23. Implement helper `_compute_fingerprint(key_data)` —
  SHA-256 of base64-decoded body, base64-encoded, padding stripped

## MacosSshServerConfigurationManager — Gaps

- [ ] 24. Comment-line preservation across mutation.  Tracked as a
  shared follow-up across all OS implementations.

- [ ] 25. Pre-write `sshd -t` validation with rollback.  Same
  follow-up as the Ubuntu sibling.

- [ ] 26. Per-user authorized-key writes without elevation.  Currently
  honours `config.elevated` uniformly.

- [ ] 27. ``launchctl bootstrap`` / ``bootout`` for first-run
  enable/disable.  Belongs to `service-management`, not this
  feature.

- [ ] 28. Apple System Settings → Sharing → Remote Login toggle
  parity.  Out of scope per the contract; the user flow may
  require ``systemsetup -setremotelogin`` which is independent of
  `sshd_config`.

- [ ] 29. SIP-related caveats.  Writing to `/etc/ssh/` is permitted
  under SIP but writing under `/usr/sbin/` (where `sshd` lives) is
  not.  This module only writes to `/etc/ssh/`, so SIP is not a
  blocker.

## Tests — Implemented

- [x] 30. `test_get_sshd_setting_parses_value`
- [x] 31. `test_list_sshd_settings_parses_top_level`
- [x] 32. `test_list_match_blocks_parses_blocks`
- [x] 33. `test_list_authorized_keys_parses_user_keys`
- [x] 34. `test_set_sshd_setting_writes_when_absent`
- [x] 35. `test_set_sshd_setting_skipped_when_already_set`
- [x] 36. `test_set_sshd_setting_replaces_existing_value`
- [x] 37. `test_dry_run_short_circuits_set_sshd_setting`
- [x] 38. `test_remove_sshd_setting_skipped_when_absent`
- [x] 39. `test_remove_sshd_setting_rewrites_file`
- [x] 40. `test_add_match_block_appends_to_config`
- [x] 41. `test_remove_match_block_skipped_when_absent`
- [x] 42. `test_remove_match_block_rewrites_file`
- [x] 43. `test_reload_sshd_invokes_launchctl_kickstart`
- [x] 44. `test_test_sshd_config_invokes_sshd_dash_t`
- [x] 45. `test_add_authorized_key_writes_when_absent`
- [x] 46. `test_add_authorized_key_skipped_when_already_present`
- [x] 47. `test_add_authorized_key_failed_on_parse_error`
- [x] 48. `test_remove_authorized_key_finds_by_fingerprint`
- [x] 49. `test_remove_authorized_key_skipped_when_fingerprint_absent`
- [x] 50. `test_writes_pass_elevated_true`
- [x] 51. `test_reads_never_elevate`
- [x] 52. `test_failed_command_returns_failed`
- [x] 53. `test_parser_handles_match_block_followed_by_eof`

## Tests — Gaps

- [ ] 54. Property-based hypothesis coverage.

- [ ] 55. Integration test on a real macOS host that exercises
  `launchctl kickstart -k` against the system-domain
  `com.openssh.sshd` daemon.

## Spec Deviations / Revisions Needed

- [ ] 56. **Reload mechanism divergence**: the contract's RHEL
  reference uses `systemctl`; macOS uses `launchctl kickstart -k`.
  Captured in `MacosSshServerConfigurationManagerConfig.service_name`
  + `service_domain` defaults.  No contract amendment required —
  the contract's "Reload mechanism" column already lists the macOS
  command explicitly.

- [ ] 57. **Type naming**: the contract uses `SshdSetting` /
  `SshdMatchBlock` / `AuthorizedKey` (with OS prefixes).  This
  module uses `MacosSshSettingValue` / `MacosSshMatchBlock` /
  `MacosSshAuthorizedKey`.  The `SettingValue` suffix tracks the
  prior wave-1 naming used by `configurationmanagement.py`.

## Follow-ups

- [ ] 58. **Cross-OS contract conformance review**.

- [ ] 59. **Shared `featurebase` upstreaming**: vendored from RHEL.
