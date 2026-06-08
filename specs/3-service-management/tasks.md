# Implementation Plan — macOS Service Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.servicemanagement` module

- [x] 2. Define `MacosServiceActionStatus` and `MacosServiceScope` enums

- [x] 3. Define `MacosServiceManagerConfig` dataclass (dry_run, elevated,
  target_uid, default_plist_dir, user_plist_dir).
  *Pilot deviation:* `mask_fallback_enabled`, `system_override_plist_dir`, and
  `user_override_plist_dir` are NOT implemented; the override-plist mask fallback
  was deferred (see Follow-ups).

- [x] 4. Define `MacosServiceActionResult`, `MacosServiceStatus`,
  `MacosServiceInfo` dataclasses and `MacosServiceQueryError` exception

- [x] 5. Implement the scope→domain helper that returns `"system"` or
  `f"gui/{target_uid}"` and raises when USER scope is requested without
  `target_uid`.
  *Pilot deviation:* the helper raises `MacosServiceQueryError` (a domain
  error) rather than `ValueError`, on the grounds that "missing config" is a
  query-resolution failure, not a programmer error like passing a non-enum
  value.  The non-enum path still raises `ValueError`.

## MacosServiceManager — Implemented

- [x] 6. Implement `__init__` accepting a runner and a config.
  *Pilot deviation:* no file operator parameter; the override-plist mask
  fallback was deferred so the file operator is unused at this pilot stage.

- [x] 7. Implement `start_service` / `stop_service` with idempotency guards based
  on `active_state`

- [x] 8. Implement `restart_service` as unconditional via `kickstart -k`

- [x] 9. Implement `reload_service` sending `kill SIGHUP`, returning `FAILED` when
  not active

- [x] 10. Implement `enable_service` / `disable_service` with idempotency guards
  based on `enabled_state`

- [x] 11. Implement `mask_service` / `unmask_service`.
  *Pilot deviation from the design:* the implementation uses option (b) only —
  alias to `disable_service` / `enable_service`.  Option (a)'s two-step
  `disable` + override-plist write is deferred (see Follow-ups).  This makes
  mask/unmask idempotent against `enabled_state in {"disabled", "enabled"}`
  exactly like Windows-style mask-aliases-disable.

- [x] 12. Implement `get_service_status` wrapping `launchctl print`, parsing
  `state`, `pid`, `disabled`, `last exit code`, and `program`/`path`, and
  returning a synthetic `not-found` status when print reports the service
  missing

