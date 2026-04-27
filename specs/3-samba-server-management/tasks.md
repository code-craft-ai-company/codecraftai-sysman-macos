# Implementation Plan — macOS Samba Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/macos/sambaservermanagement.py`
- [ ] 2. Define module-level constants (`_SHARING`, `_LAUNCHCTL`,
  `_DSCL`, `_PDBEDIT`, default service label, delegated-user
  message).
- [ ] 3. Define `MacosSambaActionStatus` alias of `ActionStatus`.
- [ ] 4. Define `MacosSambaManagerConfig` dataclass extending
  `FeatureConfig` with `service_label`, `delegate_user_mgmt`.
- [ ] 5. Define `MacosSambaActionResult` dataclass extending
  `FeatureActionResult` with `share_name`, `username`, `message`.
- [ ] 6. Define `MacosSambaShareSpec`, `MacosSambaShareInfo`,
  `MacosSambaShareAcl`, `MacosSambaUserInfo` dataclasses.
- [ ] 7. Define `MacosSambaQueryError`.

## Helpers

- [ ] 8. Implement `_parse_sharing_list_output(text)` that splits
  the multi-line per-share output of ``sharing -l`` into
  ``list[MacosSambaShareInfo]``.
- [ ] 9. Implement `_parse_pdbedit_output(text)` that parses
  ``pdbedit -L -v`` blocks into ``list[MacosSambaUserInfo]``.
- [ ] 10. Implement ``_render_dscl_acl_changes(name, acl)`` that
  builds the per-key ``dscl . -change /SharePoints/<name> ...``
  argv list.

## Manager — lifecycle

- [ ] 11. Implement `MacosSambaServerManager` class extending
  `BaseFeatureManager`.
- [ ] 12. Implement `__init__(runner, config=None)`.

## Manager — write verbs

- [ ] 13. Implement `add_share(spec)` per design.md.
- [ ] 14. Implement `remove_share(name)` per design.md.
- [ ] 15. Implement `set_share_permissions(name, acl)` per design.md.
- [ ] 16. Implement `reload_config()` per design.md.
- [ ] 17. Implement `add_samba_user(name, password)` returning
  delegated FAILED.
- [ ] 18. Implement `remove_samba_user(name)` returning delegated
  FAILED.

## Manager — read verbs

- [ ] 19. Implement `list_shares()` per design.md.
- [ ] 20. Implement `get_share(name)` per design.md.
- [ ] 21. Implement `list_samba_users()` per design.md.

## Tests

- [ ] 22. Create `test_sambaservermanagement.py`.
- [ ] 23. Add `test_add_share_invokes_sharing`.
- [ ] 24. Add `test_add_share_skipped_when_present`.
- [ ] 25. Add `test_add_share_dry_run`.
- [ ] 26. Add `test_remove_share_invokes_sharing`.
- [ ] 27. Add `test_list_shares_parses_output`.
- [ ] 28. Add `test_get_share_returns_correct_record`.
- [ ] 29. Add `test_set_share_permissions_invokes_dscl`.
- [ ] 30. Add `test_reload_config_invokes_launchctl`.
- [ ] 31. Add `test_add_samba_user_returns_failed_with_delegation_message`.
- [ ] 32. Add `test_remove_samba_user_returns_failed_with_delegation_message`.
- [ ] 33. Add `test_list_samba_users_parses_pdbedit_output`.
- [ ] 34. Add `test_writes_elevate`.
- [ ] 35. Add `test_reads_never_elevate`.
- [ ] 36. Add `test_failed_command_surfaces_stderr`.
- [ ] 37. Add `test_user_mgmt_delegates_with_explanation`.

## Follow-ups

- [ ] 38. **Local-user wiring.** Allow callers to set
  ``delegate_user_mgmt=False`` and dispatch ``sysadminctl
  -addUser`` / ``-deleteUser`` when explicitly opted-in.
- [ ] 39. **`pdbedit` fallback to `dscl`.** When ``pdbedit`` is
  absent on PATH, enumerate SMB-eligible users from the local
  directory.
- [ ] 40. **Drop `sharing` for ``net usershare``.** When
  ``samba`` is installed via Homebrew, mirror the Ubuntu code path
  for direct ``smb.conf`` management.
