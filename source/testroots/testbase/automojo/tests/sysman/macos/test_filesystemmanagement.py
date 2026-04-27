"""
    Tests for :class:`MacosFilesystemManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``mount`` / ``umount`` / ``chown`` / ``chmod`` /
    ``stat`` / ``cat`` / ``tee`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.filesystemmanagement import (
    MacosFileInfo,
    MacosFilesystemActionStatus,
    MacosFilesystemManager,
    MacosFilesystemManagerConfig,
    MacosFstabEntry,
    MacosMountInfo,
    _fstab_contains_match,
    _fstab_filter_out_mountpoint,
    _is_numeric_mode,
    _mode_matches,
    _parse_bsd_mount_output,
    _parse_bsd_stat_output,
    _render_fstab_line,
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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_BASIC_MOUNT_OUTPUT = (
    "/dev/disk1s1 on / (apfs, local, journaled)\n"
    "/dev/disk1s2 on /System/Volumes/Data (apfs, local, journaled)\n"
    "map auto_home on /System/Volumes/Data/home (autofs, automounted)\n"
)


def _seed_mount_output(
    *,
    runner: _MockCommandRunner,
    output: str = _BASIC_MOUNT_OUTPUT,
) -> None:
    """
        Seed a ``mount`` (no-args) result.
    """
    runner.results[("mount",)] = RunResult(
        exit_code=0,
        stdout=output,
        stderr="",
    )
    return


def _seed_stat(
    *,
    runner: _MockCommandRunner,
    path: str,
    owner: str = "alice",
    group: str = "staff",
    mode: str = "644",
    size: int = 1024,
    modified: str = "Apr 24 12:34:56 2026",
) -> None:
    """
        Seed a ``stat -f '%Su:%Sg:%Lp:%z:%Sm' <path>`` result.
    """
    output = "{}:{}:{}:{}:{}\n".format(
        owner, group, mode, size, modified,
    )
    runner.results[
        ("stat", "-f", "%Su:%Sg:%Lp:%z:%Sm", path)
    ] = RunResult(
        exit_code=0,
        stdout=output,
        stderr="",
    )
    return


def _seed_stat_missing(
    *,
    runner: _MockCommandRunner,
    path: str,
) -> None:
    """
        Seed a ``stat`` call that reports the path is missing.
    """
    runner.results[
        ("stat", "-f", "%Su:%Sg:%Lp:%z:%Sm", path)
    ] = RunResult(
        exit_code=1,
        stdout="",
        stderr="stat: {}: No such file or directory\n".format(path),
    )
    return


def _seed_fstab(
    *,
    runner: _MockCommandRunner,
    content: str,
    path: str = "/etc/fstab",
) -> None:
    """
        Seed a ``cat <path>`` :class:`RunResult`.
    """
    runner.results[("cat", path)] = RunResult(
        exit_code=0,
        stdout=content,
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# mount / unmount
# ----------------------------------------------------------------------


def test_mount_constructs_argv_with_optional_options():
    """
        mount dispatches ``mount -t <fstype> -o <options> <device>
        <mountpoint>`` when options are provided.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_mount_output(runner=runner)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.mount(
        device="/dev/disk2s1",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="rw,nosuid",
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    expected = [
        "mount", "-t", "apfs",
        "-o", "rw,nosuid",
        "/dev/disk2s1", "/Volumes/data",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_mount_skipped_when_already_mounted():
    """
        mount returns SKIPPED when the BSD ``mount`` output reports
        the device is already mounted at the requested mountpoint.
    """
    output = (
        "/dev/disk2s1 on /Volumes/data (apfs, local, journaled)\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_mount_output(runner=runner, output=output)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.mount(
        device="/dev/disk2s1",
        mountpoint="/Volumes/data",
        fstype="apfs",
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    mount_writes = [
        c for c in runner.calls
        if len(c["args"]) > 1 and c["args"][0] == "mount"
        and c["args"][1] == "-t"
    ]
    testbase.assert_equal(mount_writes, [])
    return


def test_unmount_invokes_umount():
    """
        unmount dispatches ``umount <mountpoint>`` when the mountpoint
        is currently mounted.
    """
    output = (
        "/dev/disk2s1 on /Volumes/data (apfs, local, journaled)\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_mount_output(runner=runner, output=output)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.unmount(mountpoint="/Volumes/data")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    expected = ["umount", "/Volumes/data"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_unmount_skipped_when_not_mounted():
    """
        unmount returns SKIPPED when the mountpoint is not currently
        mounted.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_mount_output(runner=runner)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.unmount(mountpoint="/Volumes/missing")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    umount_calls = [
        c for c in runner.calls if c["args"][:1] == ["umount"]
    ]
    testbase.assert_equal(umount_calls, [])
    return


# ----------------------------------------------------------------------
# fstab
# ----------------------------------------------------------------------


def test_add_fstab_entry_appends_via_tee():
    """
        add_fstab_entry pipes the rendered line into ``tee -a
        <fstab_path>`` when the file does not already contain a match.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_fstab(runner=runner, content="# comment\n")

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    spec = MacosFstabEntry(
        device="UUID=AAAA-BBBB",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="rw",
    )
    result = manager.add_fstab_entry(spec=spec)

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)

    write_call = runner.calls[-1]
    testbase.assert_equal(
        write_call["args"], ["tee", "-a", "/etc/fstab"],
    )
    expected_stdin = "UUID=AAAA-BBBB\t/Volumes/data\tapfs\trw\t0\t0\n"
    testbase.assert_equal(write_call["stdin"], expected_stdin)
    return


def test_add_fstab_entry_skipped_when_match_present():
    """
        add_fstab_entry returns SKIPPED when the file already contains
        an exact match.
    """
    fstab_content = (
        "# /etc/fstab\n"
        "UUID=AAAA-BBBB\t/Volumes/data\tapfs\trw\t0\t0\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_fstab(runner=runner, content=fstab_content)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    spec = MacosFstabEntry(
        device="UUID=AAAA-BBBB",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="rw",
    )
    result = manager.add_fstab_entry(spec=spec)

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


def test_remove_fstab_entry_filters_lines_via_tee():
    """
        remove_fstab_entry rewrites ``/etc/fstab`` via ``tee
        <fstab_path>`` with the matching line filtered out.
    """
    fstab_content = (
        "# /etc/fstab\n"
        "UUID=AAAA-BBBB\t/Volumes/data\tapfs\trw\t0\t0\n"
        "UUID=CCCC-DDDD\t/Volumes/extra\tapfs\trw\t0\t0\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_fstab(runner=runner, content=fstab_content)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.remove_fstab_entry(mountpoint="/Volumes/data")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    testbase.assert_equal(
        write_call["args"], ["tee", "/etc/fstab"],
    )
    written = write_call["stdin"]
    testbase.assert_true("/Volumes/data" not in written)
    testbase.assert_true("/Volumes/extra" in written)
    testbase.assert_true("# /etc/fstab" in written)
    return


def test_remove_fstab_entry_skipped_when_no_match():
    """
        remove_fstab_entry returns SKIPPED when no line targets the
        mountpoint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_fstab(runner=runner, content="# nothing\n")

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.remove_fstab_entry(mountpoint="/Volumes/missing")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# chown / chmod
# ----------------------------------------------------------------------


def test_chown_skipped_when_owner_and_group_match():
    """
        chown returns SKIPPED (non-recursive) when ``stat`` already
        reports the desired owner and group.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_stat(
        runner=runner,
        path="/tmp/example",
        owner="alice", group="staff",
    )

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.chown(
        path="/tmp/example",
        user="alice",
        group="staff",
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    chown_calls = [
        c for c in runner.calls if c["args"][:1] == ["chown"]
    ]
    testbase.assert_equal(chown_calls, [])
    return


def test_chown_dispatches_user_group_token():
    """
        chown emits the ``user:group`` token expected by BSD chown.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_stat(
        runner=runner,
        path="/tmp/example",
        owner="root", group="wheel",
    )

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.chown(
        path="/tmp/example", user="alice", group="staff",
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    expected = ["chown", "alice:staff", "/tmp/example"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_chown_recursive_emits_minus_r_and_skips_idempotency():
    """
        chown(recursive=True) dispatches ``chown -R`` unconditionally
        without consulting ``stat`` for the parent path.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.chown(
        path="/tmp/example",
        user="alice",
        group="staff",
        recursive=True,
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    expected = ["chown", "-R", "alice:staff", "/tmp/example"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    # No stat call because recursive bypasses idempotency.
    stat_calls = [
        c for c in runner.calls if c["args"][:1] == ["stat"]
    ]
    testbase.assert_equal(stat_calls, [])
    return


def test_chmod_skipped_when_mode_matches():
    """
        chmod returns SKIPPED when the requested mode matches the
        current mode.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_stat(
        runner=runner, path="/tmp/example", mode="644",
    )

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.chmod(path="/tmp/example", mode="0644")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SKIPPED)
    chmod_calls = [
        c for c in runner.calls if c["args"][:1] == ["chmod"]
    ]
    testbase.assert_equal(chmod_calls, [])
    return


def test_chmod_dispatches_when_mode_differs():
    """
        chmod dispatches ``chmod <mode> <path>`` when the current mode
        differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_stat(runner=runner, path="/tmp/example", mode="600")

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    result = manager.chmod(path="/tmp/example", mode="0644")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.SUCCESS)
    expected = ["chmod", "0644", "/tmp/example"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# Read verbs
# ----------------------------------------------------------------------


def test_get_mounts_parses_bsd_mount_output():
    """
        get_mounts parses ``<device> on <mountpoint> (<fstype>,
        <options>)`` rows.
    """
    runner = _MockCommandRunner()
    _seed_mount_output(runner=runner)

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    mounts = manager.get_mounts()

    testbase.assert_equal(len(mounts), 3)
    testbase.assert_equal(mounts[0].device, "/dev/disk1s1")
    testbase.assert_equal(mounts[0].mountpoint, "/")
    testbase.assert_equal(mounts[0].fstype, "apfs")
    testbase.assert_equal(mounts[0].options, "local, journaled")

    testbase.assert_equal(mounts[1].mountpoint, "/System/Volumes/Data")

    testbase.assert_equal(mounts[2].device, "map auto_home")
    testbase.assert_equal(mounts[2].fstype, "autofs")
    return


def test_stat_parses_bsd_format_output():
    """
        stat parses ``%Su:%Sg:%Lp:%z:%Sm`` into a
        :class:`MacosFileInfo`.
    """
    runner = _MockCommandRunner()
    _seed_stat(
        runner=runner,
        path="/tmp/example",
        owner="alice",
        group="staff",
        mode="755",
        size=4096,
        modified="Apr 24 12:34:56 2026",
    )

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    info = manager.stat(path="/tmp/example")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.path, "/tmp/example")
    testbase.assert_equal(info.owner, "alice")
    testbase.assert_equal(info.group, "staff")
    testbase.assert_equal(info.mode, "755")
    testbase.assert_equal(info.size, 4096)
    testbase.assert_equal(info.modified, "Apr 24 12:34:56 2026")
    return


def test_stat_returns_none_for_missing_path():
    """
        stat returns ``None`` when the runner reports a non-zero exit
        code.
    """
    runner = _MockCommandRunner()
    _seed_stat_missing(runner=runner, path="/tmp/missing")

    manager = MacosFilesystemManager(
        runner=runner, config=MacosFilesystemManagerConfig(),
    )

    info = manager.stat(path="/tmp/missing")
    testbase.assert_equal(info, None)
    return


# ----------------------------------------------------------------------
# Dry-run + elevation policy
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_mount():
    """
        Dry-run short-circuits mount without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosFilesystemManager(
        runner=runner,
        config=MacosFilesystemManagerConfig(dry_run=True),
    )

    result = manager.mount(
        device="/dev/disk2s1",
        mountpoint="/Volumes/data",
        fstype="apfs",
    )

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    expected = ["mount", "-t", "apfs", "/dev/disk2s1", "/Volumes/data"]
    testbase.assert_equal(result.command, expected)
    return


def test_dry_run_short_circuits_chmod():
    """
        Dry-run short-circuits chmod without invoking the runner or
        the stat probe.
    """
    runner = _MockCommandRunner()
    manager = MacosFilesystemManager(
        runner=runner,
        config=MacosFilesystemManagerConfig(dry_run=True),
    )

    result = manager.chmod(path="/tmp/example", mode="0644")

    testbase.assert_equal(result.status, MacosFilesystemActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_writes_pass_elevated_when_configured():
    """
        Writes pass ``elevated=True`` to the runner when the config
        elevates.  Reads (mount enumeration, stat probes) do NOT.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_mount_output(runner=runner)

    manager = MacosFilesystemManager(
        runner=runner,
        config=MacosFilesystemManagerConfig(elevated=True),
    )

    manager.mount(
        device="/dev/disk2s1",
        mountpoint="/Volumes/data",
        fstype="apfs",
    )

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["mount", "-t"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)

    read_calls = [
        c for c in runner.calls
        if c["args"] == ["mount"]
    ]
    testbase.assert_true(len(read_calls) >= 1)
    for call in read_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_stat_call_never_elevates():
    """
        ``stat`` reads never pass ``elevated=True`` even when the
        config elevates writes.
    """
    runner = _MockCommandRunner()
    _seed_stat(runner=runner, path="/tmp/example")

    manager = MacosFilesystemManager(
        runner=runner,
        config=MacosFilesystemManagerConfig(elevated=True),
    )

    manager.stat(path="/tmp/example")

    stat_calls = [
        c for c in runner.calls if c["args"][:1] == ["stat"]
    ]
    testbase.assert_equal(len(stat_calls), 1)
    testbase.assert_equal(stat_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Module-level parser unit tests
# ----------------------------------------------------------------------


def test_parse_bsd_mount_output_handles_complex_options():
    """
        ``_parse_bsd_mount_output`` returns one row per parseable line
        with comma-separated attributes split into fstype + options.
    """
    output = (
        "/dev/disk1s1 on / (apfs, local, read-only, journaled, sealed)\n"
    )
    parsed = _parse_bsd_mount_output(output=output)
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0].fstype, "apfs")
    testbase.assert_equal(
        parsed[0].options, "local, read-only, journaled, sealed",
    )
    return


def test_parse_bsd_mount_output_anchors_on_last_on_token():
    """
        ``_parse_bsd_mount_output`` anchors on the LAST ``" on "`` so
        device names that happen to contain ``" on "`` literally still
        parse correctly.

        ``//user@srv on path/share on /mnt (smbfs)`` is exotic but
        valid for SMB mounts — the device name itself contains the
        substring.
    """
    output = "//user@srv on path/share on /mnt (smbfs)\n"
    parsed = _parse_bsd_mount_output(output=output)
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0].device, "//user@srv on path/share")
    testbase.assert_equal(parsed[0].mountpoint, "/mnt")
    testbase.assert_equal(parsed[0].fstype, "smbfs")
    return


def test_parse_bsd_stat_output_preserves_modification_string():
    """
        ``_parse_bsd_stat_output`` splits with ``maxsplit=4`` so the
        modification field's embedded colons survive.
    """
    output = "alice:staff:644:512:Apr 24 12:34:56 2026\n"
    parsed = _parse_bsd_stat_output(output=output, path="/tmp/x")
    testbase.assert_true(parsed is not None)
    testbase.assert_equal(parsed.owner, "alice")
    testbase.assert_equal(parsed.group, "staff")
    testbase.assert_equal(parsed.mode, "644")
    testbase.assert_equal(parsed.size, 512)
    testbase.assert_equal(parsed.modified, "Apr 24 12:34:56 2026")
    return


def test_render_fstab_line_uses_tab_separators():
    """
        ``_render_fstab_line`` produces a tab-separated, newline-
        terminated row.
    """
    entry = MacosFstabEntry(
        device="UUID=ABC",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="rw",
        dump=0,
        pass_=2,
    )
    rendered = _render_fstab_line(entry=entry)
    testbase.assert_equal(
        rendered, "UUID=ABC\t/Volumes/data\tapfs\trw\t0\t2\n",
    )
    return


def test_fstab_contains_match_ignores_comments():
    """
        ``_fstab_contains_match`` skips comment lines and short
        records.
    """
    content = (
        "# UUID=ABC /Volumes/data apfs rw 0 0\n"
        "UUID=ABC\t/Volumes/data\tapfs\trw\t0\t0\n"
    )
    spec = MacosFstabEntry(
        device="UUID=ABC",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="rw",
    )
    testbase.assert_equal(
        _fstab_contains_match(content=content, spec=spec), True,
    )

    no_match = MacosFstabEntry(
        device="UUID=ABC",
        mountpoint="/Volumes/data",
        fstype="apfs",
        options="ro",
    )
    testbase.assert_equal(
        _fstab_contains_match(content=content, spec=no_match), False,
    )
    return


def test_fstab_filter_out_mountpoint_preserves_comments():
    """
        ``_fstab_filter_out_mountpoint`` keeps comments and unrelated
        records, while removing the matching record and reporting
        ``removed=True``.
    """
    content = (
        "# header comment\n"
        "UUID=AAA\t/a\tapfs\trw\t0\t0\n"
        "UUID=BBB\t/b\tapfs\trw\t0\t0\n"
    )
    new_content, removed = _fstab_filter_out_mountpoint(
        content=content, mountpoint="/a",
    )
    testbase.assert_equal(removed, True)
    testbase.assert_true("# header comment" in new_content)
    testbase.assert_true("/a" not in new_content)
    testbase.assert_true("/b" in new_content)
    return


def test_mode_matches_handles_leading_zero():
    """
        ``_mode_matches`` recognises ``"0644"`` and ``"644"`` as the
        same mode, and rejects symbolic forms.
    """
    testbase.assert_equal(
        _mode_matches(requested="0644", observed="644"), True,
    )
    testbase.assert_equal(
        _mode_matches(requested="644", observed="755"), False,
    )
    testbase.assert_equal(
        _mode_matches(requested="u+rwx", observed="755"), False,
    )
    testbase.assert_equal(_is_numeric_mode(token="0644"), True)
    testbase.assert_equal(_is_numeric_mode(token="u+rwx"), False)
    testbase.assert_equal(_is_numeric_mode(token=""), False)
    return


# ----------------------------------------------------------------------
# Smoke test — dataclass round-trip
# ----------------------------------------------------------------------


def test_file_info_and_mount_info_round_trip():
    """
        :class:`MacosFileInfo` and :class:`MacosMountInfo` accept
        every documented field with sensible defaults.
    """
    fi = MacosFileInfo(
        path="/tmp/x",
        owner="alice",
        group="staff",
        mode="644",
        size=0,
        modified="Apr 24 12:34:56 2026",
    )
    testbase.assert_equal(fi.path, "/tmp/x")

    mi = MacosMountInfo(
        device="/dev/disk1",
        mountpoint="/",
        fstype="apfs",
        options="local",
    )
    testbase.assert_equal(mi.fstype, "apfs")
    return
