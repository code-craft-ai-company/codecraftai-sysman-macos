# Implementation Plan -- Bootloader Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `bootloadermanagement` module under
  `source/packages/ccai/sysman/macos/`.

- [x] 2. Alias `MacosBootloaderActionStatus = ActionStatus`.

- [x] 3. Define `MacosBootEntryKind` enum with the values
  `BLESS_VOLUME`, `KERNEL_ENTRY`, `LOADER_ENTRY` (only
  ``BLESS_VOLUME`` results in a write).

- [x] 4. Define `MacosBootloaderManagerConfig` dataclass with
  `dry_run`, `elevated`, and `default_volume`.

- [x] 5. Define `MacosBootloaderActionResult` dataclass extending
  `FeatureActionResult` with `entry_id`, `requires_reboot`, and
  `message`.

- [x] 6. Define `MacosBootEntrySpec` dataclass holding `entry_kind`,
  `volume_path`, and `name`.

- [x] 7. Define `MacosBootEntryInfo` dataclass holding `id`, `name`,
  `kernel`, `initrd`, `cmdline`, `device`, `is_default`, and
  `boot_count`.

- [x] 8. Define `MacosBootloaderQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 9. Implement `_parse_bless_info(text)` that walks
  ``bless --info --verbose`` output and returns a key-keyed dict.

- [x] 10. Implement `_parse_nvram_boot_args(text)` that returns the
  payload from a `nvram boot-args` line.

- [x] 11. Implement `_extract_current_boot_path(parsed)` that picks
  the active boot path from the parsed bless dict.

## MacosBootloaderManager Verbs

- [x] 12. Implement `list_entries()` combining
  `bless --info --verbose` and `nvram boot-args`.

- [x] 13. Implement `get_default_entry()` preferring
  `bless --info --getBoot` and falling back to verbose parsing.

- [x] 14. Implement `set_default_entry(entry_id)` running
  `bless --mount <path> --setBoot` with idempotency probe.

- [x] 15. Implement `add_entry(spec)` accepting only
  `BLESS_VOLUME`; running `bless --mount <path>`.  Reject other
  kinds with `unsupported_entry_kind`.

- [x] 16. Implement `remove_entry(entry_id)` running
  `bless --unbless <path>` with idempotency probe.

- [x] 17. Implement `update_bootloader_config()` returning a
  SUCCESS no-op carrying the standard message.

- [x] 18. Implement `regenerate_config()` returning a SUCCESS
  no-op carrying the standard message.

## MacosBootloaderManager Internal Helpers

- [x] 19. Implement `_read_current_boot_path()` (terse + verbose
  fallback).

- [x] 20. Implement `_read_boot_args()` running `nvram boot-args`.

- [x] 21. Implement `_volume_already_blessed(volume_path)` for
  idempotency probes.

- [x] 22. Implement `_synthesize_default_entry(boot_path, cmdline)`
  for the configured-default-volume fallback.

- [x] 23. Implement `_build_result(...)` constructing a
  `MacosBootloaderActionResult` without dispatch.

## Tests

- [x] 24. `test_list_entries_parses_bless_output` -- bless parser.

- [x] 25. `test_get_default_entry_resolves_id` -- terse-form
  resolution.

- [x] 26. `test_get_default_entry_raises_when_unresolvable` -- typed
  error.

- [x] 27. `test_set_default_entry_invokes_bless` -- happy path.

- [x] 28. `test_set_default_entry_skipped_when_already_default` --
  idempotency.

- [x] 29. `test_add_entry_bless_volume_invokes_bless` -- happy
  path.

- [x] 30. `test_add_entry_skipped_when_present` -- idempotency.

- [x] 31. `test_add_entry_dry_run_short_circuits` -- dry-run.

- [x] 32. `test_add_entry_unsupported_kind_returns_failed` --
  rejection.

- [x] 33. `test_remove_entry_invokes_unbless` -- happy path.

- [x] 34. `test_remove_entry_skipped_when_absent` -- idempotency.

- [x] 35. `test_update_bootloader_config_returns_noop_success` --
  no-op.

- [x] 36. `test_regenerate_config_returns_noop_success` -- no-op.

- [x] 37. `test_writes_elevate` -- elevation policy.

- [x] 38. `test_reads_never_elevate` -- elevation policy.

- [x] 39. `test_failed_command_surfaces_stderr` -- error surface.

## Follow-ups

- [ ] 40. Add a multi-bless inventory probe via `diskutil list`
  parsing once the macOS sysman package exposes diskutil bindings.
  The current implementation only consults the active boot device
  for idempotency.

- [ ] 41. Surface the `com.apple.Boot.plist` root device hint when
  diverged from the bless'd volume.  Current behaviour relies on
  `bless` keeping the plist consistent.

- [ ] 42. Add Apple-Silicon-specific guidance once
  `bputil` (the boot-policy utility) is wired in for setting
  reduced-security boot policies.  Out of scope here.
