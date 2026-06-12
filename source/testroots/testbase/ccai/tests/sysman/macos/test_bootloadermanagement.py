"""
    Tests for :class:`MacosBootloaderManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live ``bless`` / ``nvram`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.bootloadermanagement import (
    MacosBootEntryKind,
    MacosBootEntrySpec,
    MacosBootloaderActionStatus,
    MacosBootloaderManager,
    MacosBootloaderManagerConfig,
    MacosBootloaderQueryError,
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

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
    default_volume: Optional[str] = None,
) -> MacosBootloaderManager:
    """
        Construct a manager wired to ``runner``.
    """
    config = MacosBootloaderManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        default_volume=default_volume,
    )
    rtnval = MacosBootloaderManager(runner=runner, config=config)
    return rtnval


_BLESS_INFO_VERBOSE = """\
finderinfo[0]:    104  =>  blessed System folder is /System
finderinfo[5]:    104  =>  OS X blessed folder is /System
64-bit VSDB volume id:  0xC0FFEE
current boot path:        /Volumes/Macintosh HD
efi boot device:          /Volumes/Macintosh HD
"""

_NVRAM_BOOT_ARGS = "boot-args   -v debug=0x144\n"


# ----------------------------------------------------------------------
# list_entries
# ----------------------------------------------------------------------


def test_list_entries_parses_bless_output():
    """
        ``list_entries`` parses ``bless --info --verbose`` and the
        ``nvram boot-args`` output into one entry for the active
        boot volume.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--verbose")] = _ok(
        stdout=_BLESS_INFO_VERBOSE,
    )
    runner.results[("nvram", "boot-args")] = _ok(stdout=_NVRAM_BOOT_ARGS)
    manager = _make_manager(runner=runner)

    entries = manager.list_entries()

    testbase.assert_equal(len(entries), 1)
    testbase.assert_equal(entries[0].id, "/Volumes/Macintosh HD")
    testbase.assert_equal(entries[0].is_default, True)
    testbase.assert_equal(entries[0].cmdline, "-v debug=0x144")


# ----------------------------------------------------------------------
# get_default_entry
# ----------------------------------------------------------------------


def test_get_default_entry_resolves_id():
    """
        ``get_default_entry`` resolves the active boot volume via
        ``bless --info --getBoot``.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Macintosh HD\n",
    )
    runner.results[("nvram", "boot-args")] = _ok(stdout=_NVRAM_BOOT_ARGS)
    manager = _make_manager(runner=runner)

    default_entry = manager.get_default_entry()

    testbase.assert_equal(default_entry.id, "/Volumes/Macintosh HD")
    testbase.assert_equal(default_entry.is_default, True)


def test_get_default_entry_raises_when_unresolvable():
    """
        ``get_default_entry`` raises :class:`MacosBootloaderQueryError`
        when neither bless form succeeds.
    """
    runner = _MockCommandRunner()
    runner.default_result = _fail(stderr="bless: not found", exit_code=127)
    manager = _make_manager(runner=runner)

    raised = False
    try:
        manager.get_default_entry()
    except MacosBootloaderQueryError:
        raised = True

    testbase.assert_equal(raised, True)


# ----------------------------------------------------------------------
# set_default_entry
# ----------------------------------------------------------------------


def test_set_default_entry_invokes_bless():
    """
        ``set_default_entry`` runs ``bless --mount <path> --setBoot``
        when the volume is not already the boot device.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Other\n",
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id="/Volumes/Macintosh HD")

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["bless", "--mount", "/Volumes/Macintosh HD", "--setBoot"],
    )
    testbase.assert_equal(last_call["elevated"], True)
    testbase.assert_equal(result.requires_reboot, True)


def test_set_default_entry_skipped_when_already_default():
    """
        ``set_default_entry`` returns SKIPPED when the volume is
        already the active boot device.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Macintosh HD\n",
    )
    manager = _make_manager(runner=runner)

    result = manager.set_default_entry(entry_id="/Volumes/Macintosh HD")

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SKIPPED)
    setboot_calls = [
        call for call in runner.calls
        if "--setBoot" in call["args"]
    ]
    testbase.assert_equal(len(setboot_calls), 0)


# ----------------------------------------------------------------------
# add_entry
# ----------------------------------------------------------------------


def test_add_entry_bless_volume_invokes_bless():
    """
        ``add_entry`` runs ``bless --mount <path>`` for a
        BLESS_VOLUME spec.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Other\n",
    )
    manager = _make_manager(runner=runner, elevated=True)

    spec = MacosBootEntrySpec(
        entry_kind=MacosBootEntryKind.BLESS_VOLUME,
        volume_path="/Volumes/Recovery",
        name="Recovery Volume",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["bless", "--mount", "/Volumes/Recovery"],
    )


