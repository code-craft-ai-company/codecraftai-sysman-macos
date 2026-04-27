# Design

## Overview

`MacosLocalizationManager` provides idempotent, dry-run-aware control
over a host's locale, keymap, and timezone settings on macOS. It is
a thin facade over `defaults`, `systemsetup`, `locale -a`, and a
best-effort directory listing of `/System/Library/Keyboard Layouts/`,
dispatched through the injected `ICommandRunner`. The module imports
no `subprocess`, opens no files, and consumes no `IFileOperator`.

The module sits in `automojo.sysman.macos.localizationmanagement` and
exports the following OS-prefixed surface:

| Type | Role |
|------|------|
| `MacosLocalizationManager` | Manager class. |
| `MacosLocalizationManagerConfig` | Tunable per-tool paths plus `dry_run` / `elevated`. |
| `MacosLocalizationActionStatus` | Alias of the package `ActionStatus`. |
| `MacosLocalizationActionResult` | Result dataclass with `setting`, `previous_value`, `requires_reboot`. |
| `MacosLocaleInfo` | `get_locale` return shape. |
| `MacosKeymapInfo` | `get_keymap` return shape. |
| `MacosLocalizationQueryError` | Raised on read-side failures. |

`get_timezone` returns a plain `str` — the IANA timezone name — per
the per-OS verb table.

## Architecture

```
                Maintenance coordinator
                        │
                        │  builds MacosLocalizationManager(runner, config)
                        ▼
            MacosLocalizationManager
                        │
                        │  .run(argv, elevated=…)
                        ▼
                ICommandRunner
                        │
              ┌─────────┴────────┐
              ▼                  ▼
       LocalCommandRunner    SSHCommandRunner
              │                  │
              └────────┬─────────┘
                       │  aspects compose here
                       ▼
            (Persistence | Retry | Timeout)
                       │
                       ▼
       defaults / systemsetup / locale / ls
```

`MacosLocalizationManager` extends `BaseFeatureManager` from
`automojo.sysman.macos.featurebase`. The localization module exposes
its own `_dispatch_localization_write` helper instead of calling
`_dispatch` directly so the result dataclass's per-feature fields
(`setting`, `previous_value`, `requires_reboot`) are populated
explicitly and the read-before-write probe stays inside the manager.

## Verb-by-verb design

### `get_locale() -> MacosLocaleInfo`

Argv pair:
* `["defaults", "read", "<defaults_domain>", "AppleLocale"]`
  (`defaults_domain` defaults to `"NSGlobalDomain"`)
* `["defaults", "read", "<defaults_domain>", "AppleLanguages"]`

Both reads are dispatched with `elevated=False`. The `AppleLanguages`
read is non-fatal: a non-zero exit collapses to an empty list.

Parser: `_parse_apple_locale` strips the single-line value;
`_parse_apple_languages` walks the old-style plist body and returns
the ordered list of quoted tokens.

The country code is derived from the parsed `AppleLocale` value via
`_derive_country`:

| `AppleLocale` | `country` |
|---------------|-----------|
| `en_US` | `US` |
| `fr_FR` | `FR` |
| `en` | `None` |

`encoding`, `lc_messages`, `lc_time`, `lc_numeric` are always `None`
on macOS (Apple's `AppleLocale` carries no encoding suffix; locale
overrides are exposed via `LC_*` environment variables, not via
`.GlobalPreferences`).

Failure surface: a non-zero exit on the `AppleLocale` read with
non-empty stderr raises `MacosLocalizationQueryError`. A non-zero
exit with empty stderr is the standard "key does not exist yet"
path on a fresh install; the parser collapses it to
`MacosLocaleInfo(language="")`.

### `set_locale(locale, *, preferred_language=None) -> MacosLocalizationActionResult`

Argv: `["defaults", "write", "<defaults_domain>", "AppleLocale",
"<locale>"]`

When `preferred_language` is supplied, after the primary write
succeeds the manager additionally dispatches:

`["defaults", "write", "<defaults_domain>", "AppleLanguages",
"-array", "<lang>"]`

Idempotency: probes via `get_locale()`. When the parsed
`AppleLocale` matches AND (when supplied) `preferred_language`
already heads the parsed `preferred_languages` array, returns
`SKIPPED`.

