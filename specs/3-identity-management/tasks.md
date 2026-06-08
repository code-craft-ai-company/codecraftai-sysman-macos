# Implementation Plan — macOS Identity Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work tracked for follow-up batches.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.identitymanagement` module.

- [x] 2. Define `MacosIdentityActionStatus` (SUCCESS / FAILED /
  SKIPPED / DRY_RUN).
  *Aliased to the shared `featurebase.ActionStatus` to honour the
  contract's prefixed-naming requirement without forking the enum.*

- [x] 3. Define `MacosIdentityManagerConfig` dataclass (dry_run,
  elevated, dsconfigad_executable, dscl_executable,
  dscacheutil_executable, killall_executable, default_ou, force).

- [x] 4. Define `MacosIdentityActionResult` dataclass with `realm`,
  `username`, and `message` fields layered on top of
  `FeatureActionResult`.

- [x] 5. Define `MacosRealmKind` enum with `ACTIVE_DIRECTORY`,
  `OPEN_DIRECTORY`, `UNKNOWN`.

- [x] 6. Define `MacosRealmJoinSpec` dataclass with `realm`,
  `admin_user`, `admin_password`, `computer_name`, `ou_path`,
  `domain_controller`.

- [x] 7. Define `MacosRealmInfo`, `MacosRealmStatus`,
  `MacosUserResolveResult`, `MacosGroupResolveResult` dataclasses.

- [x] 8. Define `MacosIdentityQueryError` exception.

- [ ] 9. Define `MacosIdentityCredentialError` exception.
  *Deferred — credentials are required fields on
  `MacosRealmJoinSpec` and on the `leave_realm` argument list, so
  the missing-credential case is structurally impossible.*

- [ ] 10. Define `MacosIdentityRealmNotFoundError` exception.
  *Deferred — `leave_realm` and `resolve_*` use SKIPPED /
  found=False semantics for missing realms / principals.*

## MacosIdentityManager — Implemented

- [x] 11. Implement `__init__` accepting runner + optional config.
  *Inherited from `BaseFeatureManager.__init__`.*

- [x] 12. Implement `join_realm(spec)` — `dsconfigad -add` with
  optional `-computer` / `-ou` / `-preferred` / `-force` flags.
  Password lands on argv on the runner side; the recorded argv on
  the result has the value redacted to `<redacted>`.  Idempotent
  via `dsconfigad -show`.

- [x] 13. Implement `leave_realm(admin_user, admin_password,
  realm=None)` — `dsconfigad -remove -username <admin> -password
  <pw> [-force]`.  Skips when not bound or bound to a different
  realm.

- [x] 14. Implement `get_realm_status()` — `dsconfigad -show`
  parsed.  Reports `MacosRealmStatus(joined=False)` when both
  `Active Directory Forest` and `Active Directory Domain` are
  empty / dashed.

- [x] 15. Implement `list_joined_realms()` — single-realm reality,
  returns a list of zero or one based on `get_realm_status`.

- [x] 16. Implement `sync_users()` — `dscacheutil -flushcache`
  followed by `killall -HUP mDNSResponder`.

- [x] 17. Implement `resolve_user(username)` — `dscl /Search -read
  /Users/<u>` parsed.  Detects realm via `OriginalNodeName`
  containing `/Active Directory/<domain>/`.

- [x] 18. Implement `resolve_group(groupname)` — `dscl /Search
  -read /Groups/<g>` parsed.  Returns `found=False` on `dscl`
  non-zero.

- [x] 19. Implement `_sanitise_argv_passwords` — walks argv and
  replaces the value following any password-bearing flag with
  `<redacted>`.

- [x] 20. Implement `_parse_dsconfigad_show` — parses `Key = Value`
  lines into a flat dict.

- [x] 21. Implement `_parse_dscl_read_output` — parses
  `Attribute: value` lines with continuation-line folding.

- [x] 22. Implement `_detect_realm_from_node` — extracts the AD
  domain from an `OriginalNodeName` path.

## Tests — Implemented

- [x] 23. `_MockCommandRunner` recording every `run` call.

- [x] 24. `test_join_realm_invokes_dsconfigad` — argv shape +
  `elevated=True` + recorded `<redacted>`.

- [x] 25. `test_join_realm_skipped_when_joined`.

- [x] 26. `test_join_realm_dry_run`.

- [x] 27. `test_leave_realm_invokes_dsconfigad`.

- [x] 28. `test_leave_realm_skipped_when_not_joined`.

- [x] 29. `test_get_realm_status_parses_dsconfigad_output`.

- [x] 30. `test_get_realm_status_returns_unjoined_when_unbound`.

- [x] 31. `test_list_joined_realms_returns_list`.

- [x] 32. `test_sync_users_invokes_dscacheutil_and_killall`.

- [x] 33. `test_resolve_user_parses_dscl_output` — AD-backed
  record with realm extraction.

- [x] 34. `test_resolve_user_local_no_realm` — local record with
  `realm=None`.

- [x] 35. `test_resolve_user_returns_not_found_when_unknown`.

- [x] 36. `test_resolve_group_parses_dscl_output`.

- [x] 37. `test_password_redacted_in_recorded_command` — argv
  recorded on the result MUST replace the password value with
  `<redacted>`.

- [x] 38. `test_password_redacted_for_leave_realm`.

- [x] 39. `test_writes_elevate`.

- [x] 40. `test_reads_never_elevate`.

- [x] 41. `test_failed_command_surfaces_stderr`.

- [x] 42. `test_get_realm_status_raises_on_query_failure` —
  `MacosIdentityQueryError` on `dsconfigad -show` non-zero exit.

## Tracked Follow-Ups

- [ ] 43. `discover_realm(domain)` — `dsconfigad -showadvanced`
  parsed plus `nslookup _ldap._tcp.<domain>` for SRV records.

- [ ] 44. Open Directory enumeration in `list_joined_realms` via
  `dscl /Search -read / SearchPolicy`.

- [ ] 45. `extra_options` on `MacosRealmJoinSpec` for
  `dsconfigad`-specific extras (`-mobile`, `-mobileconfirm`,
  `-localhome`, `-useuncpath`, etc.).

- [ ] 46. `IdentityCredentialError` integration once the
  cross-OS credential abstraction lands.

- [ ] 47. Service-side restart of `opendirectoryd` after a sync.
  Owned by `3-service-management`.

- [ ] 48. Per-user Kerberos ticket lifecycle (`kinit` is available
  on macOS but is out of contract scope).

- [ ] 49. `MacosIdentityToolNotAvailableError` for hosts that
  lack `dsconfigad` (rare, but possible on minimal installs).

- [ ] 50. Cache the `dsconfigad -show` output across the
  `get_realm_status` / `_is_realm_joined` calls inside a single
  `join_realm` invocation to avoid two calls.
