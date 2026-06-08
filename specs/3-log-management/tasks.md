# Implementation Plan — macOS Log Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.logmanagement` module

- [x] 2. Define `MacosLogActionStatus` enum (SUCCESS / FAILED / SKIPPED
  / DRY_RUN), aliased to the shared `ActionStatus` from
  `ccai.sysman.macos.featurebase`

- [x] 3. Define `MacosLogManagerConfig` dataclass (`dry_run`,
  `elevated`, `newsyslog_d`, `default_window`).
  *Pilot deviation:* the spec earlier mentioned `MacosLogDiskUsage`
  on the manager surface; the pilot does not implement
  `query_disk_usage` and therefore does not declare a
  `MacosLogDiskUsage` dataclass — see Follow-up #25.

- [x] 4. Define `MacosLogEntry` / `MacosLogEntries` / `MacosLogInfo` /
  `MacosLogActionResult` dataclasses

- [x] 5. Define `MacosRotationPolicy` and `MacosLogQueryError`
  exception.
  *Pilot deviation:* `MacosLogDiskUsage` is intentionally omitted (see
  task 3 / Follow-up #25).

## MacosLogManager — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional `config`.
  *Pilot deviation:* no `file_operator` parameter; the pilot follows
  the macOS pattern of routing all file I/O through the runner via
  `cat` / `tee` / `rm` / `test -e` / `ls -1`.  The contract's
  `IFileOperator` collaborator is documented as a forward-looking
  design note.

- [x] 7. Implement `_build_subsystem_predicate` helper

- [x] 8. Implement `_parse_log_ndjson` and `_parse_one_log_line`
  using the unified-log JSON shape (`timestamp`, `processImagePath`,
  `subsystem`, `eventMessage`)

- [ ] 9. Implement `_subsystem_to_launch_label` helper for
  `launchctl kill SIGHUP` rotation of unified subsystems.
  *Pilot status:* deferred — the unified log has no per-subsystem
  rotate primitive that would benefit from a SIGHUP, and the pilot
  routes rotation exclusively through `newsyslog`.  Revisit if the
  contract requires per-subsystem rotation semantics on macOS.

- [x] 10. Implement `get_log(name, lines, since)` with subsystem
  predicate construction, NDJSON parsing, and tolerance to
  `--last <window>` vs `--start <iso>`

- [x] 11. Implement `tail_log(name, follow)` as a generator over
  `log stream --predicate ... --style ndjson`

- [x] 12. Implement `list_logs()` composing `log config --list` and
  `ls -1 <newsyslog_d>`

- [x] 13. Implement `rotate_log(name)` with newsyslog drop-in vs
  global-config dispatch

- [x] 14. Implement `truncate_log(name)` with file-path dispatch via
  `truncate -s 0 <path>` and FAILED return for unified subsystems

- [x] 15. Implement `configure_rotation(name, policy)` with
  idempotent render-and-write via `tee`

- [ ] 16. Implement `remove_rotation_config(name)` with
  absent / present guard.
  *Pilot status:* deferred — the contract names this as a write verb,
  but the pilot prioritises the `configure_rotation` path.  See
  Follow-up #26.

- [ ] 17. Implement `get_rotation_config(name)` parsing the newsyslog
  stanza back into a `MacosRotationPolicy`.
  *Pilot status:* deferred together with task 16.  The renderer
  (`_render_newsyslog`) is byte-stable, but the inverse parser is not
  written — see Follow-up #26.

- [ ] 18. Implement `query_disk_usage(name)` for unified subsystems
  and plain-file logs.
  *Pilot status:* deferred — macOS does not expose a per-subsystem
  size primitive (`log stats` is undocumented), and the
  `query_disk_usage` contract verb requires both unified and file
  paths.  See Follow-up #25.

- [x] 19. Implement `_render_newsyslog` producing a newsyslog stanza
  with frequency-to-when mapping (`daily` -> `$D0`, `weekly` ->
  `$W0D0`, `monthly` -> `$M1D0`)

- [ ] 20. Implement `_parse_rotation_policy` mapping a stanza back
  to `MacosRotationPolicy`.
  *Pilot status:* the helper `_extract_log_path_from_newsyslog` does
  the minimum needed by `truncate_log`.  Full inverse parsing is
  deferred together with task 17.

## MacosLogManager — Gaps (specified but not implemented)

- [ ] 21. Predicate richness: the pilot fixes the predicate to
  `subsystem == "<name>"`.  Process / category / `eventMessage`
  matching is not parameterised into the API.

- [ ] 22. `log erase` for unified-log truncation is intentionally
  out of scope (requires Recovery mode); documented in module
  docstring.

- [ ] 23. Unified-log `signpost` and `activity` events are not
  surfaced in `MacosLogEntry`.

## Tests — Implemented

- [x] 24. `test_get_log_constructs_log_show_command`
- [x] 25. `test_get_log_includes_since_when_provided`
- [x] 26. `test_get_log_parses_ndjson_payload`
- [x] 27. `test_get_log_raises_query_error_on_failure`
- [x] 28. `test_get_log_trims_to_lines_cap`
- [x] 29. `test_tail_log_uses_log_stream_and_yields_entries`
- [x] 30. `test_list_logs_combines_unified_and_newsyslog_sources`
- [x] 31. `test_rotate_log_uses_drop_in_when_present`
- [x] 32. `test_rotate_log_falls_back_to_global_newsyslog`
- [x] 33. `test_rotate_log_dry_run_does_not_invoke_newsyslog`
- [x] 34. `test_truncate_log_returns_failed_without_drop_in`
- [x] 35. `test_truncate_log_dispatches_truncate_for_file_path`
- [x] 36. `test_truncate_log_skipped_when_file_already_empty`
- [x] 37. `test_configure_rotation_writes_via_tee`
- [x] 38. `test_configure_rotation_skipped_when_content_matches`
- [x] 39. `test_configure_rotation_dry_run_short_circuits`
- [x] 40. `test_writes_elevate_when_configured_reads_do_not`

## Tests — Gaps

- [ ] 41. Parser robustness on malformed `log show --style ndjson`
  output (mixed valid + invalid lines): the implementation tolerates
  this (raw collector); a dedicated test is deferred.

- [ ] 42. Dry-run matrix test covering every write verb asserting
  zero mutating calls (currently covered piecewise on
  `rotate_log` and `configure_rotation`).

- [ ] 43. Test for `tail_log(follow=False)` semantics — the pilot
  argv is the same regardless of `follow`, but a dedicated assertion
  on a finite payload would harden the contract.

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/log-management/contract.md`.
  Notable deviations: `query_disk_usage` is unimplemented;
  `MacosLogDiskUsage` is therefore absent.

- [ ] 25. **Disk-usage feature**: implement `query_disk_usage` once
  a stable macOS primitive is identified.  Candidates:
  `log stats` (undocumented), `du` against
  `/var/db/diagnostics/Persist`, or a per-subsystem heuristic.

- [ ] 26. **Inverse parsing**: implement
  `remove_rotation_config` / `get_rotation_config` /
  `_parse_rotation_policy` so callers can introspect a previously
  written drop-in.

- [ ] 27. **Tail bounding**: production callers need a way to
  bound `tail_log` (line count, deadline, or sentinel).  Today the
  iterator yields until the runner's stdout drains.  Revisit once
  `ICommandRunner` exposes a streaming surface.
