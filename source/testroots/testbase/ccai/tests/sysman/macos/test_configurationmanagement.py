"""
    Tests for :class:`MacosConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult`` values keyed by
    argv tuple.  No live ``defaults`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.configurationmanagement import (
    MacosConfigActionStatus,
    MacosConfigManagerConfig,
    MacosConfigQueryError,
    MacosConfigurationManager,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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


# ----------------------------------------------------------------------
# Routing — plist vs plain text
# ----------------------------------------------------------------------


def test_routing_plist_extension_dispatches_defaults():
    """
        A path ending in ``.plist`` is routed through ``defaults
        read``.
    """
    plist_path = "/Library/Preferences/com.example.foo.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "MyKey")] = RunResult(
        exit_code=0, stdout="MyValue\n", stderr="",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.read_setting(file=plist_path, key="MyKey")

    testbase.assert_equal(result.value, "MyValue")
    testbase.assert_equal(runner.calls[-1]["args"][0], "defaults")
    return


def test_routing_library_preferences_path_dispatches_defaults():
    """
        A path under ``/Library/Preferences/`` (no extension) is also
        routed through ``defaults``.
    """
    plist_path = "/Library/Preferences/com.example.bar"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "MyKey")] = RunResult(
        exit_code=0, stdout="MyValue\n", stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.read_setting(file=plist_path, key="MyKey")

    testbase.assert_equal(result.value, "MyValue")
    testbase.assert_equal(runner.calls[-1]["args"][0], "defaults")
    return


def test_routing_plain_text_uses_cat():
    """
        A non-plist source is routed through ``cat`` for reads.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0,
        stdout='KEY1="value1"\nKEY2="value2"\n',
        stderr="",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.read_setting(file="/etc/example.conf", key="KEY1")

    testbase.assert_equal(result.value, "value1")
    testbase.assert_equal(runner.calls[-1]["args"][0], "cat")
    return


# ----------------------------------------------------------------------
# read_setting / list_settings
# ----------------------------------------------------------------------


def test_read_setting_plist_runs_defaults_read():
    """
        read_setting on a plist source dispatches
        ``defaults read <path> <key>``.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=0, stdout="Dark\n", stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.read_setting(file=plist_path, key="Theme")

    testbase.assert_equal(result.key, "Theme")
    testbase.assert_equal(result.value, "Dark")
    testbase.assert_equal(result.file, plist_path)
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_read_setting_missing_key_raises():
    """
        read_setting raises :class:`MacosConfigQueryError` when the
        plain-text file does not contain the key.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0, stdout='OTHER="value"\n', stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    raised = False
    try:
        manager.read_setting(file="/etc/example.conf", key="MISSING")
    except MacosConfigQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_list_settings_plist_parses_defaults_read():
    """
        list_settings on a plist source flattens ``defaults read``
        output into a dict of top-level entries.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    body = (
        "{\n"
        '    "Theme" = "Dark";\n'
        "    Count = 42;\n"
        "    Nested = {\n"
        '        "inner" = "ignored";\n'
        "    };\n"
        "}\n"
    )
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path)] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.list_settings(file=plist_path)

    testbase.assert_equal(result["Theme"], "Dark")
    testbase.assert_equal(result["Count"], "42")
    # Nested dicts are skipped intentionally.
    testbase.assert_true("Nested" not in result)
    return


def test_list_settings_text_preserves_keys():
    """
        list_settings on a plain text source returns every
        shell-style entry.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0,
        stdout='# header\nKEY1="value1"\nKEY2="value2"\n',
        stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.list_settings(file="/etc/example.conf")

    testbase.assert_equal(result["KEY1"], "value1")
    testbase.assert_equal(result["KEY2"], "value2")
    return


# ----------------------------------------------------------------------
# write_setting — plist
# ----------------------------------------------------------------------


def test_write_setting_plist_infers_string_type():
    """
        write_setting on a plist with a non-numeric value dispatches
        ``defaults write <path> <key> -string <value>``.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=1, stdout="", stderr="key not found",
    )
    runner.results[(
        "defaults", "write", plist_path, "Theme", "-string", "Dark",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file=plist_path, key="Theme", value="Dark",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["defaults", "write", plist_path, "Theme", "-string", "Dark"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_write_setting_plist_infers_int_type():
    """
        write_setting on a plist with an integer-shaped string
        dispatches ``defaults write ... -int <value>``.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Count")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[(
        "defaults", "write", plist_path, "Count", "-int", "42",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=plist_path, key="Count", value="42",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(
        write_calls[0]["args"],
        ["defaults", "write", plist_path, "Count", "-int", "42"],
    )
    return


def test_write_setting_plist_infers_bool_type():
    """
        write_setting on a plist with a "true"/"false" string
        dispatches ``defaults write ... -bool <value>``.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Enabled")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[(
        "defaults", "write", plist_path, "Enabled", "-bool", "true",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=plist_path, key="Enabled", value="true",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(
        write_calls[0]["args"],
        ["defaults", "write", plist_path, "Enabled", "-bool", "true"],
    )
    return


def test_write_setting_plist_skipped_when_value_matches():
    """
        write_setting returns SKIPPED when ``defaults read`` already
        reports the desired value.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=0, stdout="Dark\n", stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=plist_path, key="Theme", value="Dark",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "write"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# write_setting — plain text
# ----------------------------------------------------------------------


