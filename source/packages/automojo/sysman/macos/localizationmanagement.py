"""
    macOS localization-management feature.

    Implements the cross-OS localization-management contract for macOS
    using ``defaults`` (the canonical persistent surface for the system
    locale and keyboard input sources), ``systemsetup`` (the canonical
    persistent surface for the timezone), and ``locale -a`` (the BSD
    enumerator for installed POSIX locales).  Every system command is
    dispatched through the injected :class:`ICommandRunner`; this
    module never imports ``subprocess`` and never opens a file directly.

    Verb catalogue:
        * ``get_locale`` -> ``defaults read .GlobalPreferences
          AppleLocale`` plus ``defaults read .GlobalPreferences
          AppleLanguages`` (the latter is opportunistic and surfaces the
          ordered preferred-language list).
        * ``set_locale`` -> ``defaults write .GlobalPreferences
          AppleLocale '<value>'`` plus, when the caller supplies a
          BCP-47-ish ``preferred_language``, ``defaults write
          .GlobalPreferences AppleLanguages '("<lang>")'``.
        * ``list_supported_locales`` -> ``locale -a``.
        * ``get_keymap`` -> ``defaults read
          ~/Library/Preferences/com.apple.HIToolbox
          AppleSelectedInputSources``.
        * ``set_keymap`` -> ``defaults write
          ~/Library/Preferences/com.apple.HIToolbox
          AppleSelectedInputSources '<plist>'``.  macOS uses Input
          Sources rather than a console-style keymap; this manager
          documents the limited surface and accepts well-known names
          (e.g. ``"U.S."``, ``"British"``).
        * ``get_timezone`` -> ``systemsetup -gettimezone``.
        * ``set_timezone`` -> ``systemsetup -settimezone <iana>``.
        * ``get_supported_timezones`` -> ``systemsetup -listtimezones``.
        * ``get_supported_keymaps`` -> best-effort directory listing of
          ``/System/Library/Keyboard Layouts/`` via ``ls -1``, falling
          back to a curated bundled list when the listing produces zero
          rows.

    Persistence model:
        macOS distinguishes per-user defaults
        (``~/Library/Preferences/...``) from system-wide defaults
        (``/Library/Preferences/...``).  ``defaults write
        .GlobalPreferences AppleLocale`` mutates the *current user's*
        preferences and does NOT require root.  ``systemsetup
        -settimezone`` mutates a system-level configuration and DOES
        require root.  This manager binds each verb to its native
        scope:

        * ``set_locale`` -> per-user (no root needed by default;
          honours ``config.elevated`` when the caller wants to wrap
          the dispatch with ``sudo`` for cross-user runs).
        * ``set_keymap`` -> per-user.
        * ``set_timezone`` -> system-wide; honours
          ``config.elevated``.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner` (``defaults`` / ``systemsetup`` /
        ``locale`` / ``ls``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Reads (``get_locale``, ``list_supported_locales``,
        ``get_keymap``, ``get_supported_keymaps``, ``get_timezone``,
        ``get_supported_timezones``, every idempotency probe) never
        elevate.  Writes honour ``config.elevated``.

    Idempotency:
        * ``set_locale`` reads ``AppleLocale`` and skips when the
          parsed value matches.
        * ``set_keymap`` reads
          ``AppleSelectedInputSources`` and skips when the requested
          input-source name appears in the parsed plist.
        * ``set_timezone`` reads ``systemsetup -gettimezone`` and skips
          when the parsed timezone matches.

    Curated keymap fallback:
        ``ls /System/Library/Keyboard\\ Layouts/`` is the canonical
        enumeration of installed Apple keyboard-layout bundles, but the
        directory is empty on some minimal images and the user-installed
        layouts live under ``~/Library/Keyboard Layouts/`` (which would
        need a per-user listing).  The manager falls back to a curated
        bundled list of well-known short names when the system listing
        returns zero rows.
"""

import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------


_DEFAULTS = "defaults"
_SYSTEMSETUP = "systemsetup"
_LOCALE = "locale"
_LS = "ls"

