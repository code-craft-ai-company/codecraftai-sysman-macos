# Implementation Plan — macOS Configuration Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.configurationmanagement`
  module

- [x] 2. Define `MacosConfigActionStatus` enum (aliased from
  `featurebase.ActionStatus`)

- [x] 3. Define `MacosConfigManagerConfig` dataclass (`dry_run`,
  `elevated`, `backup_suffix_format`, `defaults_command`).  Note:
  named `MacosConfigManagerConfig` rather than the spec's
  `MacosConfigurationManagerConfig` for naming-length parity with the
  `<Feature>ManagerConfig` form used elsewhere.

- [x] 4. Define `MacosConfigActionResult` and `MacosSettingValue`
  dataclasses.  **Deviation:** `MacosConfigDiffResult`,
  `MacosConfigValidationResult`, and `MacosConfigFileInfo` are NOT
  surfaced in this pilot; the `diff` / `validate` /
  `set_permissions` / `set_ownership` verbs they support are
  deferred (see Gaps).

- [x] 5. Define `MacosConfigQueryError`.  **Deviation:** the spec
  also called for `MacosConfigFileNotFoundError`,
  `MacosConfigTemplateError`,
  `MacosConfigValidatorNotFoundError`, and
  `MacosConfigSettingNotFoundError`.  The pilot consolidates every
  read failure into the single `MacosConfigQueryError` for parity
  with the Ubuntu sibling; the more granular error types are
  deferred until the validator / template features land.

## ValidatorRegistry

- [ ] 6. Define `MACOS_VALIDATORS` module-level dict — deferred

- [ ] 7. Implement `register_validator(id, builder)` runtime hook —
  deferred

## MacosConfigurationManager — Implemented

- [x] 8. Implement `__init__` accepting `runner` and optional
  `config` (no `IFileOperator`; file I/O routes through the runner)

- [x] 9. Implement `_is_plist_source(source)` returning True for
  `*.plist`, `/Library/Preferences/*`, or `~/Library/Preferences/*`.
  This collapses the spec's three-way `_classify_source` into a
  binary plist-vs-text decision because `defaults` accepts both
  domain names and full paths via the same argument shape.

- [x] 10. Implement `_infer_plist_type(value)` returning the
  correct `defaults write -<type>` flag.  Pilot supports `-bool`
  (for "true"/"false"), `-int` (for digit-only strings), and
  `-string` (everything else).  Floats / arrays / nested dicts are
  documented as the next-tier follow-up.