def test_write_setting_text_rewrites_content():
    """
        write_setting on a plain text source rewrites the file via
        ``tee`` with the merged content.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0,
        stdout='KEY1="old"\nOTHER="keep"\n',
        stderr="",
    )
    runner.results[("tee", "/etc/example.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file="/etc/example.conf", key="KEY1", value="new",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true('KEY1="new"' in payload)
    testbase.assert_true('OTHER="keep"' in payload)
    return


def test_write_setting_text_skipped_when_value_matches():
    """
        write_setting on a plain text source returns SKIPPED when the
        file already carries the desired pair.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0,
        stdout='KEY1="value1"\n',
        stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.write_setting(
        file="/etc/example.conf", key="KEY1", value="value1",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(tee_calls, [])
    return


def test_write_setting_dry_run_no_runner_call():
    """
        Dry-run short-circuits write_setting without dispatching tee
        or defaults write.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(dry_run=True),
    )
    result = manager.write_setting(
        file="/etc/example.conf", key="KEY1", value="new",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.DRY_RUN)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# remove_setting
# ----------------------------------------------------------------------


def test_remove_setting_plist_runs_defaults_delete():
    """
        remove_setting on a plist source dispatches
        ``defaults delete <path> <key>``.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=0, stdout="Dark\n", stderr="",
    )
    runner.results[(
        "defaults", "delete", plist_path, "Theme",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    result = manager.remove_setting(file=plist_path, key="Theme")

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["defaults", "delete"]
    ]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(delete_calls[0]["elevated"], True)
    return


def test_remove_setting_plist_skipped_when_key_absent():
    """
        remove_setting on a plist source returns SKIPPED when the
        idempotency probe finds no current value.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Missing")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.remove_setting(file=plist_path, key="Missing")

    testbase.assert_equal(result.status, MacosConfigActionStatus.SKIPPED)
    return


def test_remove_setting_text_rewrites_without_key():
    """
        remove_setting on a plain text source rewrites the file with
        the matching line removed.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0,
        stdout='KEY1="v1"\nKEY2="v2"\n',
        stderr="",
    )
    runner.results[("tee", "/etc/example.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.remove_setting(file="/etc/example.conf", key="KEY1")

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    payload = tee_calls[0]["stdin"]
    testbase.assert_true("KEY1" not in payload)
    testbase.assert_true('KEY2="v2"' in payload)
    return


# ----------------------------------------------------------------------
# reload_configuration / backup / restore
# ----------------------------------------------------------------------


def test_reload_configuration_runs_launchctl_kickstart():
    """
        reload_configuration dispatches
        ``launchctl kickstart -k system/<service>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    result = manager.reload_configuration(service="com.example.app")

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"],
        ["launchctl", "kickstart", "-k", "system/com.example.app"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


def test_backup_copies_with_timestamp():
    """
        backup_configuration dispatches ``cp -p`` to a sibling
        timestamped path and surfaces ``backup_path``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.backup_configuration(file="/etc/example.conf")

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    cp_args = runner.calls[-1]["args"]
    testbase.assert_equal(cp_args[0], "cp")
    testbase.assert_equal(cp_args[1], "-p")
    testbase.assert_equal(cp_args[2], "/etc/example.conf")
    testbase.assert_true(cp_args[3].startswith("/etc/example.conf."))
    testbase.assert_true(cp_args[3].endswith(".bak"))
    testbase.assert_equal(result.backup_path, cp_args[3])
    return


def test_restore_skipped_when_byte_match():
    """
        restore_configuration returns SKIPPED when the target already
        byte-matches the backup.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cmp", "-s", "/var/backups/example.bak", "/etc/example.conf",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.restore_configuration(
        backup_path="/var/backups/example.bak",
        target_path="/etc/example.conf",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SKIPPED)
    cp_calls = [c for c in runner.calls if c["args"][:1] == ["cp"]]
    testbase.assert_equal(cp_calls, [])
    return


def test_restore_dispatches_cp_when_differs():
    """
        restore_configuration dispatches ``cp -p`` when ``cmp``
        reports the files differ.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cmp", "-s", "/var/backups/example.bak", "/etc/example.conf",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(
        "cp", "-p", "/var/backups/example.bak", "/etc/example.conf",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    result = manager.restore_configuration(
        backup_path="/var/backups/example.bak",
        target_path="/etc/example.conf",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.SUCCESS)
    cp_calls = [c for c in runner.calls if c["args"][:1] == ["cp"]]
    testbase.assert_equal(len(cp_calls), 1)
    testbase.assert_equal(cp_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Elevation / error surfaces
# ----------------------------------------------------------------------


def test_reads_never_elevate_even_when_config_elevated():
    """
        Reads never elevate even when config.elevated is True.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=0, stdout="Dark\n", stderr="",
    )
    runner.results[("defaults", "read", plist_path)] = RunResult(
        exit_code=0, stdout="{\n}\n", stderr="",
    )
    runner.results[("cat", "/etc/example.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(elevated=True),
    )
    manager.read_setting(file=plist_path, key="Theme")
    manager.list_settings(file=plist_path)
    manager.list_settings(file="/etc/example.conf")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on ``defaults write`` maps to FAILED with the
        captured stderr / exit_code / command.
    """
    plist_path = "/Library/Preferences/com.example.app.plist"
    runner = _MockCommandRunner()
    runner.results[("defaults", "read", plist_path, "Theme")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[(
        "defaults", "write", plist_path, "Theme", "-string", "Dark",
    )] = RunResult(
        exit_code=1,
        stdout="",
        stderr="defaults: permission denied",
    )

    manager = MacosConfigurationManager(
        runner=runner,
        config=MacosConfigManagerConfig(),
    )
    result = manager.write_setting(
        file=plist_path, key="Theme", value="Dark",
    )

    testbase.assert_equal(result.status, MacosConfigActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in result.stderr)
    return
