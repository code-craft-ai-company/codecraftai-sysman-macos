# Implementation Plan — macOS Audit Management

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.auditmanagement`
  module.

- [x] 2. Define `MacosAuditActionStatus` enum (re-exported alias of
  `ccai.sysman.macos.featurebase.ActionStatus`).

- [x] 3. Define `MacosAuditManagerConfig` dataclass (`dry_run`,
  `elevated`, `audit_control`, `audit_user`, `audit_log_dir`,
  `auditd_plist`, `auditd_label`).

- [x] 4. Define `MacosAuditActionResult` dataclass (subclass of
  `FeatureActionResult`; adds `rule_id`, `rule_key`, `message`).

- [x] 5. Define `MacosAuditRuleSpec` dataclass (`name`,
  `event_class`, `condition`, `enabled`).

- [x] 6. Define `MacosAuditRuleInfo` dataclass (`rule_id`, `name`,
  `event_class`, `expression`, `subsystem`, `source`).

- [x] 7. Define `MacosAuditStatus` dataclass (`enabled`, `running`,
  `pid`, `failure_mode`, `rate_limit`, `backlog_limit`, `backlog`,
  `lost_events`, `locked`, `raw`).

- [x] 8. Define `MacosAuditEventQuery` and `MacosAuditEvent`
  dataclasses.

- [x] 9. Define `MacosAuditQueryError` and
  `MacosAuditImmutableError` exceptions.

## MacosAuditManager — Implemented

- [x] 10. Implement `__init__` (inherited from
  `BaseFeatureManager`) accepting `runner` and optional `config`.

- [x] 11. Implement `_add_class_to_flags` and
  `_remove_class_from_flags` so that `add_rule` / `remove_rule`
  can rewrite the `flags` line of `audit_control` deterministically.

- [x] 12. Implement `add_rule(spec)` with idempotency guard,
  dry-run short-circuit, `tee` write, and `audit -s` reload.

- [x] 13. Implement `remove_rule(rule_id)` with idempotency guard,
  dry-run short-circuit, `tee` write, and `audit -s` reload.

- [x] 14. Implement `list_rules` composing `audit_control` flags
  and `audit_user` rows.

- [x] 15. Implement `enable_auditing` issuing `audit -i` and
  `launchctl bootstrap system <plist>` with idempotency and
  immutable-mode guards.

- [x] 16. Implement `disable_auditing` issuing `launchctl bootout
  <label>` with idempotency and immutable-mode guards.

- [x] 17. Implement `get_audit_status` parsing `audit -s` into the
  structured dataclass.

- [x] 18. Implement `search_events(query)` building the `praudit`
  argv and filtering lines by substring fields.

- [x] 19. Implement `_parse_audit_control_flags`, `_parse_audit_user`,
  `_parse_audit_status`, `_parse_praudit_lines`.

- [x] 20. Implement `_safe_status` to keep write paths resilient
  to read-time failures.

- [x] 21. Implement `_read_file`, `_latest_log_path` helpers.

## MacosAuditManager — Gaps (specified but not implemented)

- [ ] 22. Standalone `reload_rules` verb (currently folded into
  `add_rule` / `remove_rule`).

- [ ] 23. Native time-bound filtering for `search_events` via
  `auditreduce -a`/`-d` rather than client-side substring filters.

- [ ] 24. `audit_user` write path (today the manager only reads
  per-user rules).

## Tests — Implemented

- [x] 25. `test_add_rule_invokes_audit`
- [x] 26. `test_add_rule_skipped_when_present`
- [x] 27. `test_add_rule_dry_run_short_circuits`
- [x] 28. `test_event_class_added_to_audit_control`
- [x] 29. `test_remove_rule_invokes_audit`
- [x] 30. `test_remove_rule_skipped_when_absent`
- [x] 31. `test_list_rules_parses_output`
- [x] 32. `test_enable_auditing_invokes_audit`
- [x] 33. `test_enable_auditing_skipped_when_enabled`
- [x] 34. `test_disable_auditing_invokes_launchctl`
- [x] 35. `test_get_audit_status_parses_audit_output`
- [x] 36. `test_get_audit_status_detects_locked_mode`
- [x] 37. `test_search_events_invokes_praudit_with_query`
- [x] 38. `test_search_events_filters_lines_by_key`
- [x] 39. `test_writes_elevate`
- [x] 40. `test_reads_never_elevate`
- [x] 41. `test_failed_command_surfaces_stderr`
- [x] 42. `test_add_class_to_flags_appends`
- [x] 43. `test_remove_class_from_flags_drops`
- [x] 44. `test_parse_audit_control_flags_handles_full_file`
- [x] 45. `test_parse_audit_status_handles_partial_output`
- [x] 46. `test_parse_praudit_lines_respects_limit`

## Tests — Gaps

- [ ] 47. Coverage for partial-failure scenarios where `tee`
  succeeds but `audit -s` fails (current implementation
  downgrades to FAILED but the test surface is incomplete).

- [ ] 48. Coverage for the `+`/`-` prefixed token forms that
  `audit_control(5)` accepts (e.g., `flags:lo,+fa` to add a
  class without replacing).

## Spec Deviations / Revisions Needed

- [ ] 49. **`IFileOperator` collaborator**: the contract names
  `IFileOperator` as the route for rule-file I/O.  This
  implementation routes every read / write through the
  `ICommandRunner` instead.

- [ ] 50. **Class-based addressing**: the contract's abstract
  `AuditRuleSpec.expression` field carries the rule body as a
  string.  On macOS the body is a single class token; downstream
  cross-OS callers must be aware of this divergence.

- [ ] 51. **`reload_rules` verb**: surface as a standalone public
  verb instead of folding it into `add_rule` / `remove_rule`.

- [ ] 52. **`AuditImmutableError` raising**: this implementation
  returns `FAILED` with a descriptive message rather than raising
  the exception.

## Follow-ups

- [ ] 53. **Cross-OS contract conformance review**: verify that
  the shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/audit-management/contract.md`.

- [ ] 54. **Coordinator wiring**: confirm that the maintenance
  coordinator routes through `MacosAuditManager` rather than
  constructing `audit` invocations directly.
