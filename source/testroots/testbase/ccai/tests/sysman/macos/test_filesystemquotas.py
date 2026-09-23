"""
    Tests for :class:`MacosFilesystemQuotasManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``quotacheck`` / ``quotaon`` /
    ``setquota`` / ``quota`` / ``repquota`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.filesystemquotas import (
    MacosFilesystemQuotasManager,
    MacosFilesystemQuotasManagerConfig,
    MacosQuotaActionStatus,
    MacosQuotaFilesystemNotMountedError,
    MacosQuotaPrincipalKind,
    _parse_mount_output,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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
# Seed helpers
# ----------------------------------------------------------------------


def _seed_mount_hfs(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
) -> None:
    """
        Seed ``mount`` with a single HFS+ entry for
        ``mountpoint``.
    """
    body = (
        "/dev/disk2s1 on {} (hfs, local, journaled)\n".format(mountpoint)
    )
    runner.results[("mount",)] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_mount_apfs(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
) -> None:
    """
        Seed ``mount`` with a single APFS entry for
        ``mountpoint``.
    """
    body = (
        "/dev/disk1s1 on {} (apfs, sealed, local, read-only)\n".format(mountpoint)
    )
    runner.results[("mount",)] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_mount_empty(runner: _MockCommandRunner) -> None:
    """
        Seed ``mount`` to return no rows.
    """
    runner.results[("mount",)] = RunResult(
        exit_code=0,
        stdout="",
        stderr="",
    )
    return


def _seed_quotaon_status(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
    user_on: bool,
    group_on: bool,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``quotaon -p <mountpoint>`` output.
    """
    user_state = "on" if user_on else "off"
    group_state = "on" if group_on else "off"
    body_lines = [
        "user quota on {} (/dev/disk2s1) is {}".format(mountpoint, user_state),
        "group quota on {} (/dev/disk2s1) is {}".format(mountpoint, group_state),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quotaon", "-p", mountpoint)
    runner.results[key] = RunResult(
        exit_code=exit_code,
        stdout=body if exit_code == 0 else "",
        stderr=stderr,
    )
    return


def _seed_quota_user(
    runner: _MockCommandRunner,
    *,
    user: str,
    mountpoint: str,
    used_kb: int,
    soft_kb: int,
    hard_kb: int,
) -> None:
    """
        Seed ``quota -u <user>``.
    """
    body_lines = [
        "Disk quotas for user {} (uid 1001):".format(user),
        "     Filesystem   blocks   quota   limit   grace   files   quota   limit   grace",
        "       {}   {}  {}  {}        100       0       0".format(
            mountpoint, used_kb, soft_kb, hard_kb,
        ),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quota", "-u", user)
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_quota_group(
    runner: _MockCommandRunner,
    *,
    group: str,
    mountpoint: str,
    used_kb: int,
    soft_kb: int,
    hard_kb: int,
) -> None:
    """
        Seed ``quota -g <group>``.
    """
    body_lines = [
        "Disk quotas for group {} (gid 2001):".format(group),
        "     Filesystem   blocks   quota   limit   grace   files   quota   limit   grace",
        "       {}   {}  {}  {}        50       0       0".format(
            mountpoint, used_kb, soft_kb, hard_kb,
        ),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quota", "-g", group)
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_quota_user_no_record(
    runner: _MockCommandRunner,
    *,
    user: str,
) -> None:
    """
        Seed ``quota -u`` with empty body and exit code 2.
    """
    key = ("quota", "-u", user)
    runner.results[key] = RunResult(
        exit_code=2,
        stdout="",
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# enable_quotas
# ----------------------------------------------------------------------


def test_enable_quotas_invokes_quotacheck_and_quotaon():
    """
        ``enable_quotas`` on HFS+ must invoke ``quotacheck -ug``
        and ``quotaon -ug``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=False,
        group_on=False,
    )

    config = MacosFilesystemQuotasManagerConfig(elevated=True)
    manager = MacosFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SUCCESS)

    mutation_calls = [
        call for call in runner.calls
        if call["args"][0] in ("quotacheck", "quotaon")
        and (len(call["args"]) < 2 or call["args"][1] != "-p")
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    testbase.assert_equal(
        mutation_calls[0]["args"],
        ["quotacheck", "-ug", "/Volumes/Backup"],
    )
    testbase.assert_equal(
        mutation_calls[1]["args"],
        ["quotaon", "-ug", "/Volumes/Backup"],
    )
    return


def test_enable_quotas_skipped_when_enabled():
    """
        ``enable_quotas`` must return SKIPPED when both kinds are
        already on.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=True,
        group_on=True,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SKIPPED)
    return


def test_enable_quotas_dry_run():
    """
        ``enable_quotas`` must return DRY_RUN with no runner call.
    """
    runner = _MockCommandRunner()
    config = MacosFilesystemQuotasManagerConfig(dry_run=True)
    manager = MacosFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command,
        ["quotacheck", "-ug", "/Volumes/Backup"],
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# disable_quotas
# ----------------------------------------------------------------------


def test_disable_quotas_invokes_quotaoff():
    """
        ``disable_quotas`` on HFS+ must invoke ``quotaoff -ug``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=True,
        group_on=True,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.disable_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SUCCESS)

    quotaoff_calls = [
        call for call in runner.calls if call["args"][0] == "quotaoff"
    ]
    testbase.assert_equal(len(quotaoff_calls), 1)
    testbase.assert_equal(
        quotaoff_calls[0]["args"],
        ["quotaoff", "-ug", "/Volumes/Backup"],
    )
    return


# ----------------------------------------------------------------------
# set_user_quota / set_group_quota
# ----------------------------------------------------------------------


def test_set_user_quota_invokes_setquota():
    """
        ``set_user_quota`` must invoke ``setquota -u``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quota_user_no_record(runner, user="alice")

    config = MacosFilesystemQuotasManagerConfig(elevated=True)
    manager = MacosFilesystemQuotasManager(runner=runner, config=config)

    result = manager.set_user_quota(
        mountpoint="/Volumes/Backup",
        user="alice",
        soft_bytes=200 * 1024 * 1024,
        hard_bytes=400 * 1024 * 1024,
    )

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SUCCESS)

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 1)
    testbase.assert_equal(
        setquota_calls[0]["args"],
        ["setquota", "-u", "alice", "204800", "409600", "0", "0", "/Volumes/Backup"],
    )
    return


def test_set_user_quota_skipped_when_match():
    """
        ``set_user_quota`` must return SKIPPED when the current
        record already matches.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/Volumes/Backup",
        used_kb=10000,
        soft_kb=204800,
        hard_kb=409600,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.set_user_quota(
        mountpoint="/Volumes/Backup",
        user="alice",
        soft_bytes=204800 * 1024,
        hard_bytes=409600 * 1024,
    )

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SKIPPED)
    return


def test_set_group_quota_invokes_setquota():
    """
        ``set_group_quota`` must invoke ``setquota -g``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    runner.results[("quota", "-g", "staff")] = RunResult(
        exit_code=2, stdout="", stderr="",
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.set_group_quota(
        mountpoint="/Volumes/Backup",
        group="staff",
        soft_bytes=100 * 1024 * 1024,
        hard_bytes=200 * 1024 * 1024,
    )

    testbase.assert_equal(result.status, MacosQuotaActionStatus.SUCCESS)

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 1)
    testbase.assert_equal(
        setquota_calls[0]["args"],
        ["setquota", "-g", "staff", "102400", "204800", "0", "0", "/Volumes/Backup"],
    )
    return


# ----------------------------------------------------------------------
# get_user_quota / get_group_quota
# ----------------------------------------------------------------------


def test_get_user_quota_parses_quota_output():
    """
        ``get_user_quota`` must parse ``quota -u``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/Volumes/Backup",
        used_kb=1024,
        soft_kb=2048,
        hard_kb=4096,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    info = manager.get_user_quota(
        mountpoint="/Volumes/Backup",
        user="alice",
    )

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.used_bytes, 1024 * 1024)
    testbase.assert_equal(info.soft_bytes, 2048 * 1024)
    testbase.assert_equal(info.hard_bytes, 4096 * 1024)
    return


def test_get_group_quota_parses_quota_output():
    """
        ``get_group_quota`` must parse ``quota -g``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quota_group(
        runner,
        group="staff",
        mountpoint="/Volumes/Backup",
        used_kb=512,
        soft_kb=1024,
        hard_kb=2048,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    info = manager.get_group_quota(
        mountpoint="/Volumes/Backup",
        group="staff",
    )

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.principal_kind, MacosQuotaPrincipalKind.GROUP)
    testbase.assert_equal(info.used_bytes, 512 * 1024)
    return


# ----------------------------------------------------------------------
# list_quotas
# ----------------------------------------------------------------------


def test_list_quotas_parses_repquota_output():
    """
        ``list_quotas`` must parse ``repquota -ug`` output.
    """
    body_lines = [
        "*** Report for user quotas on device /dev/disk2s1",
        "Block grace time: 7days; Inode grace time: 7days",
        "                        Block limits                File limits",
        "User            used    soft    hard  grace    used  soft  hard  grace",
        "----------------------------------------------------------------------",
        "alice     --    1024    2048    4096            100     0     0",
        "*** Report for group quotas on device /dev/disk2s1",
        "Group           used    soft    hard  grace    used  soft  hard  grace",
        "----------------------------------------------------------------------",
        "staff     --     512    1024    2048             50     0     0",
    ]
    body = "\n".join(body_lines) + "\n"

    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    runner.results[("repquota", "-ug", "/Volumes/Backup")] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    entries = manager.list_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(len(entries), 2)
    user_entries = [
        entry for entry in entries
        if entry.principal_kind is MacosQuotaPrincipalKind.USER
    ]
    group_entries = [
        entry for entry in entries
        if entry.principal_kind is MacosQuotaPrincipalKind.GROUP
    ]
    testbase.assert_equal(len(user_entries), 1)
    testbase.assert_equal(len(group_entries), 1)
    testbase.assert_equal(user_entries[0].principal, "alice")
    testbase.assert_equal(user_entries[0].used_bytes, 1024 * 1024)
    return


# ----------------------------------------------------------------------
# get_quota_status
# ----------------------------------------------------------------------


def test_get_quota_status_parses_quotaon_output():
    """
        ``get_quota_status`` on HFS+ must parse ``quotaon -p``
        output.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=True,
        group_on=False,
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    status = manager.get_quota_status(mountpoint="/Volumes/Backup")

    testbase.assert_equal(status.user_quotas_active, True)
    testbase.assert_equal(status.group_quotas_active, False)
    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.filesystem_type, "hfs")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write commands must dispatch with ``elevated=True`` when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=False,
        group_on=False,
    )

    config = MacosFilesystemQuotasManagerConfig(elevated=True)
    manager = MacosFilesystemQuotasManager(runner=runner, config=config)
    manager.enable_quotas(mountpoint="/Volumes/Backup")

    mutation_calls = [
        call for call in runner.calls
        if call["args"][0] in ("quotacheck", "quotaon")
        and (len(call["args"]) < 2 or call["args"][1] != "-p")
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    for call in mutation_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs must dispatch with ``elevated=False`` regardless
        of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/Volumes/Backup",
        used_kb=1, soft_kb=2, hard_kb=3,
    )
    runner.results[("repquota", "-ug", "/Volumes/Backup")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=True,
        group_on=True,
    )

    config = MacosFilesystemQuotasManagerConfig(elevated=True)
    manager = MacosFilesystemQuotasManager(runner=runner, config=config)

    manager.get_user_quota(mountpoint="/Volumes/Backup", user="alice")
    manager.list_quotas(mountpoint="/Volumes/Backup")
    manager.get_quota_status(mountpoint="/Volumes/Backup")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``quotacheck`` must return ``FAILED`` with the
        runner's stderr captured.
    """
    runner = _MockCommandRunner()
    _seed_mount_hfs(runner, mountpoint="/Volumes/Backup")
    _seed_quotaon_status(
        runner,
        mountpoint="/Volumes/Backup",
        user_on=False,
        group_on=False,
    )
    runner.results[("quotacheck", "-ug", "/Volumes/Backup")] = RunResult(
        exit_code=2,
        stdout="",
        stderr="quotacheck: cannot find filesystem to check",
    )

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="/Volumes/Backup")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("quotacheck:" in result.stderr, True)
    return


