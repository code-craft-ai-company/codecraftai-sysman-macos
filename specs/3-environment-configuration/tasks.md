# Implementation Plan — macOS Environment Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.environmentconfiguration`
  module

- [x] 2. Define `MacosEnvActionStatus` (aliased from
  `featurebase.ActionStatus`) and `MacosEnvScope` enums

- [x] 3. Define `MacosEnvironmentConfigurationManagerConfig`
  dataclass (`dry_run`, `elevated`, `system_zshenv`,
  `user_zshenv_basename`).  Note: renamed from the spec's
  `MacosEnvironmentConfiguratorConfig` for cross-package consistency
  (every other manager uses the `<Manager>Config` form).

- [x] 4. Define `MacosEnvValue` and `MacosEnvActionResult`
  dataclasses

- [x] 5. Define `MacosEnvQueryError` exception (renamed from the
  spec's `MacosEnvConfigError` for cross-package consistency with
  `<Feature>QueryError`).

## MacosEnvironmentConfigurationManager — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional
  `config` (no `IFileOperator`; file I/O routes through the runner)

- [ ] 7. Implement `_parse_launchd_conf` parser for
  `/etc/launchd.conf`.  **Deviation:** Apple deprecated
  `/etc/launchd.conf` in macOS 10.10 (Yosemite); this manager
  intentionally does NOT touch that file.  Persistence is performed
  via the sentinel block in `/etc/zshenv` instead, which is the
  modern equivalent path and matches the Ubuntu sibling's drop-in
  shape.

- [ ] 8. Implement `_parse_launchctl_export` parser for
  `launchctl export` stdout.  **Deviation:** modern macOS
  `launchctl` does not expose a whole-domain enumeration verb; the
  `list_system_env` implementation surfaces the persistent surface
  (sentinel block) only.

- [x] 9. Implement `_parse_block_exports` parser for `.zshenv`
  sentinel-block content (`_parse_block_exports`,
  `_set_block_export`, `_remove_block_export`,
  `_find_block`).  Sentinel pair:
  `# --- ccai system env start ---` /
  `# --- ccai system env end ---`.

- [x] 10. Implement `_resolve_user(username)` helper returning
  `(uid, home)` with `MacosEnvQueryError` on unknown.  Uses
  `id -u` for the uid and `dscl . -read /Users/<user>
  NFSHomeDirectory` for the home directory because macOS' user
  database is layered atop OpenDirectory and `getent` is unreliable.

