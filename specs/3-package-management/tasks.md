# Implementation Plan — macOS Package Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.packagemanagement` module

- [x] 2. Define `MacosPackageActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)
  - Aliased to the shared :class:`ActionStatus` via the
    ``.macos.featurebase`` base, per the pattern locked in by the
    service-management pilot.

- [x] 3. Define `MacosPackageManagerConfig` dataclass (dry_run,
  elevated, brew_user, cask_default)
  - Spec revision: the `assume_yes` field is not surfaced because
    `brew` is non-interactive by default; the `cask_default` flag
    added here lets callers flip the default channel between formula
    and cask without per-call overrides.

- [x] 4. Define `MacosPackageActionResult` and `MacosPackageInfo`
  dataclasses
  - Spec deviation: the batch does NOT implement
    `MacosPackageUpdateInfo` (deferred with `list_upgradable`).

- [x] 5. Define `MacosPackageQueryError` exception
  - Spec deviation: `MacosPackageFileNotFoundError` is NOT defined —
    `install_local` is out of scope for this batch.

## Parsing Helpers

- [x] 6. Implement `_parse_brew_list_versions_single(stdout)` returning
  the last whitespace-separated token of the first non-empty line
  (the active version when multiple kegs are present)

- [x] 7. Implement `_parse_brew_info_json(stdout, default_kind)`
  returning a `MacosPackageInfo` or `None`, honouring the missing-
  formula stderr sentinel in `get_package_info`

- [ ] 8. Implement `_parse_brew_outdated_json(stdout)` returning a
  list of `MacosPackageUpdateInfo` from `formulae[]`
  - Deferred with `list_upgradable`.  `_is_outdated` handles the
    single-package probe that `upgrade_package` uses.

- [x] 9. Implement `_wrap_with_brew_user(argv)` to prefix
  `sudo -u <brew_user> --` when `config.brew_user` is set
  - Spec revision: the prefix does NOT include `-H` in this batch.
    Callers needing the home-directory reset can extend the helper.

## MacosPackageManager — Implemented

- [x] 10. Implement `__init__` accepting runner + optional config
  - Inherited from :class:`BaseFeatureManager`; the one-time warning
    when `elevated=True` is NOT emitted in this batch because
    `_dispatch_brew` simply ignores `config.elevated` entirely.

- [x] 11. Implement `install_package(name, version=None, kind=None)` —
  probe via `brew list --versions`, dispatch `brew install <name>`
  (or `brew install <name>@<version>` for versioned formulae; with
  `--cask` when `kind=CASK`)

- [x] 12. Implement `remove_package(name, kind=None)` — probe via
  `brew list --versions`, dispatch `brew uninstall <name>` (with
  `--cask` when `kind=CASK`), `SKIPPED` when absent

- [x] 13. Implement `upgrade_package(name, kind=None)` — probe via
  `brew list --versions` + `brew outdated --json=v2 <name>`,
  dispatch `brew upgrade <name>`; `SKIPPED` when not installed
  (message `"package is not installed; nothing to upgrade"`), and
  when no upgrade is available

- [x] 14. Implement `upgrade_all()` — dispatch `brew upgrade`;
  never skips

- [x] 15. Implement `is_installed(name, kind=None)` — `brew list
  --versions [--cask] <name>`; `True` only when exit 0 and stdout
  non-empty

- [x] 16. Implement `get_package_info(name, kind=None)` — dispatch
  `brew info --json=v2 <name>`, parse, map the "No available
  formula" / "No available cask" stderr sentinels to `None`, raise
  on other failures

- [x] 17. Implement `list_installed()` — dispatch `brew list
  --versions` and `brew list --cask --versions`, parse each line
  and merge

- [ ] 18. Implement `list_upgradable()` — deferred to a follow-up
  batch

- [ ] 19. Implement `install_local(filepath)` — deferred to a
  follow-up batch

- [x] 20. Implement `refresh_metadata()` — dispatch `brew update`;
  never skips

- [x] 21. Implement `_dispatch_brew` helper that always passes
  `elevated=False`, expects the argv to already be wrapped by
  `_wrap_with_brew_user` when needed, and honours `dry_run`
  - Spec deviation: the manager does NOT raise when root + brew_user
    is `None`.  The current batch trusts the caller to pass a brew
    user when running as root; a `MacosPackageQueryError` guard can
    be layered on in a follow-up.

## MacosPackageManager — Gaps (specified but not implemented)

- [ ] 22. Mac App Store (`mas`) integration — out of base contract

- [ ] 23. `brew pin` / `brew unpin` for version-hold semantics —
  out of base contract

- [ ] 24. Homebrew bundle (`brew bundle`) support for bulk
  install — out of base contract

- [ ] 25. `installer -pkg <file> -target /` fallback for non-cask
  local packages — out of base contract

## Tests — Implemented

- [x] 26. `test_install_package_invokes_brew_install`
- [x] 27. `test_install_package_with_version_constructs_versioned_formula`
- [x] 28. `test_install_package_skipped_when_installed`
- [ ] 29. `test_install_package_skipped_when_version_matches`
- [x] 30. `test_brew_never_elevates_even_when_config_elevated`
  (merged "install never elevates" with the wider elevation test)
- [x] 31. `test_remove_package_invokes_brew_uninstall`
- [x] 32. `test_remove_package_skipped_when_not_installed`
- [x] 33. `test_upgrade_package_invokes_brew_upgrade`
- [ ] 34. `test_upgrade_package_skipped_when_not_installed_has_message`
- [x] 35. `test_upgrade_package_skipped_when_not_outdated`
- [ ] 36. `test_upgrade_package_skipped_when_version_matches`
- [x] 37. `test_upgrade_all_invokes_brew_upgrade_all`
- [ ] 38. `test_upgrade_all_never_skips`
- [x] 39. `test_is_installed_returns_true_for_installed`
- [x] 40. `test_is_installed_returns_false_for_not_installed`
- [x] 41. `test_get_package_info_parses_brew_info_json`
- [x] 42. `test_get_package_info_returns_none_for_unknown_formula`
- [ ] 43. `test_get_package_info_raises_on_unexpected_error`
- [x] 44. `test_list_installed_parses_brew_list_versions`
- [x] 45. `test_list_installed_raises_on_formula_listing_failure`
  (renamed from the spec's `test_list_installed_raises_on_non_zero_exit`)
- [ ] 46. `test_list_upgradable_parses_outdated_json`
- [ ] 47. `test_list_upgradable_empty_when_no_outdated_formulae`
- [x] 48. `test_install_cask_passes_cask_flag`
  (renamed from the spec's `test_install_local_dispatches_brew_install_cask`)
- [ ] 49. `test_install_local_raises_for_missing_file`
- [x] 50. `test_refresh_metadata_invokes_brew_update`
- [ ] 51. `test_refresh_metadata_never_skips`
- [ ] 52. `test_elevated_config_is_ignored_and_warns_once`
  (covered partially by `test_brew_never_elevates_even_when_config_elevated`;
  the warning assertion is deferred with the `__init__` warning hook)
- [x] 53. `test_brew_user_wraps_with_sudo_u`
- [ ] 54. `test_root_without_brew_user_raises`
- [x] 55. `test_dry_run_short_circuits_install`
- [x] 56. `test_failed_command_surfaces_stderr_in_result`

## Tests — Gaps

- [ ] 57. Test dry-run short-circuits every mutating method without
  invoking the runner (only install is covered explicitly)

- [ ] 58. Test dry-run still raises
  `MacosPackageFileNotFoundError` — blocked on `install_local`

- [x] 59. Test runner is NEVER called with `elevated=True` in any
  scenario — covered by
  `test_brew_never_elevates_even_when_config_elevated`

- [ ] 60. Property-based tests (hypothesis) covering Properties
  1 through 30

## Follow-ups

- [ ] 61. **Cross-OS contract conformance review**: verify the
  `MacosPackageActionResult` / `MacosPackageInfo` field shapes match
  the cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/package-management/contract.md`
  and that the `brew_user` / no-elevation deviation is surfaced
  loudly in the contract's macOS row.

- [ ] 62. **Add `list_upgradable` + `install_local`**: complete the
  contract surface to parity with Ubuntu's `UbuntuPackageManager`.

- [ ] 63. **Root + brew_user guard**: decide whether the manager
  should actively detect running as root without a `brew_user`
  configured and raise `MacosPackageQueryError`, or continue to
  trust the caller.

- [ ] 64. **Mac App Store extension plan**: scope whether the
  `mas` integration belongs in this module or a companion manager.

- [ ] 65. **codecraftai-* reuse check**: confirm no other feature in
  `codecraftai-sysman-macos` invokes `brew` directly; all such calls
  should go through `MacosPackageManager`.
