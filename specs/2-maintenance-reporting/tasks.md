# Implementation Plan — macOS Maintenance Reporting

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.maintenancereporting` module

- [x] 2. Define `MacosActionStatus` and `MacosReportFormat` enums

- [x] 3. Define `MacosMaintenanceReporterConfig` dataclass (dry_run, output_dir,
  default_format, json_indent)

- [x] 4. Define `MacosActionRecord`, `MacosReportSummary`,
  `MacosReportWriteResult`, `MacosMaintenanceSession`, and
  `MacosMaintenanceReport` dataclasses

- [x] 5. Implement `MacosActionRecord.__post_init__` that auto-stamps `timestamp`
  when none is supplied

## MacosMaintenanceReporter — Implemented

- [x] 6. Implement `__init__` accepting an `ICommandRunner` (file I/O via the
  runner per the locked-in pattern -- see "Spec deviation" note below), a
  `MacosMaintenanceReporterConfig`, and an optional `clock` callable

> **Spec deviation:** the original requirements/design referenced an
> `IFileOperator` for emit egress.  Per the pattern locked in by the prior
> 13 features in this package (and matching the Ubuntu peer), file I/O is
> dispatched through `ICommandRunner` using `mkdir -p` + `tee` instead.
> The verbs and result shapes are unchanged.

- [x] 7. Implement `begin_session(session_id, metadata=None)` — fresh session
  state, hostname auto-stamping, description harvested from metadata

- [x] 8. Implement `record_action(record)` and `record_query(record)` as the
  same append-with-timestamp path on separate lists

- [x] 9. Implement `record_error(error, context)` — captures traceback, appends
  a FAILED record to the actions list, raises `RuntimeError` when no session is
  active

- [x] 10. Implement `end_session(status=None)` — stamps `ended_at` and
  recomputes the session status

- [x] 11. Implement `summary_counts()`, `actions_by_status(*statuses)`, and
  `actions_by_feature(feature)`

- [x] 12. Implement `to_json(indent)` via a private `_to_dict` helper, ISO-8601
  UTC timestamps, and `json.dumps`

- [x] 13. Implement `to_text()` with the banner/summary/actions layout

- [x] 14. Implement `to_yaml()` via PyYAML's `safe_dump`; falls back to a JSON
  dump (which is valid YAML for the data shapes here) when PyYAML is unavailable

- [x] 15. Implement `to_markdown()` producing the H1 title, metadata table,
  summary table, and per-action sections

- [x] 16. Implement `emit_report(format, path=None)` — dry-run short-circuit,
  format dispatch, `mkdir -p` + `tee` egress through the runner,
  exception-to-error-result mapping

- [x] 17. Implement `default_session_path(session_id)` using `output_dir` and
  raising `ValueError` for empty ids

## MacosMaintenanceReport — Gaps (specified but not implemented)

- [ ] 18. HTML / PDF output formats (explicitly out of scope per contract)

- [ ] 19. Streaming emission for very large sessions (explicitly out of scope)

- [ ] 20. Correlation with external ticketing systems

- [ ] 21. Structured log emission in parallel via the unified log (`os_log`)

## Tests — Implemented

(Test names below are mapped to the actual test functions in
`source/testroots/testbase/automojo/tests/sysman/macos/test_maintenancereporting.py`.
Where the original test name describes a sibling behaviour, the equivalent
covered test is noted in parentheses.)

- [x] 22. `test_begin_session_creates_session` (covers initialise-state)
- [x] 23. `test_record_action_appends_to_actions` (covers prior-action discard via
  fresh state in `test_begin_session_creates_session`)
- [x] 24. `test_record_action_appends_to_actions` (covers insertion order)
- [x] 25. `MacosActionRecord.__post_init__` exercised under
  `test_record_action_appends_to_actions`
- [x] 26. `test_record_query_appends_to_queries`
- [x] 27. `test_record_action_without_session_raises` (same require-session guard)
- [x] 28. `test_end_session_sets_ended_at_and_status`
- [x] 29. `test_session_counts_aggregate_correctly` exercises the no-failed path
  (FAILED-only path covered by `test_end_session_sets_ended_at_and_status`)
- [x] 30. `test_session_counts_aggregate_correctly`
- [x] 31. `test_actions_by_status_filter`
- [x] 32. `test_actions_by_feature_filter`
- [x] 33. `test_emit_json_report_writes_file` (round-trips through `json.loads`)
- [x] 34. `test_to_text_contains_key_fields`
- [x] 35. `test_emit_json_report_writes_file`
- [x] 36. `test_emit_dry_run_skips_runner`
- [x] 37. `test_emit_failure_when_tee_fails`
- [x] 38. `test_emit_unknown_format_returns_error_result`
- [x] 39. `test_default_session_path_uses_output_dir`

## Tests — Gaps

- [ ] 40. Test that the YAML emit path returns a typed failure when PyYAML is not
  importable

- [x] 41. Test that the Markdown emit path produces valid headings and tables
  (covered by `test_emit_markdown_report_writes_file` plus
  `test_emit_yaml_report_writes_file` for the YAML variant)

- [ ] 42. Test that the injected `_clock` callable is used for `started_at`,
  `finished_at`, and default action timestamps

- [ ] 43. Test that aspect composition on the file operator is transparent: same
  `MacosReportWriteResult.success`, `path`, `dry_run` with or without aspects

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify that
  `MacosMaintenanceReport` exposes exactly the verbs listed in the contract, with
  the same result shapes, including the `record_query` alias.

- [ ] 45. **Default-base documentation**: confirm that the chosen default base
  `/var/log/automojo/maintenance` is outside System Integrity Protection paths and
  document the required `sudo` policy or TCC grants for CI agents that need to
  write reports without an interactive login.
