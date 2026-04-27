# Implementation Plan — macOS Kernel Parameters

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.kernelparameters` module

- [x] 2. Define `MacosKernelParameterActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN) — aliased from
  `automojo.sysman.macos.featurebase.ActionStatus`

- [x] 3. Define `MacosKernelParameterManagerConfig` dataclass
  (dry_run, elevated, sysctl_conf_path).  Note: deviates from the
  original spec's `persist_dir` shape and instead carries the full
  `sysctl_conf_path` ("/etc/sysctl.conf"), matching the macOS pilot's
  decision to address the single flat persistence file directly.

- [x] 4. Define `MacosParameterValue` dataclass (name, value,
  is_default).  Note: spec called for a `source` field; `is_default`
  was kept as a tri-state placeholder to align with the Ubuntu/RHEL
  siblings.  Source-tagging is omitted because there is only one
  persistence file on macOS.

- [x] 5. Define `MacosKernelParameterActionResult` dataclass with
  contract-mandated fields including `requires_reboot`, `diff`,
  `source`, `message`

- [x] 6. Define `MacosKernelParameterQueryError` exception

## Allowlist and Parsing Helpers

- [ ] 7. Define `_ALLOWED_PREFIXES` constant and
  `_is_allowlisted(name)` helper.  **Deviation:** the macOS pilot
  intentionally does NOT enforce a prefix allowlist (unlike Ubuntu).
  macOS' sysctl namespace is not always anchored at one of the
  Linux-canonical prefixes, and the SIP / read-only enforcement is
  done by the kernel itself — re-implementing it in userspace would
  produce false negatives on legitimate keys.

- [ ] 8. Define `_SIP_READ_ONLY` set and `_is_sip_read_only(name)`
  helper — deferred; the kernel's runtime rejection surfaces as
  `FAILED` with the underlying stderr.

- [x] 9. Implement `_parse_sysctl_a(stdout)` supporting both
  `name: value` and `name = value` separator styles

- [ ] 10. Implement `_parse_sysctl_conf(content)` returning list of
  `(kind, line_or_pair)` preserving comments — deferred; the simpler
  `_set_sysctl_line` / `_remove_sysctl_line` line-rewrite helpers
  cover every current verb.

- [x] 11. Implement strict `name=value` rendering (no spaces) in
  `_set_sysctl_line`

- [ ] 12. Implement `_compute_file_diff(old_content, new_content,
  path)` for dry-run diff output — deferred follow-up.

## MacosKernelParameterManager — Implemented

- [x] 13. Implement `__init__` accepting runner and optional config
  (no `IFileOperator`; file I/O routes through the runner per the
  established macOS pattern)

- [x] 14. Implement `get_parameter(name)` — `sysctl -n`, return
  `MacosParameterValue` or `None` for unknown-oid, raise
  `MacosKernelParameterQueryError` for other failures

- [x] 15. Implement `list_parameters()` — `sysctl -a --ignore`,
  parse with `_parse_sysctl_a`, raise on non-zero exit

- [x] 16. Implement `set_parameter(name, value, persistent)` —
  idempotency check(s), runtime dispatch, optional persistent file
  rewrite + runtime apply

- [x] 17. Implement `reload_parameters()` — `sysctl -f
  /etc/sysctl.conf` (the macOS equivalent of `sysctl --system`).
  Note: never SKIPS in this implementation; absent-file detection is
  surfaced via the runner's stderr instead.

- [x] 18. Implement `remove_persisted_parameter(name)` — file read,
  rewrite with target line removed, idempotent when file or key
  absent

- [x] 19. Implement `_set_parameter_runtime_only` helper (dry-run
  short-circuit)

- [x] 20. Implement `_set_parameter_persistent` helper (dry-run
  short-circuit)

## MacosKernelParameterManager — Gaps

- [ ] 21. launchd plist-based persistence — NOT implemented; out of
  scope

- [ ] 22. NVRAM / EFI-variable tunables — NOT implemented; separate
  feature

- [ ] 23. Mass-apply from a policy document (YAML / TOML) — NOT
  implemented; out of scope

## Tests — Implemented

- [x] 24. `test_get_parameter_returns_value`
- [x] 25. `test_get_parameter_returns_none_on_unknown_oid`
- [x] 26. `test_get_parameter_raises_on_unexpected_failure`
- [x] 27. `test_list_parameters_parses_both_separators`
- [x] 28. `test_list_parameters_raises_on_non_zero_exit`
- [x] 29. `test_set_runtime_skipped_when_value_matches`
- [x] 30. `test_set_runtime_dispatches_sysctl_w`
- [x] 31. `test_set_persistent_rewrites_file_and_applies_runtime`
- [x] 32. `test_set_persistent_skipped_when_file_and_runtime_match`
- [ ] 33. `test_set_rejects_non_allowlisted_prefix` — N/A; allowlist
  policy was intentionally dropped (see task 7)
- [ ] 34. `test_set_rejects_sip_read_only_at_runtime` — deferred
  with the SIP set
- [x] 35. `test_set_persistent_partial_failure_reports_step`
- [x] 36. `test_reload_dispatches_sysctl_f` (renamed from
  `test_reload_dispatches_sysctl_p` to match the macOS flag)
- [ ] 37. `test_reload_skipped_when_sysctl_conf_absent` — deferred;
  current behaviour surfaces FAILED with the underlying stderr
- [x] 38. `test_remove_persisted_skipped_when_file_absent`
- [x] 39. `test_remove_persisted_preserves_other_lines`

## Tests — Gaps

- [ ] 40. Test dry-run populates diff for persistent writes —
  deferred until `_compute_file_diff` lands

- [ ] 41. Test dry-run still executes read-only idempotency guards —
  partly covered by `test_dry_run_short_circuits_set_parameter`;
  full matrix still pending

- [x] 42. Test elevation is set for writes when config enables it

- [x] 43. Test elevation is NEVER set for read-only operations
  (`test_reads_never_elevate_even_when_config_elevated`)

- [x] 44. Test new-file creation path emits header comment
  (`test_set_persistent_creates_file_when_absent`)

- [x] 45. Test strict `name=value` rendering (no spaces) — covered
  by the `tee` payload assertion in
  `test_set_persistent_rewrites_file_and_applies_runtime`

- [ ] 46. Property-based tests (hypothesis) covering Properties
  1 through 28 — deferred follow-up

## Follow-ups

- [ ] 47. **Cross-OS contract conformance review**: verify the data
  classes match the contract exactly, including `requires_reboot`
  semantics for any reboot-required keys.

- [ ] 48. **SIP-read-only set maintenance**: document a process for
  keeping `_SIP_READ_ONLY` aligned with macOS version changes (the
  set varies across releases).