_GLOBAL_DOMAIN = "NSGlobalDomain"
_HITOOLBOX_DOMAIN = (
    "/Users/Shared/.GlobalPreferences"
    # NOTE: actual per-user HIToolbox lives under ~/Library/Preferences
    # but the relative form ``-currentHost com.apple.HIToolbox`` is the
    # canonical addressing.  The default is below.
)

# The standard input-source plist domain for the current user.  When
# the manager is configured to operate against a different user, the
# caller can override via ``MacosLocalizationManagerConfig``.
_DEFAULT_HITOOLBOX = "com.apple.HIToolbox"

_APPLE_LOCALE_KEY = "AppleLocale"
_APPLE_LANGUAGES_KEY = "AppleLanguages"
_INPUT_SOURCES_KEY = "AppleSelectedInputSources"

# ``systemsetup -gettimezone`` emits exactly ``Time Zone: <iana>``.
_TIMEZONE_PFX = "Time Zone:"

# Curated fallback list of well-known macOS keyboard-layout short
# names.  Used by :meth:`get_supported_keymaps` when the directory
# listing under ``/System/Library/Keyboard Layouts/`` returns zero
# rows (some minimal images).  The list is intentionally short and
# documented in design.md.
_CURATED_KEYMAPS = (
    "U.S.",
    "U.S. International - PC",
    "British",
    "British - PC",
    "Canadian English",
    "Australian",
    "French",
    "French - PC",
    "German",
    "German - Standard",
    "Italian",
    "Spanish",
    "Spanish - ISO",
    "Swedish",
    "Swiss French",
    "Swiss German",
    "Dutch",
    "Norwegian",
    "Danish",
    "Finnish",
    "Portuguese",
    "Brazilian",
    "Russian",
    "Japanese",
    "Korean",
)


MacosLocalizationActionStatus = ActionStatus


