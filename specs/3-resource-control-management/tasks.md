# Tasks

This document tracks the implementation work for
`MacosResourceControlManager`.  Tasks marked `[x]` are landed
on the wave-5.3 branch.

---

## Implementation tasks

- [x] Stub the `automojo/sysman/macos/resourcecontrolmanagement.py`
  module with the public surface defined in `design.md`.
- [x] Implement the `MacosResourceControlActionStatus`,
  `MacosResourceControlActionResult`,
  `MacosResourceControlManagerConfig`,
  `MacosIoLimitSpec`,
  `MacosResourceGroupSpec`,
  `MacosResourceGroupInfo`,
  `MacosResourceGroupUsage`, and
  `MacosResourceControlQueryError` types.
- [x] Implement the `_validate_name` and `_validate_pid` helpers.
- [x] Implement `_render_plist_body` carrying `Label`,
  `HardResourceLimits.RLIMIT_AS`, `Disabled`, `ProgramArguments`,
  and `AttachedPIDs`.
- [x] Implement `_parse_plist_memory_bytes`,
  `_parse_plist_attached_pids`, and `_parse_top_output`
  parsers.
- [x] Implement `_read_file` and `_path_exists` probes.
- [x] Implement `create_group` with byte-equal idempotency
  guard and `launchctl bootstrap`.
- [x] Implement `delete_group` with `launchctl bootout` + `rm`.
- [x] Implement `set_cpu_limit` returning typed FAILED with
  `detail="macos_no_per_group_cpu_limit"`.
- [x] Implement `set_memory_limit` re-rendering the plist and
  re-bootstrapping.
- [x] Implement `set_io_limits` returning typed FAILED with
  `detail="macos_no_per_group_io_limit"`.
- [x] Implement `attach_process` invoking
  `taskpolicy -p <pid> -c utility` and updating the plist's
  `AttachedPIDs`.
- [x] Implement `detach_process` invoking
  `taskpolicy -p <pid> -c default` and updating the plist.
- [x] Implement `list_groups` enumerating
  `<plist_dir>/<label_prefix>*.plist` entries.
- [x] Implement `get_group_usage` aggregating `top` snapshots
  per attached PID.

---

## Test tasks

- [x] Land
  `source/testroots/testbase/automojo/tests/sysman/macos/test_resourcecontrolmanagement.py`
  with at least 14 pytest functions.
- [x] `test_create_group_invokes_launchctl` — `tee` + `launchctl
  bootstrap`.
- [x] `test_create_group_skipped_when_present` — byte-equal plist
  body -> SKIPPED.
- [x] `test_create_group_dry_run` — DRY_RUN and no runner call.
- [x] `test_delete_group_invokes_launchctl` — `bootout` + `rm`.
- [x] `test_delete_group_skipped_when_absent` — missing plist ->
  SKIPPED.
- [x] `test_set_cpu_limit_returns_failed_with_macos_detail` —
  typed FAILED with documented detail token.
- [x] `test_set_memory_limit_invokes_tee` — re-render + tee +
  bootout + bootstrap.
- [x] `test_set_memory_limit_skipped_when_match` — RLIMIT_AS
  already matches.
- [x] `test_set_io_limits_returns_failed_with_macos_detail` —
  typed FAILED with documented detail token.
- [x] `test_attach_process_invokes_taskpolicy` — `taskpolicy -p
  <pid> -c utility`.
- [x] `test_attach_process_skipped_when_present` — PID already
  in plist.
- [x] `test_detach_process_invokes_taskpolicy` — `taskpolicy -p
  <pid> -c default`.
- [x] `test_list_groups_returns_list` — enumerate plists.
- [x] `test_get_group_usage_parses_top_output` — `top` snapshots
  aggregated.
- [x] `test_writes_elevate` — `elevated=True` reaches the runner
  for writes when configured.
- [x] `test_reads_never_elevate` — reads always run with
  `elevated=False`.
- [x] `test_failed_command_surfaces_stderr` — non-zero exit
  surfaces stderr / exit_code on FAILED.

---

## Documentation tasks

- [x] `requirements.md` — 13 requirements covering the cross-OS
  contract verbs plus the macOS gap surface.
- [x] `design.md` — cross-OS resource-control-model
  concept-mapping table + verb implementation pseudocode +
  prominent macOS limitation notes.
- [x] `tasks.md` — this file.

---

## Follow-up tasks

These are intentionally deferred and tracked outside Wave-5.3.

- [ ] Translate IO contract onto macOS's `iopriority` /
  `setiopolicy_np` family — out of pilot scope.
- [ ] Per-process CPU quota emulation via spawn-time wrapper
  scripts.
- [ ] App-layer rlimit enforcement beyond `RLIMIT_AS`
  (RLIMIT_CPU, RLIMIT_NOFILE, etc.).
- [ ] Streaming launchd domain state via `launchctl print`
  watching.
- [ ] Apple Silicon QoS-class refinements (`utility` is one of
  many valid classes; cross-OS contract speaks at a single
  ceiling level).
