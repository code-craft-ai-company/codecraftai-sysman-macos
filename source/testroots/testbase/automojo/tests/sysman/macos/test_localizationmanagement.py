"""
    Tests for :class:`MacosLocalizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``defaults`` / ``systemsetup`` / ``locale`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.localizationmanagement import (
    MacosLocalizationActionStatus,
    MacosLocalizationManager,
    MacosLocalizationManagerConfig,
    MacosLocalizationQueryError,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _apple_locale_payload() -> str:
    """
        Build a representative ``defaults read .GlobalPreferences
        AppleLocale`` payload.
    """
    rtnval = "en_US\n"
    return rtnval


def _apple_languages_payload() -> str:
    """
        Build a representative ``defaults read .GlobalPreferences
        AppleLanguages`` payload.
    """
    rtnval = (
        "(\n"
        "    \"en-US\",\n"
        "    \"fr-FR\"\n"
        ")\n"
    )
    return rtnval


def _input_sources_payload() -> str:
    """
        Build a representative ``defaults read com.apple.HIToolbox
        AppleSelectedInputSources`` payload.
    """
    rtnval = (
        "(\n"
        "        {\n"
        "        \"InputSourceKind\" = \"Keyboard Layout\";\n"
        "        \"KeyboardLayout ID\" = 0;\n"
        "        \"KeyboardLayout Name\" = \"U.S.\";\n"
        "    }\n"
        ")\n"
    )
    return rtnval


def _systemsetup_gettimezone_payload() -> str:
    """
        Build a representative ``systemsetup -gettimezone`` payload.
    """
    rtnval = "Time Zone: America/Los_Angeles\n"
    return rtnval


def _systemsetup_listtimezones_payload() -> str:
    """
        Build a representative ``systemsetup -listtimezones`` payload.
    """
    rtnval = (
        "Time Zones:\n"
        "Africa/Abidjan\n"
        "America/Los_Angeles\n"
        "America/New_York\n"
        "Asia/Tokyo\n"
        "Europe/London\n"
        "UTC\n"
    )
    return rtnval


def _locale_a_payload() -> str:
    """
        Build a representative ``locale -a`` payload (BSD form).
    """
    rtnval = (
        "C\n"
        "POSIX\n"
        "en_GB.UTF-8\n"
        "en_US.UTF-8\n"
    )
    return rtnval


def _ls_keyboard_layouts_payload() -> str:
    """
        Build a representative ``ls /System/Library/Keyboard Layouts/``
        payload.
    """
    rtnval = (
        "AppleKeyboardLayouts.bundle\n"
        "Roman.bundle\n"
        "Unicode.bundle\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_get_locale_parses_defaults_output():
    """
        get_locale reads AppleLocale + AppleLanguages and parses both.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLocale")
    ] = RunResult(
        exit_code=0, stdout=_apple_locale_payload(), stderr="",
    )
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLanguages")
    ] = RunResult(
        exit_code=0, stdout=_apple_languages_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    info = manager.get_locale()

    testbase.assert_equal(info.language, "en_US")
    testbase.assert_equal(info.country, "US")
    testbase.assert_equal(info.preferred_languages, ["en-US", "fr-FR"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_keymap_parses_defaults_output():
    """
        get_keymap parses ``AppleSelectedInputSources`` and surfaces
        the primary layout name.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("defaults", "read", "com.apple.HIToolbox",
         "AppleSelectedInputSources")
    ] = RunResult(
        exit_code=0, stdout=_input_sources_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    info = manager.get_keymap()

    testbase.assert_equal(info.name, "U.S.")
    testbase.assert_equal(info.layout, "U.S.")
    testbase.assert_equal(info.variant, None)
    testbase.assert_equal(info.model, None)
    return


def test_get_timezone_parses_systemsetup_output():
    """
        get_timezone parses ``systemsetup -gettimezone``.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout=_systemsetup_gettimezone_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    tz = manager.get_timezone()

    testbase.assert_equal(tz, "America/Los_Angeles")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_list_supported_locales_returns_list():
    """
        list_supported_locales parses ``locale -a``.
    """
    runner = _MockCommandRunner()
    runner.results[("locale", "-a")] = RunResult(
        exit_code=0, stdout=_locale_a_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    locales = manager.list_supported_locales()

    testbase.assert_equal("en_US.UTF-8" in locales, True)
    testbase.assert_equal(len(locales), 4)
    return


def test_get_supported_timezones_parses_list():
    """
        get_supported_timezones strips the header line from
        ``systemsetup -listtimezones`` output.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-listtimezones")] = RunResult(
        exit_code=0, stdout=_systemsetup_listtimezones_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    zones = manager.get_supported_timezones()

    testbase.assert_equal("America/Los_Angeles" in zones, True)
    testbase.assert_equal("Time Zones:" in zones, False)
    return


def test_get_supported_keymaps_returns_list_from_listing():
    """
        get_supported_keymaps strips ``.bundle`` suffixes from the
        ``ls`` output.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("ls", "-1", "/System/Library/Keyboard Layouts/")
    ] = RunResult(
        exit_code=0, stdout=_ls_keyboard_layouts_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    keymaps = manager.get_supported_keymaps()

    testbase.assert_equal("AppleKeyboardLayouts" in keymaps, True)
    testbase.assert_equal("Roman" in keymaps, True)
    testbase.assert_equal("Unicode" in keymaps, True)
    # Suffixes stripped:
    testbase.assert_equal("Roman.bundle" in keymaps, False)
    return


def test_get_supported_keymaps_falls_back_to_curated_list():
    """
        get_supported_keymaps falls back to the curated bundled list
        when the directory listing returns zero rows.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("ls", "-1", "/System/Library/Keyboard Layouts/")
    ] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    keymaps = manager.get_supported_keymaps()

    testbase.assert_equal("U.S." in keymaps, True)
    testbase.assert_equal("British" in keymaps, True)
    testbase.assert_equal(len(keymaps) > 5, True)
    return


# ----------------------------------------------------------------------
# Write: set_locale
# ----------------------------------------------------------------------


def test_set_locale_invokes_defaults():
    """
        set_locale dispatches ``defaults write .GlobalPreferences
        AppleLocale '<value>'`` when the current value differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLocale")
    ] = RunResult(exit_code=0, stdout="fr_FR\n", stderr="")
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLanguages")
    ] = RunResult(exit_code=1, stdout="", stderr="does not exist")

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    result = manager.set_locale("en_US")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command,
        ["defaults", "write", "NSGlobalDomain", "AppleLocale", "en_US"],
    )
    testbase.assert_equal(result.setting, "locale")
    testbase.assert_equal(result.previous_value, "fr_FR")
    return


def test_set_locale_skipped_when_match():
    """
        set_locale returns SKIPPED when the current AppleLocale already
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLocale")
    ] = RunResult(exit_code=0, stdout=_apple_locale_payload(), stderr="")
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLanguages")
    ] = RunResult(
        exit_code=0, stdout=_apple_languages_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    result = manager.set_locale("en_US")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_locale_dry_run_short_circuits():
    """
        set_locale with dry_run=True returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_locale("en_US")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["defaults", "write", "NSGlobalDomain", "AppleLocale", "en_US"],
    )
    return