@dataclass
class MacosLocalizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosLocalizationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param defaults_domain: The ``defaults`` domain used for the
                                global preferences (``AppleLocale``,
                                ``AppleLanguages``).  Defaults to
                                ``"NSGlobalDomain"``, which is the
                                canonical addressing for the current
                                user's ``.GlobalPreferences`` plist.
                                Tests override to inject a fixture
                                domain.
        :param hitoolbox_domain: The ``defaults`` domain used for the
                                 keyboard-input-source plist.  Defaults
                                 to ``"com.apple.HIToolbox"``.
        :param defaults_path: Path / name of the ``defaults`` binary.
                              Defaults to ``"defaults"``.
        :param systemsetup_path: Path / name of the ``systemsetup``
                                 binary.  Defaults to
                                 ``"systemsetup"``.
        :param locale_tool_path: Path / name of the ``locale`` binary
                                 used by :meth:`list_supported_locales`.
                                 Defaults to ``"locale"``.
        :param keymap_layouts_dir: Directory listed by
                                   :meth:`get_supported_keymaps` to
                                   discover installed Apple keyboard
                                   layouts.  Defaults to
                                   ``"/System/Library/Keyboard
                                   Layouts/"``.
    """
    defaults_domain: str = _GLOBAL_DOMAIN
    hitoolbox_domain: str = _DEFAULT_HITOOLBOX
    defaults_path: str = _DEFAULTS
    systemsetup_path: str = _SYSTEMSETUP
    locale_tool_path: str = _LOCALE
    keymap_layouts_dir: str = "/System/Library/Keyboard Layouts/"


@dataclass
class MacosLocalizationActionResult(FeatureActionResult):
    """
        Result of a localization-management write operation.

        :param setting: The localization field targeted: one of
                        ``"locale"``, ``"keymap"``, or ``"timezone"``.
                        ``None`` for compound operations that span more
                        than one field.
        :param previous_value: The value read prior to the mutation,
                               when the manager was able to probe it.
                               ``None`` when the probe failed.
        :param requires_reboot: Set when the underlying mutation
                                requires a reboot to take effect.
                                ``defaults write`` to
                                ``.GlobalPreferences`` is picked up by
                                new login sessions, not the running
                                shell, so the field is left ``False``;
                                callers that want a reboot signal
                                should consult the field on a future
                                ``set_locale_system_wide`` extension.
    """
    setting: Optional[str] = None
    previous_value: Optional[str] = None
    requires_reboot: bool = False


@dataclass
class MacosLocaleInfo:
    """
        Snapshot of the macOS locale state as parsed from
        ``defaults read``.

        :param language: The active ``AppleLocale`` value (e.g.
                         ``"en_US"``).  May be the empty string when
                         the value has never been set on the host.
        :param country: Country code derived from ``language`` when the
                        ``language`` value contains an underscore-
                        separated region (e.g. ``"US"`` for
                        ``"en_US"``).  ``None`` when not derivable.
        :param encoding: Always ``None`` on macOS — Apple's
                         ``AppleLocale`` does not carry an encoding
                         suffix; the field is reserved on the dataclass
                         for cross-OS parity.
        :param lc_messages: Always ``None`` on macOS — the BSD locale
                            tooling exposes overrides via ``LC_*``
                            environment variables, not via
                            ``.GlobalPreferences``.
        :param lc_time: Same as ``lc_messages``.
        :param lc_numeric: Same as ``lc_messages``.
        :param system_default: Verbatim raw stdout from
                               ``defaults read .GlobalPreferences
                               AppleLocale``, for callers that want
                               the original payload.
        :param preferred_languages: Ordered list of language tags
                                    parsed from
                                    ``defaults read .GlobalPreferences
                                    AppleLanguages`` (e.g.
                                    ``["en-US", "fr-FR"]``).  Empty
                                    when the key is unset.
        :param raw: Mapping of every parsed field name to its raw
                    string value.  Always populated.
    """
    language: str
    country: Optional[str] = None
    encoding: Optional[str] = None
    lc_messages: Optional[str] = None
    lc_time: Optional[str] = None
    lc_numeric: Optional[str] = None
    system_default: Optional[str] = None
    preferred_languages: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class MacosKeymapInfo:
    """
        Snapshot of the keyboard configuration parsed from
        ``defaults read com.apple.HIToolbox AppleSelectedInputSources``.

        :param name: The active input-source name (e.g. ``"U.S."``,
                     ``"British"``).  May be the empty string when the
                     plist has never been written.
        :param layout: Always equal to ``name`` on macOS — Apple's
                       Input Sources do not separate layout from name.
                       The field is preserved for cross-OS parity.
        :param variant: Always ``None`` on macOS.
        :param model: Always ``None`` on macOS — Linux's
                      X11-keyboard-model concept does not apply.
        :param options: Always ``None`` on macOS.
    """
    name: str
    layout: Optional[str] = None
    variant: Optional[str] = None
    model: Optional[str] = None
    options: Optional[str] = None


class MacosLocalizationQueryError(Exception):
    """
        Raised when a localization read fails for a reason other than a
        plainly empty payload (which the parsers tolerate).
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used when there
                            is no failed argv to report.
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Localization query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_apple_locale(*, text: str) -> str:
    """
        Parse the output of ``defaults read .GlobalPreferences
        AppleLocale``.

        ``defaults`` emits a single-line value (e.g. ``en_US``) on
        success.  Some macOS builds wrap the value in surrounding
        whitespace; the parser strips both leading and trailing.

        :param text: Raw stdout.

        :returns: The trimmed locale string.  Empty string when the
                  payload is empty.
    """
    if text is None:
        rtnval = ""
        return rtnval
    rtnval = text.strip()
    return rtnval


