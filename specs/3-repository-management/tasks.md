# Implementation Plan — macOS Repository Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.repositorymanagement`
  module

- [x] 2. Define `MacosRepositoryActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)
  - Aliased to the shared :class:`ActionStatus` via the
    ``.macos.featurebase`` base.

- [x] 3. Define `MacosRepositoryManagerConfig` dataclass (dry_run,
  elevated, brew_user)
  - Spec revision: the manager accepts `brew_user` (no-root rule)
    rather than `repos_dir` / `include_urls`.  Homebrew exposes no
    filesystem surface comparable to `/etc/apt/sources.list.d`, so
    `repos_dir` is not meaningful; `include_urls` is deferred with
    `brew tap-info` enrichment.

- [x] 4. Define `MacosRepositorySpec` and `MacosRepositoryInfo`
  dataclasses
  - Spec revision: the classes carry `name`, `url`, and
    `is_cask_only` (the fields Homebrew actually needs).  The
    cross-OS `display_name`, `gpg_check`, `gpg_key`, `priority`, and
    `enabled` fields are NOT surfaced — `brew tap` has no
    corresponding native concept.

- [x] 5. Define `MacosRepositoryActionResult` dataclass (includes the
  `message` field for SKIPPED / FAILED explanations)

- [x] 6. Define `MacosRepositoryQueryError` exception
  - Spec deviation: `MacosRepositoryNotFoundError` and
    `MacosRepositoryKeyFileNotFoundError` are NOT defined because
    `set_priority` and `import_gpg_key` are not implemented in this
    batch.

## Parsing Helpers

- [x] 7. Implement `_parse_brew_tap_output(stdout)` returning a list
  of `MacosRepositoryInfo` rows built from non-empty lines

- [x] 8. Implement `_parse_brew_tap_names(stdout)` used by the
  `add_repository` / `remove_repository` idempotency guards
  - Spec revision: named differently from the spec's
    `_parse_brew_tap_info_json` because URL enrichment via
    `brew tap-info` is deferred.

- [ ] 9. Implement `_warn_na_fields(spec)` at the manager level for a
  one-time warning per field
  - Deferred: the manager currently accepts unknown fields silently
    because `MacosRepositorySpec` only exposes fields Homebrew
    actually consumes.

## MacosRepositoryManager — Implemented

- [x] 10. Implement `__init__` accepting runner and optional config
  - Inherited from :class:`BaseFeatureManager`; the one-time
    warning on `elevated=True` is NOT emitted because
    `_dispatch_brew` ignores `config.elevated` entirely.

- [x] 11. Implement `add_repository(spec)` — idempotency via
  `brew tap`, dispatch `brew tap <name> [<url>]`

- [x] 12. Implement `remove_repository(name)` — idempotency via
  `brew tap`, dispatch `brew untap <name>`

- [x] 13. Implement `enable_repository(name)` — no runner mutation;
  returns `SKIPPED` with an explanatory message ("homebrew taps
  are always enabled when present; enable_repository is a no-op")
  - Spec revision: the current batch does NOT distinguish between
    "tap present (SKIPPED)" and "tap absent (FAILED)"; Homebrew has
    no separate enabled state, so the verb is unconditionally
    SKIPPED.

- [x] 14. Implement `disable_repository(name)` — no runner mutation;
  always `FAILED` with `message="homebrew_no_disable: untap_required"`

- [x] 15. Implement `list_repositories()` — `brew tap` dispatch and
  parse
  - Spec deviation: the optional `brew tap-info --json` URL
    enrichment is NOT implemented in this batch.  The resulting
    `MacosRepositoryInfo.url` is always `None`.

- [ ] 16. Implement `get_repository(name)` — enumerate + filter.
  Deferred; the primary caller goal is covered by calling
  `list_repositories` and filtering in user code.

- [x] 17. Implement `refresh_repositories()` — dispatch
  `brew update`, never skips
  - Spec deviation: the `name` parameter is NOT surfaced in this
    batch; `brew update` always refreshes every tap.

- [ ] 18. Implement `set_priority(name, priority)` — deferred with
  `MacosRepositoryNotFoundError`

- [ ] 19. Implement `import_gpg_key(key_source)` — deferred with
  `MacosRepositoryKeyFileNotFoundError`

- [x] 20. Implement `_dispatch_brew` helper that enforces
  `elevated=False` always, populates `command`, and honours
  `dry_run`

## MacosRepositoryManager — Gaps

- [ ] 21. Homebrew bundle file (Brewfile) support — NOT implemented

- [ ] 22. Formula-level operations — NOT implemented; see
  `MacosPackageManager`

- [ ] 23. MacPorts / Fink / Nix integration — NOT implemented

## Tests — Implemented

- [x] 24. `test_add_repository_invokes_brew_tap`
  (renamed from the spec's `test_add_tap_without_url_dispatches_brew_tap_name`)
- [x] 25. `test_add_repository_with_url_passes_url`
- [x] 26. `test_add_repository_skipped_when_already_tapped`
- [ ] 27. `test_add_tap_warns_on_na_fields` — deferred with
  `_warn_na_fields`
- [x] 28. `test_remove_repository_invokes_brew_untap`
- [x] 29. `test_remove_repository_skipped_when_not_tapped`
- [x] 30. `test_enable_repository_returns_skipped_with_message`
  (the spec's present / absent split collapses to one case on
  Homebrew because taps have no separate enabled state)
- [ ] 31. `test_enable_repository_failed_when_absent` — N/A
- [ ] 32. `test_enable_repository_never_mutates` — covered by the
  SKIPPED assertion and the `len(runner.calls) == 0` check
- [x] 33. `test_disable_repository_returns_failed_with_message`
- [ ] 34. `test_disable_repository_never_mutates` — covered by the
  `len(runner.calls) == 0` check
- [x] 35. `test_list_repositories_parses_brew_tap_output`
- [x] 36. `test_list_repositories_raises_on_runner_failure`
- [ ] 37. `test_list_repositories_optional_include_urls` — deferred
  with `brew tap-info` enrichment
- [ ] 38. `test_get_repository_returns_none_on_miss` — deferred
  with `get_repository`
- [x] 39. `test_refresh_repositories_invokes_brew_update`
- [ ] 40. `test_refresh_one_dispatches_brew_update_name` — deferred
  with the `name` parameter on `refresh_repositories`
- [ ] 41. `test_set_priority_raises_when_tap_absent` — deferred
- [ ] 42. `test_set_priority_skipped_when_tap_present` — deferred
- [ ] 43. `test_import_gpg_key_missing_local_raises` — deferred
- [ ] 44. `test_import_gpg_key_skipped_otherwise` — deferred
- [ ] 45. `test_import_gpg_key_never_invokes_runner` — deferred
- [x] 46. `test_dry_run_short_circuits_add`
- [x] 47. `test_brew_never_elevates_even_when_config_elevated`
- [x] 48. `test_brew_user_wraps_with_sudo_u`
- [x] 49. `test_failed_command_surfaces_stderr_in_result`

## Tests — Gaps

- [x] 50. Test runner is NEVER invoked with `elevated=True` — covered
  by `test_brew_never_elevates_even_when_config_elevated`

- [ ] 51. Test `elevated=True` in config emits a one-time warning —
  deferred with the `__init__` warning hook

- [ ] 52. Test dry-run short-circuits for every mutating method —
  only `add_repository` is covered explicitly; `remove_repository`
  / `refresh_repositories` inherit the same
  `_dispatch_brew` dry-run guard

- [ ] 53. Property-based tests (hypothesis) covering Properties
  1 through 30

## Follow-ups

- [ ] 54. **Cross-OS contract conformance review**: confirm that
  ``enable_repository`` returning SKIPPED and
  ``disable_repository`` returning FAILED are acceptable contract
  deviations, and surface them in the contract's macOS row.

- [ ] 55. **Tap URL enrichment**: benchmark `brew tap-info --json`
  and wire up the `include_urls` flag on the config so
  `list_repositories` can surface `MacosRepositoryInfo.url` when
  requested.

- [ ] 56. **Single-tap refresh**: decide whether the `name`
  parameter on `refresh_repositories` is worth supporting; Homebrew
  has no documented `brew update <name>` subcommand, so it would
  likely fan out to `brew tap --repair <name>` or similar.

- [ ] 57. **GPG / priority deferrals**: revisit `set_priority` and
  `import_gpg_key` once cross-OS callers actually need them on
  macOS; per the contract these are allowed to be no-ops.