# ----------------------------------------------------------------------
# APFS gap
# ----------------------------------------------------------------------


def test_apfs_returns_failed_with_detail():
    """
        Writes against an APFS mountpoint must return ``FAILED``
        with ``detail="apfs_no_quota_support"`` and must NOT
        invoke any quota command.
    """
    runner = _MockCommandRunner()
    _seed_mount_apfs(runner, mountpoint="/Volumes/Data")

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="/Volumes/Data")

    testbase.assert_equal(result.status, MacosQuotaActionStatus.FAILED)
    testbase.assert_equal(result.detail, "apfs_no_quota_support")

    quota_command_calls = [
        call for call in runner.calls
        if call["args"][0] in ("quotacheck", "quotaon", "quotaoff", "setquota")
    ]
    testbase.assert_equal(len(quota_command_calls), 0)
    return


def test_apfs_set_user_quota_returns_failed_with_detail():
    """
        ``set_user_quota`` against APFS must return FAILED
        without invoking ``setquota``.
    """
    runner = _MockCommandRunner()
    _seed_mount_apfs(runner, mountpoint="/Volumes/Data")

    manager = MacosFilesystemQuotasManager(runner=runner)
    result = manager.set_user_quota(
        mountpoint="/Volumes/Data",
        user="alice",
        soft_bytes=1024,
        hard_bytes=2048,
    )

    testbase.assert_equal(result.status, MacosQuotaActionStatus.FAILED)
    testbase.assert_equal(result.detail, "apfs_no_quota_support")

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 0)
    return


# ----------------------------------------------------------------------
# Mount validation
# ----------------------------------------------------------------------


def test_resolve_fstype_raises_when_unmounted():
    """
        Writes must raise
        :class:`MacosQuotaFilesystemNotMountedError` when the
        mountpoint is absent from ``mount`` output.
    """
    runner = _MockCommandRunner()
    _seed_mount_empty(runner)

    manager = MacosFilesystemQuotasManager(runner=runner)

    raised = False
    try:
        manager.enable_quotas(mountpoint="/Volumes/Missing")
    except MacosQuotaFilesystemNotMountedError:
        raised = True

    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# Pure-parser tests
# ----------------------------------------------------------------------


def test_parse_mount_output_anchors_on_last_on():
    """
        ``_parse_mount_output`` must parse rows whose mountpoint
        contains the literal text ``" on "``.  The implementation
        anchors on the FIRST ``" on "`` (the one separating the
        device from the rest of the line); the legacy test name
        refers to that anchoring contract.
    """
    body = (
        "/dev/disk1s1 on /Volumes/Step on this (hfs, local)\n"
    )
    fstype = _parse_mount_output(
        stdout=body,
        mountpoint="/Volumes/Step on this",
    )
    testbase.assert_equal(fstype, "hfs")
    return