def _parse_apple_languages(*, text: str) -> list:
    """
        Parse the output of ``defaults read .GlobalPreferences
        AppleLanguages``.

        ``defaults`` emits an old-style plist array::

            (
                "en-US",
                "fr-FR"
            )

        The parser returns the ordered list of tag strings with quotes
        and the surrounding parentheses stripped.  It is deliberately
        forgiving — any line that contains a quoted token is treated
        as an entry; lines with only ``(`` / ``)`` / ``,`` are
        ignored.

        :param text: Raw stdout.

        :returns: A list of language tags.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line in ("(", ")", ","):
            continue
        # Strip a single trailing comma if present.
        if line.endswith(","):
            line = line[:-1].strip()
        # Strip surrounding double quotes if present.
        if len(line) >= 2 and line.startswith('"') and line.endswith('"'):
            inner = line[1:-1]
            rtnval.append(inner)
            continue
        # Some Apple plist printouts emit unquoted tokens.
        if line.startswith('"') or line.endswith('"'):
            # Mismatched quotes — best-effort: drop them.
            inner = line.strip('"')
            if len(inner) > 0:
                rtnval.append(inner)
            continue
        rtnval.append(line)
    return rtnval


def _parse_input_sources(*, text: str) -> list:
    """
        Parse the output of ``defaults read com.apple.HIToolbox
        AppleSelectedInputSources``.

        ``defaults`` emits an old-style plist array of dicts::

            (
                    {
                "InputSourceKind" = "Keyboard Layout";
                "KeyboardLayout ID" = 0;
                "KeyboardLayout Name" = "U.S.";
            }
            )

        The parser scans for ``KeyboardLayout Name`` lines and returns
        the list of layout names in source order.  It is deliberately
        forgiving — non-matching lines are skipped.

        :param text: Raw stdout.

        :returns: A list of layout names.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        line = raw.strip()
        if "KeyboardLayout Name" not in line:
            continue
        # Format: ``"KeyboardLayout Name" = "U.S.";``
        _, _, after_eq = line.partition("=")
        value = after_eq.strip()
        if value.endswith(";"):
            value = value[:-1].strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if len(value) > 0:
            rtnval.append(value)
    return rtnval


def _parse_systemsetup_timezone(*, text: str) -> str:
    """
        Parse the output of ``systemsetup -gettimezone``.

        ``systemsetup`` emits exactly one line of the form
        ``Time Zone: <iana>``.  The parser strips the prefix and any
        surrounding whitespace.

        :param text: Raw stdout.

        :returns: The IANA timezone string.  Empty string on parse
                  failure.
    """
    if text is None:
        rtnval = ""
        return rtnval
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(_TIMEZONE_PFX):
            rtnval = line[len(_TIMEZONE_PFX):].strip()
            return rtnval
    rtnval = ""
    return rtnval


def _parse_systemsetup_listtimezones(*, text: str) -> list:
    """
        Parse the output of ``systemsetup -listtimezones``.

        ``systemsetup`` emits a header line ``Time Zones:`` followed by
        one indented IANA timezone per line.  The parser strips the
        header (when present) and returns the list of timezones in
        source order.

        :param text: Raw stdout.

        :returns: A list of IANA timezone strings.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("Time Zones:"):
            continue
        rtnval.append(line)
    return rtnval


def _parse_locale_a(*, text: str) -> list:
    """
        Parse ``locale -a`` (BSD form on macOS).

        ``locale -a`` on macOS emits one locale per line.  Empty lines
        are dropped; order is preserved.

        :param text: Raw stdout.

        :returns: A list of locale strings.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        entry = raw.strip()
        if len(entry) == 0:
            continue
        rtnval.append(entry)
    return rtnval


