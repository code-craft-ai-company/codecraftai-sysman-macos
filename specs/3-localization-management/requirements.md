# Requirements

## Introduction

The localization management feature provides the
`codecraftai-sysman-macos` package with the ability to query and set
host-wide locale, keymap, and timezone settings on macOS hosts.
This spec implements the localization-management contract defined at
`codecraftai-sysman-common/specs/contracts/localization-management/contract.md`
for macOS using `defaults` (locale and input sources), `systemsetup`
(timezone), `locale -a` (BSD locale enumerator), and a best-effort
``ls`` of ``/System/Library/Keyboard Layouts/`` for installed-layout
enumeration.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs. The feature
does not decide which locale, keymap, or timezone the host should
carry — it provides the capability to apply, observe, and idempotently
re-apply those values reliably and with full auditability. Every
system command is executed exclusively through the
`codecraftai-interfaces` command-runner abstraction; this module does
not import `subprocess`, does not call `open`, and does not consume an
`IFileOperator`.

The OS-prefixed types in this module are
`MacosLocalizationManager`, `MacosLocalizationManagerConfig`,
`MacosLocalizationActionStatus` (alias of the package-shared
`ActionStatus`), `MacosLocalizationActionResult`,
`MacosLocaleInfo`, `MacosKeymapInfo`, and
`MacosLocalizationQueryError`. There is no `MacosTimezoneInfo`
dataclass: `get_timezone` returns a plain `str` (the IANA timezone
name) per the per-OS verb table. The contract's abstract
`TimezoneInfo` (with `utc_offset_seconds` and `dst_active`) overlaps
with `precision-time-management` and is owned there on macOS.

---

## Requirements

### Requirement 1: Query Current Locale

**User Story:** As a maintenance coordinator, I want to query the
active system locale, so that I can compare it against the declared
desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_locale()` THE SYSTEM SHALL invoke
   `defaults read .GlobalPreferences AppleLocale` through the
   command runner with `elevated=False`.
2. THE SYSTEM SHALL invoke `defaults read .GlobalPreferences
   AppleLanguages` to enumerate the user's preferred-language list
   (best-effort; a non-zero exit on this read is silently treated as
   an empty list).
3. THE SYSTEM SHALL return a `MacosLocaleInfo` carrying the parsed
   `AppleLocale` value as `language`, the derived country tag when
   present (e.g. `"US"` for `"en_US"`), the verbatim raw stdout as
   `system_default`, and the parsed `AppleLanguages` array as
   `preferred_languages`.
4. WHEN `defaults read AppleLocale` exits non-zero AND emits stderr
   THE SYSTEM SHALL raise `MacosLocalizationQueryError` carrying the
   failed argv, exit code, and stderr.
5. WHEN `defaults read AppleLocale` exits non-zero AND emits no
   stderr (the standard "key does not exist yet" path) THE SYSTEM
   SHALL fall through to a parse with empty inputs, returning an
   `MacosLocaleInfo(language="")`.
6. THE SYSTEM SHALL never pass `elevated=True` to the runner.

---

### Requirement 2: Set System Locale

**User Story:** As a maintenance coordinator, I want to set the
`AppleLocale` value (and optional preferred language), so that the
host applies the declared user-facing language.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_locale(locale)` THE SYSTEM SHALL
   invoke `defaults write .GlobalPreferences AppleLocale '<locale>'`
   through the command runner.
2. WHEN the caller supplies `preferred_language` THE SYSTEM SHALL
   additionally invoke `defaults write .GlobalPreferences
   AppleLanguages -array '<lang>'` after the primary write succeeds.
3. WHEN the parsed `AppleLocale` already equals the requested value
   AND (when supplied) `preferred_language` already heads the parsed
   `AppleLanguages` array THE SYSTEM SHALL return
   `MacosLocalizationActionResult(status=SKIPPED)` without invoking
   any write.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner at all.
5. WHEN the runner reports a non-zero exit on the primary write THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated, and SHALL NOT dispatch the
   `AppleLanguages` write.
6. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write step.
7. THE SYSTEM SHALL populate `result.setting="locale"` and
   `result.previous_value` (when the read probe succeeded) on every
   returned result.

> **Per-user persistence.** `defaults write .GlobalPreferences`
> mutates the *current user's* preferences and does NOT require root
> by default. Callers wanting cross-user mutation must wrap the
> dispatch with `sudo -u <user>` via the runner; this manager honours
> `config.elevated` but does not synthesise a target user.

---

### Requirement 3: Enumerate Supported Locales

**User Story:** As a maintenance coordinator, I want to enumerate
locales installed on the host, so that I can validate locale values
against what the host actually carries.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_supported_locales()` THE SYSTEM
   SHALL invoke `locale -a` through the command runner with
   `elevated=False` and parse the one-entry-per-line output.
2. THE SYSTEM SHALL return a `list[str]` of locale identifiers in
   source order with empty lines elided.
3. WHEN `locale -a` exits non-zero THE SYSTEM SHALL raise
   `MacosLocalizationQueryError`.

---

### Requirement 4: Query Current Keymap

**User Story:** As a maintenance coordinator, I want to query the
active keyboard input source, so that I can compare it against the
declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_keymap()` THE SYSTEM SHALL invoke
   `defaults read com.apple.HIToolbox AppleSelectedInputSources`
   through the command runner with `elevated=False`.