- [x] 11. Implement `_parse_defaults_read(text)` for top-level
  `key = value;` rows.  Nested dicts and arrays are intentionally
  skipped (the spec's "flatten to dotted keys" goal is deferred).

- [ ] 12. Implement `_stat(source)` parsing `stat -f "%A %Su %Sg"`
  output — deferred (used by `set_permissions` / `set_ownership`).

- [x] 13. Implement `read_setting(source, key)` with plist +
  plain-text dispatch

- [x] 14. Implement `write_setting(source, key, value)` with plist
  + plain-text dispatch and idempotency guard

- [x] 15. Implement `remove_setting(source, key)` with plist +
  plain-text dispatch and idempotency guard

- [x] 16. Implement `list_settings(source)` with plist + plain-text
  dispatch

- [ ] 17. Implement `write(source, content, mode, owner, group)`
  whole-file write — deferred

- [ ] 18. Implement `diff(source, desired_content)` — deferred

- [ ] 19. Implement `validate(source, validator_id)` — deferred

- [x] 20. Implement `backup_configuration(source)` via `cp -p` to a
  sibling timestamped path

- [x] 21. Implement `restore_configuration(backup_path,
  target_path)` via `cp -p` with a `cmp -s` byte-match idempotency
  guard

- [ ] 22. Implement `set_permissions(source, mode)` — deferred

- [ ] 23. Implement `set_ownership(source, user, group)` — deferred

- [x] 24. Implement `reload_configuration(service)` via
  `launchctl kickstart -k system/<service>`.  Note: user-domain
  reloads (`gui/<uid>/<service>`) are out of scope for this manager
  because per-user UID resolution lives on the service-management
  manager — that follow-up is tracked below.

## MacosConfigurationManager — Gaps (specified but not implemented)

- [ ] 25. Profile-based configuration (`profiles install/remove`) —
  intentionally out of scope

- [ ] 26. MDM-managed preferences (ForceRestart keys etc.) — not
  surfaced distinctly from user-writable keys

- [ ] 27. Template rendering helper (`write_template`) using
  `string.Template` — not exposed yet

## Tests — Implemented

- [x] 28. `test_read_setting_plist_runs_defaults_read`
- [x] 29. `test_read_setting_text_parses_value` (covered by
  `test_routing_plain_text_uses_cat`)
- [x] 30. `test_read_setting_missing_key_raises`
- [x] 31. `test_write_setting_plist_infers_string_type`
  (plus `_infers_int_type` and `_infers_bool_type`)
- [x] 32. `test_write_setting_text_rewrites_content`
- [x] 33. `test_write_setting_skipped_when_value_matches`
  (`_plist_skipped_when_value_matches` and
  `_text_skipped_when_value_matches`)
- [x] 34. `test_write_setting_dry_run_no_runner_call`
- [x] 35. `test_remove_setting_plist_runs_defaults_delete`
- [x] 36. `test_remove_setting_text_rewrites_without_key`
- [x] 37. `test_list_settings_plist_parses_defaults_read`
  (top-level entries; nested dicts skipped — see task 11 deviation)
- [x] 38. `test_list_settings_text_preserves_keys`
- [ ] 39. `test_write_plist_uses_import_temp_file_sequence` —
  N/A; whole-file plist write deferred (see task 17)
- [ ] 40. `test_diff_plist_uses_defaults_export` — deferred with
  task 18
- [ ] 41. `test_validate_unknown_id_raises_validator_not_found` —
  deferred with task 19
- [ ] 42. `test_validate_plist_uses_plutil_lint` — deferred with
  task 19
- [ ] 43. `test_backup_plist_exports_to_var_backups` — N/A; plist
  backup uses the same `cp -p` path as plain files (no
  `defaults export` round-trip in the pilot)
- [x] 44. `test_backup_copies_with_timestamp`
- [x] 45. `test_restore_skipped_when_byte_match` plus
  `test_restore_dispatches_cp_when_differs` (replaces the spec's
  pattern-based routing test, which is N/A in this single-routing
  implementation)
- [ ] 46. `test_set_permissions_refuses_plist_domain` — deferred
  with task 22
- [ ] 47. `test_set_ownership_refuses_plist_domain` — deferred
  with task 23
- [x] 48. `test_reload_configuration_runs_launchctl_kickstart`

## Tests — Gaps

- [x] 49. Dry-run matrix (`test_write_setting_dry_run_no_runner_call`
  for plain-text; full matrix still pending)

- [x] 50. Elevation matrix
  (`test_reads_never_elevate_even_when_config_elevated`,
  plus per-write tests asserting `elevated=True` propagation)

- [ ] 51. `_infer_plist_type` table-driven test for all Python input
  types (bool, int, float, list, dict, str) — deferred follow-up

## Follow-ups

- [ ] 52. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/configuration-management/contract.md`
  and that the validator registry is consistent with the macOS row.

- [ ] 53. **codecraftai-sysman-macos reuse check**: confirm that every
  other feature which touches `/Library/Preferences/` or `/etc/`
  delegates to `MacosConfigurationManager` rather than invoking
  `defaults` or writing files directly.

- [ ] 54. **Plist type inference enhancement**: extend
  `_infer_plist_type` to handle floats, arrays, and nested dicts —
  likely via a richer "value is already a Python object" signature
  rather than the current "value is always a string" shape.

- [ ] 55. **User-domain reload**: extend `reload_configuration` to
  accept a scope selector mirroring
  `MacosServiceManager.reload_service`, so callers can reload
  user-domain agents (`gui/<uid>/<service>`).
