# Implementation Plan — macOS User-Group Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  This batch lands the initial
``MacosUserGroupManager`` along with its data models, parsers, and a
unit-test suite that runs entirely against an in-process command-runner
mock.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.usergroupmanagement` module

- [x] 2. Define `MacosUserGroupActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN) — re-exports `featurebase.ActionStatus`.

- [x] 3. Define `MacosUserGroupManagerConfig` dataclass (dry_run,
  elevated, regular_uid_min, regular_uid_max, include_system_accounts,
  admin_user, admin_password)

- [x] 4. Define `MacosUserGroupActionResult` dataclass with sanitised
  `command` field (every `<password>` value is replaced with
  `"<redacted>"` before the argv lands on the result)

- [x] 5. Define `MacosUserSpec`, `MacosUserChanges`, `MacosUserInfo`,
  `MacosGroupSpec`, `MacosGroupInfo` dataclasses.  Note that the
  spec-named `MacosUserModSpec` ships under the more idiomatic name
  ``MacosUserChanges`` to match the cross-OS contract; revisit in the
  follow-ups below if the spec wins the rename.

- [x] 6. Define `MacosUserGroupQueryError` exception.  `MacosUserNotFoundError`
  and `MacosGroupNotFoundError` are NOT introduced as separate types;
  the manager surfaces the same condition by returning ``None`` from
  ``get_user`` / ``get_group`` and a ``FAILED`` result with
  ``message="user does not exist"`` / ``"group does not exist"`` from
  write verbs.  See deviations below.

## MacosUserGroupManager — Implemented

- [x] 7. Implement `__init__` accepting runner + config (inherited from
  `BaseFeatureManager`)

- [x] 8. Implement `create_user(spec)` — `sysadminctl -addUser` with
  admin credential pair from config, idempotent via `dscl . -read
  /Users/<name>`

- [x] 9. Implement `delete_user(name)` — `sysadminctl -deleteUser
  <name> -secure`, idempotent via `dscl . -read`.  ``-secure`` is
  always emitted (it removes the home directory); the optional
  ``remove_home`` toggle from the spec is parked under the follow-ups
  below.

- [x] 10. Implement `modify_user(name, changes)` — one
  `dscl . -change /Users/<name> <Property> <old> <new>` per scalar
  field plus `dseditgroup` calls for membership changes; returns
  ``FAILED`` when the user does not exist; first-failure short-circuit
  with ``detail`` populated and the failing argv recorded on
  ``command``.  ``move_home=True`` (ditto + rm) is parked under the
  follow-ups.

- [x] 11. Implement `get_user(name)` — `dscl . -read /Users/<name>` +
  `id -Gn` + `pwpolicy -u <name> -authentication-allowed`, parses
  `RecordName`, `UniqueID`, `PrimaryGroupID`, `RealName`,
  `NFSHomeDirectory`, `UserShell`.

- [x] 12. Implement `list_users()` — `dscl . -list /Users UniqueID`
  filtered by `regular_uid_min`/`regular_uid_max`, materialised via
  `get_user`.

- [x] 13. Implement `set_password(name, password)` — `sysadminctl
  -resetPasswordFor` with sanitised `command` replacing both
  ``-newPassword`` and ``-adminPassword`` values with `"<redacted>"`.

- [x] 14. Implement `lock_account` / `unlock_account` — `pwpolicy -u
  <name> -disableuser|-enableuser`, idempotent via current `locked`
  derived from ``pwpolicy -authentication-allowed``.

- [x] 15. Implement `create_group(spec)` — `dseditgroup -o create -i
  <gid> -r <description> <name>`, idempotent via `dscl -read
  /Groups/<name>`.

- [x] 16. Implement `delete_group(name)` — `dseditgroup -o delete`,
  idempotent via `dscl -read /Groups/<name>`.

- [x] 17. Implement `get_group(name)` — `dscl . -read /Groups/<name>`
  parser.  UUID-to-name resolution for the cross-system / mobile
  account case is parked under the follow-ups; the parser surfaces
  whatever ``GroupMembership`` reports.

- [x] 18. Implement `list_groups()` — `dscl . -list /Groups
  PrimaryGroupID` materialised via `get_group`.

- [x] 19. Implement `add_to_group` / `remove_from_group` —
  `dseditgroup -o edit -a|-d -t user`; returns ``FAILED`` when the
  group does not exist; idempotent via ``dseditgroup -o checkmember``.

- [x] 20. Implement `_dispatch` / `_build_*_result` helpers — dry-run
  short-circuit, argv sanitisation for password-bearing calls,
  runner-result mapping, elevation decision (writes elevate, reads
  never).

## MacosUserGroupManager — Gaps (specified but not implemented)