2. THE SYSTEM SHALL parse the `KeyboardLayout Name` lines out of the
   old-style plist body and return a `MacosKeymapInfo` whose `name`
   is the first parsed layout (the empty string when the plist has
   never been written).
3. THE SYSTEM SHALL set `MacosKeymapInfo.layout` equal to `name` on
   macOS — Apple's Input Sources do not separate layout from name.
4. THE SYSTEM SHALL set `variant`, `model`, and `options` to `None`
   on macOS — Linux's X11-keyboard concept does not apply.
5. WHEN `defaults read` exits non-zero AND emits stderr THE SYSTEM
   SHALL raise `MacosLocalizationQueryError`.
6. WHEN `defaults read` exits non-zero AND emits no stderr (the
   "does not exist yet" path) THE SYSTEM SHALL return
   `MacosKeymapInfo(name="")`.

---

### Requirement 5: Set Keymap

**User Story:** As a maintenance coordinator, I want to set the
keyboard input source, so that the host applies a declared keyboard
layout.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_keymap(keymap)` THE SYSTEM SHALL
   invoke `defaults write com.apple.HIToolbox
   AppleSelectedInputSources -array '<plist>'` through the runner,
   where `<plist>` is a single-element old-style array carrying
   `KeyboardLayout Name = "<keymap>"`.
2. WHEN the parsed primary input-source name already equals
   `keymap` THE SYSTEM SHALL return `status=SKIPPED` without
   invoking the runner.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, `command`
   populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write step.
6. THE SYSTEM SHALL populate `result.setting="keymap"` on every
   returned result.

> **Limited keymap surface.** macOS uses Input Sources rather than a
> console-style keymap. This manager accepts the well-known short
> names (`"U.S."`, `"British"`, `"Canadian English"`, etc.) and
> renders the corresponding old-style plist payload. The full
> ICU-style identifier surface (`"com.apple.keylayout.US"`) is NOT
> supported in this pilot and is documented as a follow-up in
> `tasks.md`.

---

### Requirement 6: Enumerate Supported Keymaps

**User Story:** As a maintenance coordinator, I want to enumerate the
keyboard layouts the host knows about, so that I can validate keymap
values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_keymaps()` THE SYSTEM
   SHALL invoke `ls -1 /System/Library/Keyboard\ Layouts/` through
   the runner with `elevated=False` and strip `.bundle` /
   `.keylayout` suffixes.
2. WHEN the listing returns zero rows (some minimal images) THE
   SYSTEM SHALL fall back to a curated bundled list of well-known
   Apple layout short names so the verb always returns a non-empty
   list.
3. THE SYSTEM SHALL never raise on this verb; failures collapse to
   the curated fallback.

---

### Requirement 7: Query Current Timezone

**User Story:** As a maintenance coordinator, I want to query the
active system timezone, so that I can compare it against the
declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_timezone()` THE SYSTEM SHALL
   invoke `systemsetup -gettimezone` through the command runner with
   `elevated=False`.
2. THE SYSTEM SHALL parse the `Time Zone:` line out of the response
   and return the trimmed IANA timezone string.
3. WHEN `systemsetup -gettimezone` exits non-zero THE SYSTEM SHALL
   raise `MacosLocalizationQueryError`.

---

### Requirement 8: Set Timezone

**User Story:** As a maintenance coordinator, I want to set the
system timezone, so that local-clock displays and scheduled jobs
honour the requested wall clock.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_timezone(tz)` THE SYSTEM SHALL
   invoke `systemsetup -settimezone <tz>` through the runner.
2. WHEN the parsed timezone already matches the requested value THE
   SYSTEM SHALL return `status=SKIPPED` without invoking the runner.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, `command`
   populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write step. `systemsetup
   -settimezone` requires root; the manager assumes the runner is
   wired with the appropriate elevation mechanism.
6. THE SYSTEM SHALL populate `result.setting="timezone"` and
   `result.previous_value` (when the read probe succeeded) on every
   returned result.

---

### Requirement 9: Enumerate Supported Timezones

**User Story:** As a maintenance coordinator, I want to enumerate
the IANA timezones the host knows about, so that I can validate
timezone values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_timezones()` THE SYSTEM
   SHALL invoke `systemsetup -listtimezones` through the runner with
   `elevated=False`.
2. THE SYSTEM SHALL strip the leading `Time Zones:` header line and
   return the remaining one-per-line entries in source order.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise
   `MacosLocalizationQueryError`.

---

### Requirement 10: Elevation and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write
   (`set_locale`, `set_keymap`, `set_timezone`).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_locale`, `list_supported_locales`, `get_keymap`,
   `get_supported_keymaps`, `get_timezone`,
   `get_supported_timezones`, every idempotency probe).
4. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
5. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner.
