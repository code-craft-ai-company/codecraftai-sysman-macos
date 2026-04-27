# Tasks

This task list maps the macOS localization-management implementation
to the requirements in `requirements.md` and the design in
`design.md`. Every task is exercised by at least one test in
`source/testroots/testbase/automojo/tests/sysman/macos/test_localizationmanagement.py`.

---

## In-scope tasks

### 1. Module skeleton and exports

- [x] Create
      `source/packages/automojo/sysman/macos/localizationmanagement.py`.
- [x] Export `MacosLocalizationManager`,
      `MacosLocalizationManagerConfig`,
      `MacosLocalizationActionStatus`,
      `MacosLocalizationActionResult`, `MacosLocaleInfo`,
      `MacosKeymapInfo`, `MacosLocalizationQueryError`.
- [x] Subclass `BaseFeatureManager` and set `feature_name =
      "localization-management"`, `result_class =
      MacosLocalizationActionResult`,
      `status_class = MacosLocalizationActionStatus`.

### 2. Read verbs

- [x] `get_locale()` — `defaults read .GlobalPreferences AppleLocale`
      + best-effort `AppleLanguages`. Raises
      `MacosLocalizationQueryError` on stderr-bearing non-zero exit;
      collapses silent non-zero exits to `MacosLocaleInfo(language="")`.
      (Requirements 1)
- [x] `list_supported_locales()` — `locale -a` parser.
      (Requirement 3)
- [x] `get_keymap()` — `defaults read com.apple.HIToolbox
      AppleSelectedInputSources` parser. (Requirement 4)
- [x] `get_supported_keymaps()` — `ls /System/Library/Keyboard
      Layouts/` parser with curated-list fallback. (Requirement 6)
- [x] `get_timezone()` — `systemsetup -gettimezone` parser.
      (Requirement 7)
- [x] `get_supported_timezones()` — `systemsetup -listtimezones`
      parser with header-line strip. (Requirement 9)
- [x] None of the read paths pass `elevated=True`.
      (Requirement 10)

### 3. Write verbs

- [x] `set_locale(locale, *, preferred_language=None)` —
      `defaults write .GlobalPreferences AppleLocale '<value>'`
      with optional `AppleLanguages` second-phase dispatch.
      (Requirement 2)
- [x] `set_keymap(keymap)` — `defaults write
      com.apple.HIToolbox AppleSelectedInputSources -array
      '<plist>'`. (Requirement 5)
- [x] `set_timezone(tz)` — `systemsetup -settimezone <tz>`.
      (Requirement 8)
- [x] Each write honours `config.elevated` on the dispatch step
      and never elevates on the probe step. (Requirement 10)

### 4. Result-shape population

- [x] Populate `result.setting` to `"locale"`, `"keymap"`, or
      `"timezone"` on every returned write result.
- [x] Populate `result.previous_value` from the read probe (when
      it ran cleanly).
- [x] Set `result.requires_reboot=False` on macOS.

### 5. Module-level helpers

- [x] `_parse_apple_locale(text)` — single-line trim parser.
- [x] `_parse_apple_languages(text)` — old-style-plist array
      parser.
- [x] `_parse_input_sources(text)` — extract every
      `KeyboardLayout Name`.
- [x] `_parse_systemsetup_timezone(text)` — strip the
      `Time Zone:` prefix.
- [x] `_parse_systemsetup_listtimezones(text)` — drop the header
      line.
- [x] `_parse_locale_a(text)` — generic one-entry-per-line parser.
- [x] `_parse_keymap_layout_listing(text)` — strip `.bundle` /
      `.keylayout` suffixes.
- [x] `_derive_country(language)` — pure derivation.
- [x] `_build_input_source_plist(layout_name)` — render the
      `AppleSelectedInputSources` write payload.

### 6. Curated keymap fallback

- [x] `_CURATED_KEYMAPS` carries ~25 well-known Apple layout
      short names.
- [x] `get_supported_keymaps()` falls back to the curated list
      when the directory listing returns zero rows.
- [x] `get_supported_keymaps()` never raises.

### 7. Tests

- [x] `test_get_locale_parses_defaults_output`
- [x] `test_get_keymap_parses_defaults_output`
- [x] `test_get_timezone_parses_systemsetup_output`
- [x] `test_list_supported_locales_returns_list`
- [x] `test_get_supported_timezones_parses_list`
- [x] `test_get_supported_keymaps_returns_list_from_listing`
- [x] `test_get_supported_keymaps_falls_back_to_curated_list`
- [x] `test_set_locale_invokes_defaults`
- [x] `test_set_locale_skipped_when_match`
- [x] `test_set_locale_dry_run_short_circuits`
- [x] `test_set_keymap_invokes_defaults`
- [x] `test_set_keymap_skipped_when_match`
- [x] `test_set_timezone_invokes_systemsetup`
- [x] `test_set_timezone_skipped_when_match`
- [x] `test_set_timezone_dry_run_short_circuits`
- [x] `test_writes_elevate_per_policy`
- [x] `test_reads_never_elevate`
- [x] `test_failed_command_surfaces_stderr`
- [x] `test_get_timezone_raises_on_systemsetup_failure`

Total: 19 tests; the cross-OS-divergent contract requires at least
14.

### 8. Spec deliverables

- [x] `specs/3-localization-management/requirements.md`
- [x] `specs/3-localization-management/design.md`
- [x] `specs/3-localization-management/tasks.md`

---

## Out-of-scope (deferred follow-ups)

* **`languagesetup` wrapping** — boot-level locale changes via
  Apple's `languagesetup` are not exposed by this pilot. The
  per-user `AppleLocale` write is sufficient for the cross-OS
  contract's "current user default" scope.
* **Full ICU-style keymap identifiers** — the cross-OS contract's
  "limited keymap surface" intentionally accepts only short names
  (`"U.S."`). Full ICU identifiers (`"com.apple.keylayout.US"`)
  are tracked here as a future-extension surface.
* **Multi-input-source writes** — `set_keymap` writes a single-
  element array. A `set_input_sources(list[str])` verb would let
  callers express the full ordered preference list; deferred.
* **Cross-user mutation** — `defaults write` to a per-user domain
  while running as another user requires `sudo -u <user>`. The
  manager honours `config.elevated` but does not synthesise a
  target-user wrapper; tracked as a follow-up.
* **`MacosTimezoneInfo`** — the contract names a `TimezoneInfo`
  with `utc_offset_seconds` and `dst_active`. macOS surfaces those
  via `precision-time-management`, so this pilot returns a plain
  `str` from `get_timezone`.
* **Region codes / home-location** — Apple's region-code surface
  (separate from locale) is explicitly out of scope per the
  contract.
* **Input-method frameworks** — IME / IBus / SCIM are explicitly
  out of scope per the cross-OS contract.