# ----------------------------------------------------------------------
# Write: set_keymap
# ----------------------------------------------------------------------


def test_set_keymap_invokes_defaults():
    """
        set_keymap dispatches ``defaults write com.apple.HIToolbox
        AppleSelectedInputSources -array <plist>`` when the current
        primary layout differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("defaults", "read", "com.apple.HIToolbox",
         "AppleSelectedInputSources")
    ] = RunResult(
        exit_code=0,
        stdout=_input_sources_payload().replace("U.S.", "British"),
        stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    result = manager.set_keymap("U.S.")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(result.setting, "keymap")
    testbase.assert_equal(result.previous_value, "British")
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
        and "AppleSelectedInputSources" in c["args"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    return


def test_set_keymap_skipped_when_match():
    """
        set_keymap returns SKIPPED when the current primary input source
        already matches.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("defaults", "read", "com.apple.HIToolbox",
         "AppleSelectedInputSources")
    ] = RunResult(
        exit_code=0, stdout=_input_sources_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    result = manager.set_keymap("U.S.")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Write: set_timezone
# ----------------------------------------------------------------------


def test_set_timezone_invokes_systemsetup():
    """
        set_timezone dispatches ``systemsetup -settimezone <iana>`` when
        the current timezone differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout="Time Zone: UTC\n", stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command,
        ["systemsetup", "-settimezone", "America/Los_Angeles"],
    )
    testbase.assert_equal(result.setting, "timezone")
    testbase.assert_equal(result.previous_value, "UTC")
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-settimezone"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_timezone_skipped_when_match():
    """
        set_timezone returns SKIPPED when the current timezone already
        matches.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout=_systemsetup_gettimezone_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemsetup", "-settimezone"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_timezone_dry_run_short_circuits():
    """
        set_timezone with dry_run=True returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_timezone("America/New_York")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["systemsetup", "-settimezone", "America/New_York"],
    )
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation, error surface
# ----------------------------------------------------------------------


def test_writes_elevate_per_policy():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate; reads in the same flow stay unelevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLocale")
    ] = RunResult(exit_code=0, stdout="fr_FR\n", stderr="")
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLanguages")
    ] = RunResult(exit_code=1, stdout="", stderr="does not exist")
    runner.results[
        ("defaults", "read", "com.apple.HIToolbox",
         "AppleSelectedInputSources")
    ] = RunResult(exit_code=1, stdout="", stderr="does not exist")
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout="Time Zone: UTC\n", stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(elevated=True),
    )
    manager.set_locale("en_US")
    manager.set_keymap("U.S.")
    manager.set_timezone("America/Los_Angeles")

    for call in runner.calls:
        argv0 = call["args"][0]
        argv1 = call["args"][1] if len(call["args"]) > 1 else ""
        if argv0 == "defaults" and argv1 == "read":
            testbase.assert_equal(call["elevated"], False)
        elif argv0 == "systemsetup" and argv1 == "-gettimezone":
            testbase.assert_equal(call["elevated"], False)
        elif argv0 == "defaults" and argv1 == "write":
            testbase.assert_equal(call["elevated"], True)
        elif argv0 == "systemsetup" and argv1 == "-settimezone":
            testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True`` even when the
        config is configured to elevate.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLocale")
    ] = RunResult(exit_code=0, stdout=_apple_locale_payload(), stderr="")
    runner.results[
        ("defaults", "read", "NSGlobalDomain", "AppleLanguages")
    ] = RunResult(
        exit_code=0, stdout=_apple_languages_payload(), stderr="",
    )
    runner.results[
        ("defaults", "read", "com.apple.HIToolbox",
         "AppleSelectedInputSources")
    ] = RunResult(
        exit_code=0, stdout=_input_sources_payload(), stderr="",
    )
    runner.results[("locale", "-a")] = RunResult(
        exit_code=0, stdout=_locale_a_payload(), stderr="",
    )
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout=_systemsetup_gettimezone_payload(), stderr="",
    )
    runner.results[("systemsetup", "-listtimezones")] = RunResult(
        exit_code=0, stdout=_systemsetup_listtimezones_payload(), stderr="",
    )
    runner.results[
        ("ls", "-1", "/System/Library/Keyboard Layouts/")
    ] = RunResult(
        exit_code=0, stdout=_ls_keyboard_layouts_payload(), stderr="",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(elevated=True),
    )
    manager.get_locale()
    manager.list_supported_locales()
    manager.get_keymap()
    manager.get_supported_keymaps()
    manager.get_timezone()
    manager.get_supported_timezones()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=0, stdout="Time Zone: UTC\n", stderr="",
    )
    runner.results[
        ("systemsetup", "-settimezone", "Bogus/Zone")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="Error: invalid time zone\n",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_timezone("Bogus/Zone")

    testbase.assert_equal(result.status, MacosLocalizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("invalid time zone" in result.stderr, True)
    testbase.assert_equal(
        result.command,
        ["systemsetup", "-settimezone", "Bogus/Zone"],
    )
    return


def test_get_timezone_raises_on_systemsetup_failure():
    """
        get_timezone raises :class:`MacosLocalizationQueryError` when
        ``systemsetup -gettimezone`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("systemsetup", "-gettimezone")] = RunResult(
        exit_code=1, stdout="",
        stderr="must be run as root\n",
    )

    manager = MacosLocalizationManager(
        runner=runner,
        config=MacosLocalizationManagerConfig(),
    )
    raised = False
    try:
        manager.get_timezone()
    except MacosLocalizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
