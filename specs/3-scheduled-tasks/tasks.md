# Implementation Plan — macOS Scheduled Tasks

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  This batch lands the contract's
core verbs with a deliberately limited launchd schedule grammar
(`"daily HH:MM"`, `"every <N>{sec,min,hour}"`); broader cron grammar is
flagged in "Gaps".

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.scheduledtasks` module

- [x] 2. Define `MacosScheduledTaskActionStatus` and `MacosTaskOwner`
  enums

- [x] 3. Define `MacosScheduledTasksManagerConfig` dataclass
  (dry_run, elevated, system_plist_dir, user_plist_dir_template,
  target_user, target_uid)

- [x] 4. Define `MacosTaskSpec` and `MacosTaskInfo` dataclasses

- [x] 5. Define `MacosScheduledTaskActionResult` dataclass and
  `MacosScheduledTaskQueryError` exception

## Rendering and Parsing Helpers

- [x] 6. Implement `_render_plist_body(spec, schedule_dict)` returning
  the XML body with `Label`, `ProgramArguments`,
  `StartCalendarInterval` or `StartInterval`, `WorkingDirectory`,
  `EnvironmentVariables`, `UserName`, and optional `RunAtLoad` keys

- [x] 7. Implement `_translate_schedule(schedule)` translating
  `"daily HH:MM"` and `"every <N>{sec,min,hour}"` into the correct
  plist key

- [x] 8. Implement `_plist_dir(owner)` returning the absolute path
  in `/Library/LaunchDaemons/` or
  `/Users/<user>/Library/LaunchAgents/`

- [x] 9. Implement `_domain_for(owner)` returning the launchctl
  domain string (`system` or `gui/<uid>`)

- [x] 10. Implement `_parse_print_output(stdout)` extracting `state`,
  `last exit code`, and other launchd properties into a dict

- [x] 11. Implement `_parse_print_disabled(stdout)` returning a set
  of disabled label strings

- [x] 12. Implement `_extract_schedule_from_plist(text)` round-tripping
  rendered plist bodies back into the canonical schedule string

- [x] 13. Implement `_extract_program_arguments_from_plist(text)`
  parsing the `ProgramArguments` array back into argv

## MacosScheduledTasksManager — Implemented

- [x] 14. Implement `__init__` accepting runner + optional config

- [x] 15. Implement `create_task(spec)` — schedule validation,
  byte-identical idempotency (plist body match + domain-enabled),
  ``mkdir -p`` for USER scope, plist write via ``tee``, and
  ``launchctl bootstrap``; FAILED with `message="bootstrap_failed"`
  on bootstrap failure (treats "already loaded" as success for
  idempotency)

- [x] 16. Implement `delete_task(name, owner)` — `launchctl bootout`
  (informational) + `rm -f`; SKIPPED when plist absent; FAILED
  with `message="plist_delete_failed"` when file delete fails

- [x] 17. Implement `enable_task(name, owner)` — dispatch
  `launchctl enable <domain>/<label>`; SKIPPED when not in
  `print-disabled` set

- [x] 18. Implement `disable_task(name, owner)` — dispatch
  `launchctl disable <domain>/<label>`; SKIPPED when already in
  `print-disabled` set

- [x] 19. Implement `run_task_now(name, owner)` — dispatch
  `launchctl kickstart <domain>/<label>` (no `-k`; we want to
  run, not restart)

- [x] 20. Implement `get_task(name, owner)` — read plist via
  ``cat`` + `launchctl print <domain>/<label>`; return `None` when
  both report not-found; raise
  `MacosScheduledTaskQueryError` on unexpected `print` failures

- [x] 21. Implement `list_tasks(owner)` — enumerate via
  `ls -1 <plist_dir>` and surface one
  :class:`MacosTaskInfo` per `.plist` entry; raise
  `MacosScheduledTaskQueryError` when ``ls`` fails for a reason
  other than the directory being absent

- [x] 22. Implement `_dispatch_write` helper — owner-based elevation
  decision, runner-result mapping, `command` capture

## MacosScheduledTasksManager — Gaps (specified but not implemented in this batch)

- [ ] 23. `validate_schedule(schedule)` public verb — currently the
  internal `_translate_schedule` raises `ValueError` for invalid
  input; surfacing it as a public bool method is deferred

- [ ] 24. KeepAlive / RunAtLoad semantics — `MacosTaskSpec.run_at_load`
  is plumbed through to the rendered plist, but a richer
  KeepAlive policy (matrix of conditions) is out of base contract

- [ ] 25. LoginWindow / per-session agents — out of base contract

- [ ] 26. Event-driven triggers (`WatchPaths`, `QueueDirectories`) —
  out of base contract

- [ ] 27. Task-chaining / dependencies between tasks — out of base
  contract

- [ ] 28. Cron-style schedule grammar (5-field, OnCalendar) — limited
  schedule grammar covers the common automation use cases; richer
  grammar is a future extension

## Tests — Implemented

- [x] 29. `test_translate_schedule_daily_form`
- [x] 30. `test_translate_schedule_every_min_form`
- [x] 31. `test_translate_schedule_rejects_unrecognised_input`
- [x] 32. `test_render_plist_body_emits_label_and_program_arguments`
- [x] 33. `test_render_plist_body_calendar_form_for_daily_schedule`
- [x] 34. `test_render_plist_body_interval_form_for_every_5min`
- [x] 35. `test_create_task_writes_plist_and_dispatches_bootstrap`
- [x] 36. `test_create_task_skipped_when_plist_matches_and_enabled`
- [x] 37. `test_create_task_failed_when_schedule_invalid`
- [x] 38. `test_create_task_dry_run_does_not_invoke_runner`
- [x] 39. `test_delete_task_dispatches_bootout_then_rm`
- [x] 40. `test_delete_task_skipped_when_plist_absent`
- [x] 41. `test_enable_task_dispatches_launchctl_enable`
- [x] 42. `test_enable_task_skipped_when_already_enabled`
- [x] 43. `test_disable_task_skipped_when_already_disabled`
- [x] 44. `test_run_task_now_dispatches_launchctl_kickstart_without_k_flag`
- [x] 45. `test_get_task_returns_populated_info_when_plist_and_print_succeed`
- [x] 46. `test_get_task_returns_none_when_plist_and_domain_unknown`
- [x] 47. `test_list_tasks_enumerates_plist_directory`
- [x] 48. `test_user_owner_uses_user_plist_dir_and_gui_domain`
- [x] 49. `test_user_owner_without_target_user_returns_failed`
- [x] 50. `test_system_owner_writes_pass_elevated_when_configured`
- [x] 51. `test_user_owner_writes_never_elevate`
- [x] 52. `test_reads_never_elevate`
- [x] 53. `test_parse_print_output_extracts_state_and_last_exit_code`
- [x] 54. `test_parse_print_disabled_extracts_disabled_labels`
- [x] 55. `test_extract_schedule_from_plist_recovers_daily_form`
- [x] 56. `test_extract_program_arguments_from_plist_returns_argv`
- [x] 57. `test_list_tasks_raises_on_unexpected_ls_failure`
- [x] 58. `test_invalid_task_name_returns_failed`

## Tests — Gaps

- [ ] 59. Per-verb invalid-name test for every name-bearing verb
  (currently only `delete_task` is exercised; the path is shared
  via `_ensure_valid_name`)

- [ ] 60. Property-based tests (hypothesis) covering Properties
  1 through 31

## Follow-ups

- [ ] 61. **Cross-OS contract conformance review**: confirm the
  `MacosTaskSpec` shape (`command: list[str]`, `schedule: str`,
  `run_at_load: bool`) matches the cross-OS contract.  Note the
  deviation from the contract's `(command: str, arguments:
  list[str])` shape — launchd's `ProgramArguments` is naturally a
  single argv vector, so we collapse to one field.

- [ ] 62. **Schedule grammar deviation**: the contract permits any
  schedule string the OS accepts; this implementation accepts only
  `"daily HH:MM"` and `"every <N>{sec,min,hour}"` and rejects
  everything else.  Document the limited grammar in the contract's
  macOS row and note that broader cron-style support is a future
  extension.

- [ ] 63. **KeepAlive extension plan**: scope whether per-task
  `KeepAlive` belongs in a subclassed spec or a dedicated
  `MacosPersistentTaskManager` companion.

- [ ] 64. **codecraftai-* reuse check**: confirm no other feature in
  `codecraftai-sysman-macos` authors plists or invokes `launchctl`
  directly; all such work should go through
  `MacosScheduledTasksManager`.