- [ ] 21. Admin vs Standard user role assignment (requires
  manipulation of the `admin` group membership via `dseditgroup`).
  Today callers can issue ``add_to_group(user, "admin")`` directly,
  but a higher-level role abstraction is not provided.

- [ ] 22. FileVault enrolment / unenrolment (`fdesetup add -usertoadd`).

- [ ] 23. Secure Token management (`sysadminctl -secureTokenStatus`).

- [ ] 24. Mobile / network account conversion.

- [ ] 24a. ``move_home=True`` semantics (ditto + rm) on
  ``modify_user``.

- [ ] 24b. UUID-to-username resolution for cross-system /
  network-account ``GroupMembership`` rows (``dscl . -search /Users
  GeneratedUID <uuid>``).

## Tests — Implemented

- [x] 25. `test_create_user_constructs_sysadminctl_argv_with_admin_pair`
- [x] 26. `test_create_user_skipped_when_present`
- [x] 27. `test_delete_user_emits_secure_flag`
- [x] 28. `test_delete_user_skipped_when_absent`
- [x] 29. `test_modify_user_emits_one_dscl_change_per_field`
- [x] 30. `test_modify_user_skipped_when_noop`
- [x] 31. `test_modify_user_returns_failed_when_user_missing`
- [x] 32. `test_modify_user_stops_on_first_failure`
- [x] 33. `test_set_password_argv_sent_to_runner_but_command_sanitised`
- [x] 34. `test_lock_account_skipped_when_already_locked`
- [x] 35. `test_unlock_account_skipped_when_already_unlocked`
- [x] 36. `test_lock_account_dispatches_pwpolicy_disableuser`
- [x] 37. `test_create_group_constructs_dseditgroup_argv`
- [x] 38. `test_delete_group_skipped_when_absent`
- [x] 39. `test_add_to_group_skipped_when_already_member`
- [x] 40. `test_add_to_group_dispatches_dseditgroup_edit`
- [x] 41. `test_remove_from_group_skipped_when_not_member`
- [x] 42. `test_add_to_group_failed_when_group_missing`
- [x] 43. `test_get_user_parses_dscl_id_and_pwpolicy`
- [x] 44. `test_get_user_returns_none_when_record_missing`
- [x] 45. `test_list_users_filters_by_uid_range`
- [x] 46. `test_list_users_raises_query_error_on_dscl_failure`
- [x] 47. `test_get_group_parses_dscl_read_output`
- [x] 48. `test_dry_run_short_circuits_create_user`
- [x] 49. `test_writes_pass_elevated_when_configured`
- [x] 50. `test_id_call_never_elevates`
- [x] 50a. `test_parse_dscl_read_output_handles_continuation_lines`
- [x] 50b. `test_parse_dscl_list_uid_line_handles_padded_columns`
- [x] 50c. `test_parse_primary_group_id_extracts_gid`
- [x] 50d. `test_materialise_user_attributes_defaults_on_missing_uid`
- [x] 50e. `test_user_info_dataclass_round_trip`
- [x] 50f. `test_group_info_dataclass_round_trip`

## Tests — Gaps

- [ ] 51. Persistence-aspect end-to-end test asserting the sanitised
  argv is what gets stored in the audit DB

- [ ] 52. Retry-aspect test proving `FAILED` -> `SUCCESS` promotion is
  transparent when `sysadminctl` intermittently fails

- [ ] 52a. ``test_modify_user_move_home_dispatches_ditto_and_rm`` — see
  follow-up 24a

- [ ] 52b. ``test_get_group_resolves_uuid_members_to_usernames`` — see
  follow-up 24b

## Follow-ups

- [ ] 53. **Cross-OS contract conformance review**: verify this
  implementation's `MacosUserGroupActionResult` field shapes match the
  cross-OS contract at
  `automojo-sysman-common/specs/contracts/user-group-management/contract.md`.

- [ ] 54. **automojo-* reuse check**: confirm that no other feature in
  `automojo-sysman-macos` invokes `sysadminctl` / `dscl` /
  `dseditgroup` directly; all such calls should go through
  `MacosUserGroupManager`.

- [ ] 55. **Spec rename review**: the spec calls the change-bag
  ``MacosUserModSpec`` while the implementation ships
  ``MacosUserChanges`` (parity with the Linux peers).  Pick one and
  align the spec + implementation.

- [ ] 56. **Admin pair documentation**: the spec calls out that
  ``sysadminctl -addUser`` and ``sysadminctl -resetPasswordFor`` need
  an admin pair when invoked non-interactively.  This is now enforced
  at the config level via ``admin_user`` + ``admin_password``;
  document the expected operator workflow (Keychain pull vs literal
  config), and consider an aspect that injects the pair from a
  secrets store rather than putting it in plain config.