`dry_run` short-circuits before the read probe. Failure on the
primary write returns `FAILED` and the secondary write is not
dispatched.

### `list_supported_locales() -> list[str]`

Argv: `["locale", "-a"]` (BSD form on macOS).

Parser: `_parse_locale_a` strips each line, drops empties.

### `get_keymap() -> MacosKeymapInfo`

Argv: `["defaults", "read", "<hitoolbox_domain>",
"AppleSelectedInputSources"]` (`hitoolbox_domain` defaults to
`"com.apple.HIToolbox"`).

Parser: `_parse_input_sources` walks the old-style plist body,
extracts every `"KeyboardLayout Name" = "<value>";` line, and
returns the ordered list. The first entry is surfaced as
`MacosKeymapInfo.name`.

`layout` mirrors `name` (Apple's Input Sources do not separate the
two). `variant`, `model`, `options` are always `None`.

### `set_keymap(keymap) -> MacosLocalizationActionResult`

Argv: `["defaults", "write", "<hitoolbox_domain>",
"AppleSelectedInputSources", "-array", "<plist>"]`

The `<plist>` argument is rendered by `_build_input_source_plist`:

```
( { "InputSourceKind" = "Keyboard Layout"; "KeyboardLayout Name" =
"<keymap>"; } )
```

Idempotency: probes via `get_keymap()`. Skip when the parsed primary
layout name equals the requested value.

> **Limited keymap surface.** macOS uses Input Sources rather than a
> console-style keymap; the manager accepts well-known short names
> (`"U.S."`, `"British"`, `"Canadian English"`, ...). Full ICU-style
> identifiers (`"com.apple.keylayout.US"`) are NOT supported in this
> pilot.

### `get_supported_keymaps() -> list[str]`

Argv: `["ls", "-1", "<keymap_layouts_dir>"]` (defaults to
`"/System/Library/Keyboard Layouts/"`).

Parser: `_parse_keymap_layout_listing` strips `.bundle` and
`.keylayout` suffixes. When the listing returns zero rows the
manager falls back to a curated bundled list (`_CURATED_KEYMAPS`)
of ~25 well-known Apple layout short names.

This verb never raises.

### `get_timezone() -> str`

Argv: `["systemsetup", "-gettimezone"]`

Parser: `_parse_systemsetup_timezone` looks for the `Time Zone:`
prefix and returns the trimmed remainder.

`systemsetup -gettimezone` does not require root despite
`systemsetup`'s general "must be run as root" pose; the manager
dispatches it with `elevated=False`. On older macOS where the
read also requires root, the dispatch will fail with stderr
"You need administrator access to run this tool" and the manager
will surface a `MacosLocalizationQueryError` carrying that message.

### `set_timezone(tz) -> MacosLocalizationActionResult`

Argv: `["systemsetup", "-settimezone", <tz>]`

`systemsetup -settimezone` requires root. The manager honours
`config.elevated`; the caller is responsible for wiring the runner
with the appropriate elevation mechanism.

Idempotency: probes via `get_timezone()`. Skip when the parsed
timezone equals the requested value.

### `get_supported_timezones() -> list[str]`

Argv: `["systemsetup", "-listtimezones"]`

Parser: `_parse_systemsetup_listtimezones` strips the leading
`Time Zones:` header line and returns the remaining one-per-line
entries.

## Cross-OS locale-format mapping

The cross-OS contract leaves `set_locale` taking a plain `str`. The
following table captures how that string is interpreted on each OS;
on macOS we ingest Apple's underscore-separated form
(`"en_US"`).

| Concept | Ubuntu | macOS | Windows |
|---------|--------|-------|---------|
| Display form | `en_US.UTF-8` (POSIX) | `en_US` (Apple form) | `en-US` (BCP 47) |
| Tool surface | `localectl set-locale LANG=<…>` | `defaults write .GlobalPreferences AppleLocale '<…>'` | `Set-Culture -CultureInfo '<…>'` |
| Encoding suffix | Required for non-ASCII (`.UTF-8`) | Implicit (UTF-8 system-wide) | N/A (UTF-16 internal) |
| Country code | Underscore-separated | Underscore-separated | Hyphen-separated |
| Validation source | `locale -a` (GNU) | `locale -a` (BSD) | `[CultureInfo]::GetCultures(AllCultures)` |