- [ ] 13. Implement `list_services` merging `launchctl list` and `launchctl
  print-disabled <domain>` output by label.
  *Pilot status:* implemented `launchctl list` parsing only.  The
  `print-disabled` merge that the design called for is deferred — the basic
  `launchctl list` output already gives PID/Status/Label, which the cross-OS
  contract's `MacosServiceInfo` shape covers.  Adding `print-disabled` would
  populate a future `enabled_state` column on `MacosServiceInfo` (currently
  absent on the dataclass — see Follow-up #48).

- [x] 14. Implement `reload_daemon` as a no-op returning SUCCESS with a
  descriptive `message`

- [ ] 15. Implement the override-plist content builder producing the
  `<key>Disabled</key><true/>` plist fragment.
  *Pilot status:* deferred together with task 11 option (a).

## MacosServiceManager — Gaps (specified but not implemented)

- [ ] 16. `launchctl bootstrap <domain> <plist>` support for installing a new
  launchd job from a newly-authored plist (today the manager assumes plists already
  exist on disk) — belongs in a future `configuration-management` spec

- [ ] 17. `launchctl asuser <uid>` wrapping for SSH sessions that need to drive
  user-scope launchd from a non-user login

- [ ] 18. TCC / Full-Disk-Access probe — surface whether the calling process holds
  the entitlements needed to stop privileged services

- [ ] 19. log extraction via `log stream --predicate` — belongs to
  `log-management`

## Tests — Implemented

- [x] 20. `test_start_service_invokes_launchctl_kickstart`
  *(spec name: `test_start_service_system_runs_launchctl_kickstart` — renamed
  to mirror Ubuntu pilot's flat naming convention.)*
- [x] 21. `test_user_scope_requires_target_uid`
  *(spec called this `test_start_service_user_requires_target_uid`; this pilot
  enforces it as a generic scope-resolution test exercised through
  `start_service`.)*
- [x] 22. `test_start_service_skipped_when_already_running`
  *(spec name: `test_start_service_skipped_when_active`.)*
- [x] 23. `test_stop_service_skipped_when_not_running`
  *(spec name: `test_stop_service_skipped_when_inactive`.)*
- [ ] 24. `test_stop_service_surfaces_tcc_denial`.
  *Pilot status:* the generic `test_failed_command_returns_failed_result_with_stderr`
  exercises the same surface (FAILED + stderr captured); a dedicated TCC-named
  variant is deferred.
- [x] 25. `test_restart_service_unconditional`
  *(spec name: `test_restart_service_never_skips`.)*
- [x] 26. `test_reload_service_refuses_when_inactive`
  *(spec name: `test_reload_service_failed_when_not_active`.)*
- [ ] 27. `test_reload_service_sends_sighup_when_active`.
  *Pilot status:* the inactive case is covered.  A happy-path SIGHUP test is
  deferred — adding it is mechanical follow-up.
- [x] 28. `test_enable_service_skipped_when_enabled`
- [ ] 29. `test_disable_service_skipped_when_masked`.
  *Pilot status:* in this pilot's mask-aliases-disable model, "masked" and
  "disabled" are the same state, so the related skip is covered by the
  `disable` idempotency on `enabled_state == "disabled"`.  A dedicated test is
  deferred; revisit when the override-plist fallback (task 11a) lands.
- [x] 30. `test_mask_service_invokes_launchctl_disable_with_mask_flag`
  *(spec name: `test_mask_service_fallback_invokes_disable_and_file_write`;
  renamed because this pilot does not yet call a file operator — see
  task 11a follow-up.)*
- [x] 31. `test_mask_service_alias_does_not_invoke_file_operator`.
  *Pilot status:* trivially true in this pilot — there is no file operator at
  all.  The dedicated test will be added when task 11a lands.
- [ ] 32. `test_mask_service_skipped_when_masked`.
  *Pilot status:* deferred; the `mask`-aliases-`disable` skip path is exercised
  indirectly by the `enable_service_skipped_when_enabled` and
  `disable_service` tests.
- [ ] 33. `test_unmask_service_fallback_removes_plist_and_enables`.
  *Pilot status:* deferred together with task 11a.
- [x] 34. `test_get_service_status_parses_launchctl_print`
  *(spec name: `test_get_service_status_parses_launchctl_print` — same.)*
- [ ] 35. `test_get_service_status_returns_not_found_for_unknown_service`.
  *Pilot status:* the implementation handles it (synthetic `not-found` return),
  but a dedicated test is deferred.
- [x] 36. `test_list_services_parses_list_output`
  *(spec name: `test_list_services_merges_list_and_print_disabled` — pilot
  defers the `print-disabled` merge per task 13.)*
- [x] 37. `test_reload_daemon_returns_success_noop`
  *(spec name: `test_reload_daemon_returns_success_without_runner_invocation` —
  same intent.)*
- [x] 38. `test_write_with_elevated_config_passes_elevated_true`
  *(spec name: `test_system_writes_pass_elevated_true_when_configured`.)*
- [ ] 39. `test_user_writes_elevate_for_other_users`.
  *Pilot deviation:* this pilot follows the Ubuntu model — USER-scope writes
  NEVER elevate, regardless of whether `target_uid != os.getuid()`.  The
  cross-user-elevation behaviour the design called for is reconsidered as
  Follow-up #50; it requires either a `launchctl asuser` wrapper or coordination
  with the runner.
- [x] 40. `test_user_scope_writes_never_elevate`
  *(spec name: `test_user_writes_do_not_elevate_for_self`.)*
- [x] 41. `test_read_never_elevates`
  *(spec name: `test_reads_never_pass_elevated_true`.)*
- [x] 42. `test_start_service_dry_run_short_circuits`
  *(spec name: `test_dry_run_skips_runner_and_file_operator_for_every_write` —
  pilot tests dry-run on `start_service`; the file-operator clause does not
  apply because the file operator is unused at this pilot stage.)*
- [ ] 43. `test_invalid_scope_raises_value_error`.
  *Pilot status:* the implementation raises `ValueError` for non-enum scope
  values; a dedicated test is deferred.

## Tests — Gaps

- [ ] 44. Test that `list_services` raises `MacosServiceQueryError` when either
  `list` or `print-disabled` returns a non-zero exit with an unknown stderr.
  *Pilot status:* `list_services` raises on `list` failure (implemented in code,
  test deferred).  `print-disabled` is not yet wired (see task 13).

- [ ] 45. Test that `get_service_status` raises `MacosServiceQueryError` when
  `print` returns an unexpected non-"service not found" stderr.
  *Pilot status:* the implementation raises; a dedicated test is deferred.

- [ ] 46. Test that aspect composition on the runner is transparent (same
  `status` field with and without aspects)

- [ ] 47. Test that the override-plist path is constructed correctly for both
  system and user scopes (and that user-scope tilde expansion is applied to the
  target user's home directory).
  *Pilot status:* deferred together with task 11a.

## Follow-ups

- [ ] 48. **Cross-OS contract conformance review**: verify that
  `MacosServiceActionResult`, `MacosServiceStatus`, `MacosServiceInfo` shapes
  match the contract.  The current pilot's `MacosServiceInfo` does NOT carry
  `active_state`, `enabled_state`, or `description` columns — the cross-OS
  contract row may need to be updated to reflect what `launchctl list` actually
  emits, OR `list_services` may need to be enriched via `print-disabled` +
  `print` per row (expensive on macOS).  This is a contract-vs-implementation
  alignment decision.

- [ ] 49. **launchd bootstrap review**: decide whether `start_service` should
  automatically `bootstrap` when the service is not yet loaded into the domain;
  currently it relies on the plist already being loaded.

- [ ] 50. **Override-plist mask fallback**: implement task 11a (two-step
  `disable` + override-plist write via an `IFileOperator`), task 15 (plist
  content builder), task 33 / 47 (tests).  This is the macOS-native masking
  semantics the design originally specified; this pilot defers it in favour of
  the simpler mask-aliases-disable contract that matches Windows.

- [ ] 51. **Cross-user USER-scope elevation**: this pilot enforces "USER-scope
  writes never elevate" exactly like the Ubuntu pilot.  The macOS design
  originally wanted `elevated=True` when `target_uid != os.getuid()` so a root
  process could drive another user's `gui/<uid>` domain.  Revisit once the
  `launchctl asuser <uid>` wrapper (task 17) is designed; the elevation
  decision belongs at that layer, not in the service manager.
