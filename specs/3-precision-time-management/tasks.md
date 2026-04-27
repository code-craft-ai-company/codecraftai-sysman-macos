# Implementation Plan — macOS Precision-Time Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify follow-ups deferred to a later iteration.

## Module and Models

- [x] 1. Create the
  `automojo.sysman.macos.precisiontimemanagement` module.

- [x] 2. Define `MacosPrecisionTimeActionStatus` (alias of
  `ActionStatus`).

- [x] 3. Define `MacosPrecisionTimeManagerConfig` dataclass
  (inherits `dry_run` and `elevated` from `FeatureConfig`).

- [x] 4. Define `MacosPrecisionTimeActionResult` dataclass with
  the `server` carry-through field.

- [x] 5. Define `MacosTimeStatus`, `MacosTimeSourceInfo`,
  `MacosTimeDrift` dataclasses.

- [x] 6. Define `MacosPrecisionTimeQueryError` exception.

## MacosPrecisionTimeManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config.
  Spec deviation: no `IFileOperator` collaborator — file I/O for
  the legacy ntp.conf fallback flows through the runner via `cat`.

- [x] 8. Implement `get_time_status()` — dispatches
  `systemsetup -getusingnetworktime` and
  `systemsetup -getnetworktimeserver`.  Always populates
  `synchronized` with the same value as `enabled` because Apple's
  tooling does not expose a distinct sync-state reading.

- [x] 9. Implement `get_time_sources()` — three-tier fallback
  chain (`defaults read` → `systemsetup -getnetworktimeserver` →
  `cat /etc/ntp.conf`), each routed through the runner.

- [x] 10. Implement `get_drift()` — reads the current server,
  dispatches `sntp <server>`, parses the
  `±offset +/- error<unit>` payload.

- [x] 11. Implement `add_time_source(server)` — recompute list,
  rewrite via `systemsetup -setnetworktimeserver "<rendered>"`;
  idempotent via membership check.

- [x] 12. Implement `remove_time_source(server)` — recompute,
  rewrite; idempotent via membership check.

- [x] 13. Implement `set_time_servers(servers)` — render full list
  and dispatch; idempotent via sorted-list comparison.

- [x] 14. Implement `force_sync_now()` — read current server, then
  dispatch `sntp -sS <server>`; returns `FAILED` when no server is
  configured.

- [x] 15. Implement `enable_sync()` / `disable_sync()` — dispatch
  `systemsetup -setusingnetworktime on|off`; idempotent via the
  current `Network Time: On|Off` reading.

## Renderers and Parsers — Implemented

- [x] 16. `_parse_using_network_time` — parse the
  ``Network Time: On|Off`` payload.

- [x] 17. `_parse_network_time_server` — parse the
  ``Network Time Server: <server>`` payload.

- [x] 18. `_parse_defaults_time_service_config` — parse the
  ``TimeServers = ( ... );`` block out of a defaults read payload.

- [x] 19. `_parse_ntp_conf_servers` — parse `server`/`pool`
  declarations out of legacy `/etc/ntp.conf`.

- [x] 20. `_parse_sntp_offset` — parse the sntp
  ``±offset +/- error<unit>`` payload, normalising the error to
  seconds.

- [x] 21. `_scale_for_unit` — convert a unit suffix to a
  multiplicative seconds scale.

## Tests

- [x] 22. `test_get_time_status_parses_systemsetup_output`
- [x] 23. `test_get_time_status_returns_disabled_when_off`
- [x] 24. `test_get_time_sources_parses_systemsetup_output`
- [x] 25. `test_get_time_sources_parses_defaults_output`
- [x] 26. `test_get_drift_parses_sntp_output`
- [x] 27. `test_add_time_source_invokes_systemsetup`
- [x] 28. `test_add_time_source_skipped_when_present`
- [x] 29. `test_add_time_source_dry_run`
- [x] 30. `test_remove_time_source_invokes_systemsetup`
- [x] 31. `test_remove_time_source_skipped_when_absent`
- [x] 32. `test_set_time_servers_invokes_systemsetup`
- [x] 33. `test_set_time_servers_skipped_when_match`
- [x] 34. `test_force_sync_now_invokes_sntp`
- [x] 35. `test_enable_sync_skipped_when_already_enabled`
- [x] 36. `test_disable_sync_invokes_systemsetup`
- [x] 37. `test_writes_elevate`
- [x] 38. `test_reads_never_elevate`
- [x] 39. `test_failed_command_surfaces_stderr`
- [x] 40. `test_get_time_status_raises_on_systemsetup_failure`

## Follow-ups

- [ ] 41. Replace string `last_sync` with a parsed `datetime`
  once Apple exposes a last-sync timestamp through user-space
  tooling (currently neither `systemsetup` nor `sntp` reports
  one).
- [ ] 42. Surface a unified diff on dry-run results so callers
  can audit the would-have-been content change.
- [ ] 43. Add a `force_sync_now` retry-fallback when the configured
  server is not reachable — currently the verb propagates the
  underlying `sntp` failure.
- [ ] 44. Extend `get_time_sources` to consult the runtime `timed`
  daemon's IPC interface (where exposed) for richer per-source
  state — current implementation reports only the configured
  list.