- [ ] 11. Implement `_is_current_user(username)` helper —
  deferred follow-up; the user-scope writes therefore always pass
  `elevated=False` (per the Ubuntu sibling's documented limitation),
  not "suppress elevation only when same user".

- [ ] 12. Implement `_apply_change(lines, name, value_or_None)` line
  rewrite helper — superseded by the per-verb sentinel-block
  rewriters (`_set_block_export`, `_remove_block_export`).

- [x] 13. Implement `set_system_env(name, value)` with `launchctl
  setenv` (runtime) and `/etc/zshenv` sentinel-block write
  (persistence).  **Deviation:** the spec also mentioned an
  `/etc/paths.d/ccai` drop-in for `PATH`; that special-case is
  out of scope for the pilot — `PATH` is treated like any other
  variable.

- [x] 14. Implement `unset_system_env(name)` with `launchctl
  unsetenv` and `/etc/zshenv` sentinel-block rewrite.  When the
  block becomes empty AND the rest of the file is empty, the file
  is removed entirely via `rm -f`.

- [x] 15. Implement `get_system_env(name)` runtime-first read

- [x] 16. Implement `list_system_env()` — surfaces the sentinel
  block only (see deviation on task 8)

- [x] 17. Implement `set_user_env(user, name, value)` with
  unknown-user `FAILED`, `launchctl asuser <uid> launchctl setenv`,
  `~<user>/.zshenv` sentinel-block write, and `elevated=False` for
  every user-scope dispatch (see task 11)

- [x] 18. Implement `unset_user_env(user, name)` with unknown-user
  `FAILED`, `launchctl asuser <uid> launchctl unsetenv`, and
  `~<user>/.zshenv` sentinel-block rewrite

- [ ] 19. Implement `get_user_env(user, name)` runtime-first read
  raising `MacosEnvQueryError` on unknown user — deferred; the
  current pilot exposes user-scope writes/unsets only.

- [ ] 20. Implement `list_user_env(user)` merging runtime +
  `.zshenv` — deferred follow-up.

## MacosEnvironmentConfigurationManager — Gaps (specified but not implemented)

- [ ] 21. LaunchAgent plist authoring (per-user
  `~/Library/LaunchAgents/<label>.plist` with `EnvironmentVariables`
  dict) — intentionally out of scope

- [ ] 22. Bash / fish shell rc files — only `.zshenv` is written
  today

- [ ] 23. Variable interpolation (`${OTHER}`) — values are opaque

## Tests — Implemented

- [x] 24. `test_set_system_env_invokes_launchctl_and_writes_zshenv`
  (renamed from `_and_writes_conf` because the persistent surface
  is `/etc/zshenv`, not `/etc/launchd.conf`)
- [ ] 25. `test_set_system_env_writes_paths_d_for_PATH` — N/A;
  `/etc/paths.d/` special case dropped (see task 13)
- [x] 26. `test_set_system_env_skipped_when_both_sources_match`
- [ ] 27. `test_set_system_env_creates_missing_launchd_conf` —
  superseded; `test_set_system_env_invokes_launchctl_and_writes_zshenv`
  exercises the missing-file path against `/etc/zshenv`
- [x] 28. `test_set_system_env_dry_run_no_side_effects`
- [x] 29. `test_unset_system_env_idempotency_when_absent`
- [x] 30. `test_unset_system_env_invokes_launchctl_and_rewrites_zshenv`
  (renamed)
- [x] 31. `test_unset_system_env_deletes_file_when_block_emptied`
  (covers the file-cleanup path; the original `_rewrites_launchd_conf`
  case is N/A)
- [ ] 32. `test_unset_system_env_deletes_paths_d_for_PATH` — N/A;
  see task 13
- [x] 33. `test_get_system_env_runtime_first`
- [x] 34. `test_get_system_env_falls_back_to_zshenv` (renamed from
  `_falls_back_to_launchd_conf`)
- [x] 35. `test_list_system_env_parses_block_only` (renamed; see
  task 8 deviation)
- [x] 36. `test_set_user_env_failed_for_unknown_user`
- [x] 37. `test_set_user_env_invokes_launchctl_asuser_and_writes_zshenv`
- [ ] 38. `test_set_user_env_creates_missing_zshenv` — covered by
  task 37 (the test seeds an absent file)
- [ ] 39. `test_set_user_env_skipped_when_value_matches_both_sources`
  — deferred follow-up
- [ ] 40. `test_set_user_env_suppresses_elevation_for_current_user`
  — N/A; user-scope writes ALWAYS pass `elevated=False` until
  same-user detection lands (see task 11)
- [x] 41. `test_unset_user_env_failed_for_unknown_user`
- [x] 42. `test_unset_user_env_idempotent_when_absent`
- [ ] 43. `test_get_user_env_raises_for_unknown_user` — deferred
  with task 19
- [ ] 44. `test_list_user_env_returns_empty_when_no_source` —
  deferred with task 20

## Tests — Gaps

- [x] 45. Dry-run matrix (covered for `set_system_env` via
  `test_set_system_env_dry_run_no_side_effects`); full matrix still
  pending

- [x] 46. Elevation matrix
  (`test_reads_never_elevate_even_when_config_elevated`,
  `test_set_user_env_invokes_launchctl_asuser_and_writes_zshenv`
  asserting `elevated=False` for user-scope writes)

- [ ] 47. Parser robustness on malformed `launchctl getenv` /
  `dscl` output — deferred follow-up

## Follow-ups

- [ ] 48. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
  and that the scope-mapping rows are consistent.

- [ ] 49. **codecraftai-sysman-macos reuse check**: confirm that
  features which need environment variables delegate to
  `MacosEnvironmentConfigurationManager` rather than invoking
  `launchctl` directly.

- [ ] 50. **Same-user elevation suppression**: implement
  `_is_current_user` (probably via `id -un` against the runner) and
  switch user-scope writes from "always `elevated=False`" to
  "elevate when target != current".
