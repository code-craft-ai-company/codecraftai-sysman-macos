# Implementation Plan

- [x] 1. Wire the module skeleton
  - Create `automojo/sysman/macos/mailtransportmanagement.py` with
    the module docstring, the verb / tool mapping table, and the
    imports of `ActionStatus`, `BaseFeatureManager`,
    `FeatureActionResult`, `FeatureConfig` from `featurebase`.
  - Define module-level constants: `_CAT`, `_TEE`, `_POSTCONF`,
    `_POSTQUEUE`, `_NEWALIASES`, `_MAILQ`, `_SENDMAIL`,
    `_LAUNCHCTL`, `_DEFAULT_ALIASES_FILE`,
    `_DEFAULT_SERVICE_LABEL`, `_DEFAULT_SERVICE_DOMAIN`,
    `_KNOWN_MAIN_KEYS`, `_INT_KEYS`.
  - Alias `MacosMtaActionStatus = ActionStatus`.
  - Document the daemon-disabled-by-default caveat prominently.
  - _Requirements: 12_

- [x] 2. Define data models
  - `MacosMtaManagerConfig(FeatureConfig)` with `aliases_file`,
    `service_label`, `service_domain`, `reload_after_set` defaults.
  - `MacosMtaActionResult(FeatureActionResult)` with `setting` and
    `local`.
  - `MacosMtaConfig` with the named directive fields plus
    `extras`.
  - `MacosMtaAliasInfo` with `local` and `remote`.
  - `MacosMtaQueueStatus` with `pending_count`, `deferred_count`,
    `total_size_bytes`, `queue_age_oldest`.
  - `MacosMtaQueryError(Exception)`.
  - _Requirements: 1, 2, 3, 4, 5, 7_

- [x] 3. Implement read verbs
  - `get_mta_config()` — `postconf -n`, parse, raise on failure.
  - `list_aliases()` — `cat /etc/aliases` + parse.
  - `get_queue_status()` — `mailq` + parse.
  - All three MUST pass `elevated=False`.
  - _Requirements: 1, 5, 7_

- [x] 4. Implement `set_mta_config`
  - Honour dry-run.
  - Probe live config via `get_mta_config()`.
  - Per-setting compare and dispatch `postconf -e`.
  - Trail `launchctl kickstart -k <domain>/<label>` after at least
    one successful change AND `config.reload_after_set=True`.
  - Aggregate result: SKIPPED / SUCCESS / FAILED.
  - Audit trail in `stdout` (`SKIPPED|CHANGED|FAILED|KICKSTART`).
  - _Requirements: 2, 9, 10, 11, 12_

- [x] 5. Implement alias write verbs
  - `add_alias(local, remote)` — read, parse, skip when matched,
    upsert, write via `tee`, chain `newaliases`.
  - `remove_alias(local)` — read, skip when missing/absent,
    rewrite, write via `tee`, chain `newaliases`.
  - Use `_invoke_newaliases` to surface `FAILED` from the chained
    rebuild.
  - Honour dry-run; honour `elevated`.
  - _Requirements: 3, 4, 9, 10, 11_

- [x] 6. Implement operational write verbs
  - `test_mail_delivery(to, from_addr, subject)` — build payload
    via `_build_probe_message`, dispatch `sendmail -t` with stdin.
  - `flush_queue()` — dispatch `postqueue -f`.
  - Both verbs MUST NOT skip; both honour dry-run.
  - _Requirements: 6, 8, 9, 10, 11_

- [x] 7. Implement parsing helpers
  - `_parse_postconf_n_output`, `_apply_directive`,
    `_config_to_directive_pairs`, `_DirectivePairs`.
  - `_parse_aliases_file`, `_upsert_alias`, `_remove_alias`.
  - `_parse_mailq_output`.
  - `_build_probe_message`.
  - _Requirements: 1, 5, 7_

- [x] 8. Author requirements doc
  - Twelve requirements covering reads, writes, idempotency,
    dry-run, elevation, failure reporting, and the macOS
    daemon-disabled-by-default caveat.
  - _Requirements: -_

- [x] 9. Author design doc
  - Architecture diagram, configuration, data models, MTA
    mapping with cross-OS concept-mapping table, key method
    designs, idempotency model, dry-run / elevation policy,
    correctness properties, testing strategy, seams.
  - _Requirements: -_

- [x] 10. Author test module (>= 14 tests)
  - `test_get_mta_config_parses_postconf_output`.
  - `test_get_mta_config_raises_on_failure_with_stderr`.
  - `test_set_mta_config_invokes_postconf`.
  - `test_set_mta_config_skipped_per_setting_when_match`.
  - `test_set_mta_config_dry_run_short_circuits`.
  - `test_add_alias_invokes_tee_and_newaliases`.
  - `test_add_alias_skipped_when_present`.
  - `test_add_alias_dry_run_short_circuits`.
  - `test_remove_alias_invokes_tee_and_newaliases`.
  - `test_remove_alias_skipped_when_absent`.
  - `test_list_aliases_parses_etc_aliases`.
  - `test_list_aliases_returns_empty_when_file_missing`.
  - `test_test_mail_delivery_invokes_sendmail`.
  - `test_test_mail_delivery_dry_run_short_circuits`.
  - `test_get_queue_status_parses_mailq`.
  - `test_get_queue_status_empty_queue`.
  - `test_flush_queue_invokes_postqueue`.
  - `test_writes_elevate`.
  - `test_reads_never_elevate`.
  - `test_failed_command_surfaces_stderr`.
  - `test_add_alias_failed_when_newaliases_fails`.
  - `test_set_mta_config_failed_when_kickstart_fails` (consumer
    macOS scenario).
  - Module-level parser sanity tests for postconf / aliases /
    mailq.
  - _Requirements: 1-12_

- [ ] 11. Follow-ups (not part of this wave)
  - Pre-flight probe via `launchctl print system/org.postfix.master`
    so the manager can produce a clear "daemon not loaded" detail
    instead of surfacing the kickstart failure.
  - Auto-disable `reload_after_set` when the daemon is detected
    as not-loaded.
  - Switch to `launchctl kill -SIGHUP system/org.postfix.master`
    once we confirm Postfix master honours SIGHUP for re-read on
    macOS the same way it does on Linux.
  - Add `set_mta_config` "directives to remove" surface via
    `postconf -X`.
  - Surface `mailq -j` JSON output for richer queue
    introspection.
  - Cross-feature integration: emit a `MaintenanceReport` row per
    write verb once the macOS package adopts the report bus.