The macOS manager does NOT auto-convert between forms. Callers
working across OSes are expected to render the per-OS form
explicitly. The companion `preferred_language` argument on
`set_locale` accepts the BCP-47 hyphenated form (`"en-US"`) for the
`AppleLanguages` array.

## Persistence and elevation policy

| Operation | Persistence scope | Elevation |
|-----------|-------------------|-----------|
| `defaults read .GlobalPreferences AppleLocale` (read) | per-user | Never |
| `defaults read .GlobalPreferences AppleLanguages` (read) | per-user | Never |
| `defaults read com.apple.HIToolbox AppleSelectedInputSources` (read) | per-user | Never |
| `locale -a` (read) | system | Never |
| `systemsetup -gettimezone` (read) | system | Never |
| `systemsetup -listtimezones` (read) | system | Never |
| `ls /System/Library/Keyboard Layouts/` (read) | system | Never |
| `defaults write .GlobalPreferences AppleLocale` (write) | per-user | Honours `config.elevated` (typically NOT needed) |
| `defaults write .GlobalPreferences AppleLanguages` (write) | per-user | Honours `config.elevated` |
| `defaults write com.apple.HIToolbox AppleSelectedInputSources` (write) | per-user | Honours `config.elevated` |
| `systemsetup -settimezone` (write) | system | Honours `config.elevated` (REQUIRED for the call to succeed) |

`defaults write` to a per-user domain does not require root when
running as the target user. A coordinator running as a different
user must wrap the runner with `sudo -u <user>` (or run as that
user); this manager honours `config.elevated` but does not
synthesise a target-user wrapper.

## Idempotency table

| Verb | Probe argv | Skip condition |
|------|-----------|---------------|
| `set_locale` | `defaults read .GlobalPreferences AppleLocale` (+ `AppleLanguages`) | parsed `AppleLocale` == requested AND (when supplied) `preferred_language` heads the parsed array |
| `set_keymap` | `defaults read com.apple.HIToolbox AppleSelectedInputSources` | parsed primary layout name == requested |
| `set_timezone` | `systemsetup -gettimezone` | parsed timezone == requested |

## Result-dataclass contract

`MacosLocalizationActionResult` extends the package
`FeatureActionResult` with three fields:

| Field | Type | Populated when |
|-------|------|----------------|
| `setting` | `str | None` | Always populated for verbs that target a single field (`"locale"` / `"keymap"` / `"timezone"`). |
| `previous_value` | `str | None` | Populated when the read probe ran cleanly; `None` when the probe failed. |
| `requires_reboot` | `bool` | Always `False` on macOS. `defaults write` to `.GlobalPreferences` is picked up by new login sessions; `systemsetup -settimezone` takes effect immediately. |

## Module-level helpers

* `_parse_apple_locale(text)` — single-line trim of the
  `AppleLocale` payload.
* `_parse_apple_languages(text)` — old-style-plist array parser
  for `AppleLanguages`.
* `_parse_input_sources(text)` — old-style-plist array-of-dicts
  parser that extracts every `KeyboardLayout Name` field.
* `_parse_systemsetup_timezone(text)` — `Time Zone:` line
  extractor.
* `_parse_systemsetup_listtimezones(text)` — header-aware list
  parser for `systemsetup -listtimezones`.
* `_parse_locale_a(text)` — generic one-entry-per-line parser.
* `_parse_keymap_layout_listing(text)` — `ls` output parser
  with `.bundle` / `.keylayout` suffix stripping.
* `_derive_country(language)` — pure derivation of the country
  code from a `<lang>_<country>` value.
* `_build_input_source_plist(layout_name)` — old-style-plist
  renderer for `AppleSelectedInputSources` writes.

Each helper is a pure function so it can be unit-tested in
isolation without a runner stub.

## Curated keymap fallback

`_CURATED_KEYMAPS` carries ~25 well-known Apple layout short names:

```
"U.S.", "U.S. International - PC", "British", "British - PC",
"Canadian English", "Australian", "French", "French - PC",
"German", "German - Standard", "Italian", "Spanish",
"Spanish - ISO", "Swedish", "Swiss French", "Swiss German",
"Dutch", "Norwegian", "Danish", "Finnish", "Portuguese",
"Brazilian", "Russian", "Japanese", "Korean"
```

The fallback fires when `ls /System/Library/Keyboard Layouts/`
returns zero rows (some minimal images), so
`get_supported_keymaps()` always produces a non-empty list a
caller can validate against.

## Failure surface

| Failure | Surface |
|---------|---------|
| `defaults read AppleLocale` exits non-zero with stderr | `MacosLocalizationQueryError` |
| `defaults read AppleLocale` exits non-zero with no stderr | Falls through; `MacosLocaleInfo(language="")` |
| `defaults read AppleLanguages` exits non-zero | Silent; `preferred_languages=[]` |
| `defaults read AppleSelectedInputSources` exits non-zero with stderr | `MacosLocalizationQueryError` |
| `defaults read AppleSelectedInputSources` exits non-zero with no stderr | Falls through; `MacosKeymapInfo(name="")` |
| `locale -a` exits non-zero | `MacosLocalizationQueryError` |
| `systemsetup -gettimezone` exits non-zero | `MacosLocalizationQueryError` |
| `systemsetup -listtimezones` exits non-zero | `MacosLocalizationQueryError` |
| `ls /System/Library/Keyboard Layouts/` exits non-zero | Silent; falls back to `_CURATED_KEYMAPS` |
| `defaults write` exits non-zero | `MacosLocalizationActionResult(status=FAILED)` |
| `systemsetup -settimezone` exits non-zero | `MacosLocalizationActionResult(status=FAILED)` |

Reads raise (typed). Writes never raise; failures are surfaced via
the `FAILED` status carrying the captured `exit_code`, `stdout`,
`stderr`, and `command`.

## Aspect-composition guarantees

The manager makes no assumptions about how the runner dispatches a
command. Retry / persistence / timeout aspects compose
transparently:

* Retry can flip a `FAILED` to `SUCCESS` between attempts.
* Persistence aspects MUST NOT mutate the result.
* Timeout aspects raise their own errors, which propagate through
  the runner unchanged.

## Testing strategy

Tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple. No live
`defaults` / `systemsetup` / `locale` / `ls` is invoked. Coverage:

* Read parsers: `defaults read AppleLocale` + `AppleLanguages`,
  `defaults read AppleSelectedInputSources`, `systemsetup
  -gettimezone`, `systemsetup -listtimezones`, `locale -a`,
  `ls /System/Library/Keyboard Layouts/`.
* Curated fallback: `get_supported_keymaps()` returns the curated
  list when the directory listing is empty.
* Write argv shape: locale, keymap, timezone — including the
  `AppleLanguages` second-phase dispatch.
* Idempotency: each write skips when its probe matches.
* Dry-run: each write returns `DRY_RUN` without invoking the runner.
* Elevation: writes elevate when configured; reads never elevate.
* Failure surface: non-zero writes produce `FAILED` with stderr;
  non-zero reads raise `MacosLocalizationQueryError`.

The minimum 14-test bar is exceeded by the current
`test_localizationmanagement.py` module.

## Out-of-scope notes

* **System-wide locale.** Apple ships `languagesetup` for boot-
  level locale changes; this pilot does not wrap it.
* **Full ICU-style keymap identifiers.** The contract's "limited
  keymap surface" intentionally accepts only short names.
* **Timezone DST / offset surface.** `precision-time-management`
  owns `dst_active` and `utc_offset_seconds` on macOS.
* **Per-user locale via `sudo -u`.** Cross-user mutation requires
  the caller to wire a wrapper into the runner.
* **Region codes.** `Set-WinHomeLocation` / Apple region codes are
  out of scope per the cross-OS contract.

## Future-extension surface

* A `get_active_input_sources()` verb that returns the full
  ordered list (not just the primary layout name).
* A `set_input_sources(list[str])` verb that writes a multi-
  element array to `AppleSelectedInputSources`.
* A `regenerate_locale(locale)` analogue for macOS that wraps
  `languagesetup` for boot-level changes.
* Returning a richer `MacosTimezoneInfo` if and when the
  `precision-time-management` overlap is collapsed.