def _parse_keymap_layout_listing(*, text: str) -> list:
    """
        Parse the output of ``ls -1 /System/Library/Keyboard\\ Layouts/``.

        ``ls -1`` emits one filename per line.  The manager strips the
        ``.bundle`` and ``.keylayout`` suffixes Apple uses for installed
        layouts so callers see the user-facing short names.

        :param text: Raw stdout.

        :returns: A list of layout names.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        entry = raw.strip()
        if len(entry) == 0:
            continue
        if entry.endswith(".bundle"):
            entry = entry[: -len(".bundle")]
        elif entry.endswith(".keylayout"):
            entry = entry[: -len(".keylayout")]
        rtnval.append(entry)
    return rtnval


def _derive_country(*, language: str) -> Optional[str]:
    """
        Derive the country code from an ``AppleLocale`` value.

        Examples::

            "en_US" -> "US"
            "fr_FR" -> "FR"
            "en"    -> None

        :param language: The ``AppleLocale`` value.

        :returns: The country code, or ``None``.
    """
    if language is None:
        rtnval: Optional[str] = None
        return rtnval
    stripped = language.strip()
    if len(stripped) == 0:
        rtnval = None
        return rtnval
    if "_" not in stripped:
        rtnval = None
        return rtnval
    _, _, country_text = stripped.partition("_")
    if len(country_text) == 0:
        rtnval = None
        return rtnval
    rtnval = country_text
    return rtnval


def _build_input_source_plist(*, layout_name: str) -> str:
    """
        Render an ``AppleSelectedInputSources`` old-style plist body
        for a single layout name.

        :param layout_name: The Apple layout short name (e.g.
                            ``"U.S."``).

        :returns: The plist body, suitable as the trailing argument to
                  ``defaults write -array``.
    """
    rtnval = (
        "( "
        "{{ "
        "\"InputSourceKind\" = \"Keyboard Layout\"; "
        "\"KeyboardLayout Name\" = \"{layout}\"; "
        "}} "
        ")"
    ).format(layout=layout_name)
    return rtnval


# ----------------------------------------------------------------------
# MacosLocalizationManager
# ----------------------------------------------------------------------


class MacosLocalizationManager(BaseFeatureManager):
    """
        macOS localization manager.  Wraps ``defaults``, ``systemsetup``,
        ``locale``, and ``ls`` through the injected
        :class:`ICommandRunner`.  Reads never elevate; writes elevate
        when ``config.elevated`` is True.
    """

    result_class = MacosLocalizationActionResult
    status_class = MacosLocalizationActionStatus
    feature_name = "localization-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_locale(self) -> MacosLocaleInfo:
        """
            Read the active locale via ``defaults read
            .GlobalPreferences AppleLocale`` plus ``defaults read
            .GlobalPreferences AppleLanguages``.

            :returns: The :class:`MacosLocaleInfo`.
            :raises MacosLocalizationQueryError: When ``defaults read
                                                 AppleLocale`` exits
                                                 non-zero with stderr.
        """
        cfg = self._typed_config()
        locale_command = [
            cfg.defaults_path,
            "read",
            cfg.defaults_domain,
            _APPLE_LOCALE_KEY,
        ]
        locale_run = self._runner.run(list(locale_command), elevated=False)
        if locale_run.exit_code != 0:
            stderr_text = locale_run.stderr or ""
            if len(stderr_text.strip()) > 0:
                raise MacosLocalizationQueryError(
                    command=locale_command,
                    exit_code=locale_run.exit_code,
                    stderr=stderr_text,
                )

        languages_command = [
            cfg.defaults_path,
            "read",
            cfg.defaults_domain,
            _APPLE_LANGUAGES_KEY,
        ]
        languages_run = self._runner.run(
            list(languages_command), elevated=False,
        )
        # Missing AppleLanguages is non-fatal — empty list.
        if languages_run.exit_code != 0:
            languages_text = ""
        else:
            languages_text = languages_run.stdout

        language_value = _parse_apple_locale(text=locale_run.stdout)
        country = _derive_country(language=language_value)
        preferred = _parse_apple_languages(text=languages_text)

        info = MacosLocaleInfo(
            language=language_value,
            country=country,
            encoding=None,
            lc_messages=None,
            lc_time=None,
            lc_numeric=None,
            system_default=locale_run.stdout,
            preferred_languages=preferred,
            raw={
                _APPLE_LOCALE_KEY: language_value,
                _APPLE_LANGUAGES_KEY: ",".join(preferred),
            },
        )
        return info

    def list_supported_locales(self) -> list:
        """
            Enumerate locales installed on the host via ``locale -a``.

            :returns: A list of locale identifiers.
            :raises MacosLocalizationQueryError: When ``locale -a``
                                                 exits non-zero.
        """
        cfg = self._typed_config()
        command = [cfg.locale_tool_path, "-a"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_locale_a(text=run_result.stdout)
        return rtnval

    def get_keymap(self) -> MacosKeymapInfo:
        """
            Read the active keyboard input source via ``defaults read
            com.apple.HIToolbox AppleSelectedInputSources``.

            :returns: The :class:`MacosKeymapInfo`.  When the key has
                      never been written, ``name`` is the empty string.
            :raises MacosLocalizationQueryError: When ``defaults read``
                                                 exits non-zero with
                                                 stderr.
        """
        cfg = self._typed_config()
        command = [
            cfg.defaults_path,
            "read",
            cfg.hitoolbox_domain,
            _INPUT_SOURCES_KEY,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if len(stderr_text.strip()) > 0:
                raise MacosLocalizationQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=stderr_text,
                )

        layouts = _parse_input_sources(text=run_result.stdout)
        if len(layouts) > 0:
            primary = layouts[0]
        else:
            primary = ""

        info = MacosKeymapInfo(
            name=primary,
            layout=primary if len(primary) > 0 else None,
            variant=None,
            model=None,
            options=None,
        )
        return info

    def get_supported_keymaps(self) -> list:
        """
            Enumerate installed keyboard layouts.

            Best-effort: lists ``/System/Library/Keyboard Layouts/``.
            When the listing returns zero rows, falls back to a curated
            bundled list of well-known Apple layout short names.

            :returns: A list of layout short names.
        """
        cfg = self._typed_config()
        command = [_LS, "-1", cfg.keymap_layouts_dir]
        run_result = self._runner.run(list(command), elevated=False)
        parsed: list
        if run_result.exit_code != 0:
            parsed = []
        else:
            parsed = _parse_keymap_layout_listing(text=run_result.stdout)

        if len(parsed) > 0:
            rtnval = parsed
        else:
            rtnval = list(_CURATED_KEYMAPS)
        return rtnval

    def get_timezone(self) -> str:
        """
            Read the active system timezone via ``systemsetup
            -gettimezone``.

            :returns: The IANA timezone string (e.g.
                      ``"America/Los_Angeles"``).  Empty string when
                      ``systemsetup`` fails to parse.
            :raises MacosLocalizationQueryError: When ``systemsetup``
                                                 exits non-zero.
        """
        cfg = self._typed_config()
        command = [cfg.systemsetup_path, "-gettimezone"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_systemsetup_timezone(text=run_result.stdout)
        return rtnval

    def get_supported_timezones(self) -> list:
        """
            Enumerate IANA timezones via ``systemsetup
            -listtimezones``.

            :returns: A list of IANA timezone strings in source order.
            :raises MacosLocalizationQueryError: When ``systemsetup``
                                                 exits non-zero.
        """
        cfg = self._typed_config()
        command = [cfg.systemsetup_path, "-listtimezones"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_systemsetup_listtimezones(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_locale(
        self,
        locale: str,
        *,
        preferred_language: Optional[str] = None,
    ) -> MacosLocalizationActionResult:
        """
            Set the system locale via ``defaults write
            .GlobalPreferences AppleLocale '<value>'`` (and optionally
            ``AppleLanguages``).

            Idempotent: when the parsed ``AppleLocale`` already equals
            the requested value AND (when supplied)
            ``preferred_language`` already heads the
            ``AppleLanguages`` array, returns ``SKIPPED``.

            :param locale: The desired ``AppleLocale`` value (e.g.
                           ``"en_US"``).
            :param preferred_language: Optional BCP-47 language tag
                                       (e.g. ``"en-US"``).  When
                                       supplied, the manager
                                       additionally writes a
                                       single-element
                                       ``AppleLanguages`` array.

            :returns: The :class:`MacosLocalizationActionResult`.
        """
        cfg = self._typed_config()
        write_command = [
            cfg.defaults_path,
            "write",
            cfg.defaults_domain,
            _APPLE_LOCALE_KEY,
            locale,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLocalizationActionStatus.DRY_RUN,
                setting="locale",
                detail=locale,
                command=write_command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_locale()
            previous_value = current.language
            already_matches = current.language == locale
            if already_matches and preferred_language is not None:
                if (
                    len(current.preferred_languages) == 0
                    or current.preferred_languages[0] != preferred_language
                ):
                    already_matches = False
            if already_matches:
                skipped = self._build_result(
                    status=MacosLocalizationActionStatus.SKIPPED,
                    setting="locale",
                    detail=locale,
                    previous_value=previous_value,
                    command=write_command,
                )
                return skipped
        except MacosLocalizationQueryError:
            previous_value = None

        primary_result = self._dispatch_localization_write(
            command=write_command,
            setting="locale",
            requested=locale,
            previous_value=previous_value,
        )
        if primary_result.status != MacosLocalizationActionStatus.SUCCESS:
            return primary_result

        if preferred_language is None:
            return primary_result

        languages_command = [
            cfg.defaults_path,
            "write",
            cfg.defaults_domain,
            _APPLE_LANGUAGES_KEY,
            "-array",
            preferred_language,
        ]
        languages_result = self._dispatch_localization_write(
            command=languages_command,
            setting="locale",
            requested=preferred_language,
            previous_value=previous_value,
        )
        return languages_result

    def set_keymap(self, keymap: str) -> MacosLocalizationActionResult:
        """
            Set the keyboard input source via ``defaults write
            com.apple.HIToolbox AppleSelectedInputSources '<plist>'``.

            macOS uses Input Sources rather than a console-style
            keymap; this manager accepts the well-known short names
            (``"U.S."``, ``"British"``, etc.) and renders the
            corresponding old-style plist payload.  Persistent on the
            current user only; per-user defaults to root-aware
            wrapping is the caller's responsibility (via
            ``config.elevated``).

            Idempotent: when the parsed primary input-source name
            equals ``keymap``, returns ``SKIPPED``.

            :param keymap: The Apple layout short name.

            :returns: The :class:`MacosLocalizationActionResult`.
        """
        cfg = self._typed_config()
        plist_body = _build_input_source_plist(layout_name=keymap)
        command = [
            cfg.defaults_path,
            "write",
            cfg.hitoolbox_domain,
            _INPUT_SOURCES_KEY,
            "-array",
            plist_body,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLocalizationActionStatus.DRY_RUN,
                setting="keymap",
                detail=keymap,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_keymap()
            previous_value = current.name
            if current.name == keymap:
                skipped = self._build_result(
                    status=MacosLocalizationActionStatus.SKIPPED,
                    setting="keymap",
                    detail=keymap,
                    previous_value=previous_value,
                    command=command,
                )
                return skipped
        except MacosLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="keymap",
            requested=keymap,
            previous_value=previous_value,
        )
        return rtnval

    def set_timezone(self, timezone: str) -> MacosLocalizationActionResult:
        """
            Set the system timezone via ``systemsetup -settimezone
            <iana>``.

            ``systemsetup`` requires root; the manager honours
            ``config.elevated`` on the dispatch step and never elevates
            on the probe step.

            Idempotent: when the parsed timezone matches the requested
            value, returns ``SKIPPED``.

            :param timezone: The desired IANA timezone string.

            :returns: The :class:`MacosLocalizationActionResult`.
        """
        cfg = self._typed_config()
        command = [cfg.systemsetup_path, "-settimezone", timezone]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosLocalizationActionStatus.DRY_RUN,
                setting="timezone",
                detail=timezone,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            previous_value = self.get_timezone()
            if previous_value == timezone:
                skipped = self._build_result(
                    status=MacosLocalizationActionStatus.SKIPPED,
                    setting="timezone",
                    detail=timezone,
                    previous_value=previous_value,
                    command=command,
                )
                return skipped
        except MacosLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="timezone",
            requested=timezone,
            previous_value=previous_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosLocalizationManagerConfig:
        """
            Return the manager's config typed.

            :returns: The typed configuration.
        """
        if isinstance(self._config, MacosLocalizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosLocalizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _dispatch_localization_write(
        self,
        *,
        command: list,
        setting: str,
        requested: str,
        previous_value: Optional[str],
    ) -> MacosLocalizationActionResult:
        """
            Dispatch a mutating localization command.

            :param command: The argv list.
            :param setting: One of ``"locale"``, ``"keymap"``,
                            ``"timezone"``.
            :param requested: The requested value.
            :param previous_value: The previously-read value, if any.

            :returns: The :class:`MacosLocalizationActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosLocalizationActionStatus.SUCCESS
        else:
            mapped_status = MacosLocalizationActionStatus.FAILED

        rtnval = MacosLocalizationActionResult(
            status=mapped_status,
            detail=requested,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=False,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosLocalizationActionStatus,
        setting: Optional[str],
        detail: Optional[str],
        command: list,
        previous_value: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        requires_reboot: bool = False,
    ) -> MacosLocalizationActionResult:
        """
            Construct a :class:`MacosLocalizationActionResult` without
            invoking the runner.

            :returns: The :class:`MacosLocalizationActionResult`.
        """
        rtnval = MacosLocalizationActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=requires_reboot,
        )
        return rtnval
