# Implementation Plan -- Kdump Management (macOS)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `kdumpmanagement` module under
  `source/packages/ccai/sysman/macos/`.

- [x] 2. Alias `MacosKdumpActionStatus = ActionStatus`.

- [x] 3. Define `MacosKdumpTargetKind` enum with the single value
  `LOCAL_PATH` (cross-OS contract conformance does not require the
  Linux-specific kinds on macOS).

- [x] 4. Define `MacosKdumpManagerConfig` dataclass with `dry_run`,
  `elevated`, and `report_dir` (default
  `/Library/Logs/DiagnosticReports`).

- [x] 5. Define `MacosKdumpActionResult` dataclass extending
  `FeatureActionResult` with `target`, `requires_reboot=False`, and
  `message`.

- [x] 6. Define `MacosKdumpTargetSpec` dataclass holding
  `target_kind`, `path`, and the reserved cross-OS fields.

- [x] 7. Define `MacosKdumpStatus` dataclass holding `enabled`,
  `running`, `target_kind`, `target_path`, `kernel`,
  `reserved_memory_mb`, and `message`.

- [x] 8. Define `MacosKdumpInfo` dataclass holding `id`, `path`,
  `created`, `size_bytes`, `kernel`, and `panic_message`.

- [x] 9. Define `MacosKdumpQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 10. Implement `_parse_panic_metadata(text)` that surfaces the
  first `Darwin Kernel Version` and `panic(` lines.

- [x] 11. Define the shared `_UNSUPPORTED_MESSAGE` constant.

## MacosKdumpManager Verbs

- [x] 12. Implement `enable_kdump()` -- returns `FAILED` synthetic.

- [x] 13. Implement `disable_kdump()` -- returns `FAILED` synthetic.

- [x] 14. Implement `configure_kdump_target(spec)` -- returns
  `FAILED` synthetic, echoing `spec.path` onto `target`.

- [x] 15. Implement `clear_old_dumps(age_days)`: argument validation
  -> `find` invocation honouring `config.elevated` /
  `config.dry_run`.

- [x] 16. Implement `get_kdump_status()` returning the synthetic
  always-on snapshot without invoking the runner.

- [x] 17. Implement `list_recent_dumps()` via `ls -1t` filtered to
  `*.panic` and `*.ips`.

- [x] 18. Implement `get_dump_info(dump_id)` via `cat` + BSD
  `stat -f %Sm;%z`.

## MacosKdumpManager Internal Helpers

- [x] 19. Implement `_unsupported_result(action, target)` building
  the macOS-advisory `FAILED` result.

- [x] 20. Implement `_build_result(...)` constructing a
  `MacosKdumpActionResult` without dispatch.

## Tests

- [x] 21. `test_enable_kdump_returns_failed_with_macos_message`.

- [x] 22. `test_disable_kdump_returns_failed_with_macos_message`.

- [x] 23. `test_configure_kdump_target_returns_failed_with_macos_message`.

- [x] 24. `test_get_kdump_status_returns_synthetic_status`.

- [x] 25. `test_list_recent_dumps_parses_directory_listing` -- verifies
  the extension filter.

- [x] 26. `test_list_recent_dumps_empty_when_directory_missing`.

- [x] 27. `test_clear_old_dumps_invokes_find_with_mtime`.

- [x] 28. `test_clear_old_dumps_dry_run_short_circuits`.

- [x] 29. `test_get_dump_info_parses_panic_output`.

- [x] 30. `test_writes_elevate`.

- [x] 31. `test_reads_never_elevate`.

- [x] 32. `test_failed_command_surfaces_stderr`.

- [x] 33. `test_get_dump_info_raises_on_missing_file`.

- [x] 34. `test_clear_old_dumps_rejects_negative_age`.

## Follow-ups

- [ ] 35. Wire a `sysdiagnose` trigger verb if maintenance workflows
  later require system-state captures from Python.  Currently out of
  scope: `sysdiagnose` is a TouchID-gated interactive command that
  cannot be reliably automated through a command-runner.

- [ ] 36. Surface the per-user `~/Library/Logs/DiagnosticReports/`
  directory in addition to the system-wide one.  Today the manager
  only enumerates the system-wide directory; per-user enumeration
  requires an injected user identity that is not yet wired in this
  package.

- [ ] 37. Add an explicit `_ips_metadata_parser` for IPS-format
  diagnostic reports (the `*.ips` JSON-line format is different from
  `*.panic`).  Today the parser is best-effort across both formats.