def test_add_entry_skipped_when_present():
    """
        ``add_entry`` returns SKIPPED when the volume is already the
        active boot device.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Macintosh HD\n",
    )
    manager = _make_manager(runner=runner)

    spec = MacosBootEntrySpec(
        entry_kind=MacosBootEntryKind.BLESS_VOLUME,
        volume_path="/Volumes/Macintosh HD",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SKIPPED)


def test_add_entry_dry_run_short_circuits():
    """
        ``add_entry`` returns DRY_RUN without invoking the runner
        when ``config.dry_run`` is True.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)

    spec = MacosBootEntrySpec(
        entry_kind=MacosBootEntryKind.BLESS_VOLUME,
        volume_path="/Volumes/Recovery",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)


def test_add_entry_unsupported_kind_returns_failed():
    """
        ``add_entry`` returns FAILED with stderr token
        ``unsupported_entry_kind`` when given a non-BLESS_VOLUME
        spec.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    spec = MacosBootEntrySpec(
        entry_kind=MacosBootEntryKind.KERNEL_ENTRY,
        volume_path="/Volumes/Recovery",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "unsupported_entry_kind")
    testbase.assert_equal(len(runner.calls), 0)


# ----------------------------------------------------------------------
# remove_entry
# ----------------------------------------------------------------------


def test_remove_entry_invokes_unbless():
    """
        ``remove_entry`` runs ``bless --unbless <path>`` when the
        volume is currently blessed.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Recovery\n",
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.remove_entry(entry_id="/Volumes/Recovery")

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["bless", "--unbless", "/Volumes/Recovery"],
    )


def test_remove_entry_skipped_when_absent():
    """
        ``remove_entry`` returns SKIPPED when the volume is not the
        active boot device (the only blessed-volume signal macOS
        exposes).
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Macintosh HD\n",
    )
    manager = _make_manager(runner=runner)

    result = manager.remove_entry(entry_id="/Volumes/Recovery")

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SKIPPED)


# ----------------------------------------------------------------------
# update_bootloader_config / regenerate_config
# ----------------------------------------------------------------------


def test_update_bootloader_config_returns_noop_success():
    """
        ``update_bootloader_config`` returns SUCCESS as a no-op
        without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.update_bootloader_config()

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        "OS-managed" in (result.message or ""), True,
    )


def test_regenerate_config_returns_noop_success():
    """
        ``regenerate_config`` returns SUCCESS as a no-op without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.regenerate_config()

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 0)


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Other\n",
    )
    manager = _make_manager(runner=runner, elevated=True)

    manager.set_default_entry(entry_id="/Volumes/Macintosh HD")

    setboot_calls = [
        call for call in runner.calls
        if "--setBoot" in call["args"]
    ]
    testbase.assert_equal(len(setboot_calls), 1)
    testbase.assert_equal(setboot_calls[0]["elevated"], True)


def test_reads_never_elevate():
    """
        Reads (``bless --info``, ``nvram boot-args``) never pass
        ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--verbose")] = _ok(
        stdout=_BLESS_INFO_VERBOSE,
    )
    runner.results[("nvram", "boot-args")] = _ok(stdout=_NVRAM_BOOT_ARGS)
    manager = _make_manager(runner=runner, elevated=True)

    manager.list_entries()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)


# ----------------------------------------------------------------------
# Failed-command surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failed ``bless --setBoot`` invocation surfaces the runner
        stderr on a FAILED result.
    """
    runner = _MockCommandRunner()
    runner.results[("bless", "--info", "--getBoot")] = _ok(
        stdout="/Volumes/Other\n",
    )
    runner.results[
        ("bless", "--mount", "/Volumes/Macintosh HD", "--setBoot")
    ] = _fail(stderr="bless: insufficient privileges", exit_code=13)
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id="/Volumes/Macintosh HD")

    testbase.assert_equal(result.status, MacosBootloaderActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 13)
    testbase.assert_equal(
        "bless: insufficient privileges" in (result.stderr or ""),
        True,
    )
