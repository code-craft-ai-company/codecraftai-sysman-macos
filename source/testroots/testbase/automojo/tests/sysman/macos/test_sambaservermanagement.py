"""
    Tests for :class:`MacosSambaServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``sharing`` / ``launchctl`` /
    ``dscl`` / ``pdbedit`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.sambaservermanagement import (
    MacosSambaActionStatus,
    MacosSambaManagerConfig,
    MacosSambaQueryError,
    MacosSambaServerManager,
    MacosSambaShareAcl,
    MacosSambaShareInfo,
    MacosSambaShareSpec,
    MacosSambaUserInfo,
    _parse_pdbedit_output,
    _parse_sharing_list_output,
    _render_dscl_acl_changes,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
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


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


def _seed_sharing_list(
    runner: _MockCommandRunner,
    *,
    stdout: str,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``sharing -l``.
    """
    runner.results[("sharing", "-l")] = RunResult(
        exit_code=exit_code, stdout=stdout, stderr=stderr,
    )
    return


def _seed_pdbedit_verbose(
    runner: _MockCommandRunner,
    *,
    stdout: str,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``pdbedit -L -v``.
    """
    runner.results[("pdbedit", "-L", "-v")] = RunResult(
        exit_code=exit_code, stdout=stdout, stderr=stderr,
    )
    return


def _make_spec(
    *,
    name: str = "public",
    path: str = "/Users/Shared/public",
    description: Optional[str] = None,
    read_only: bool = False,
    valid_users: Optional[list] = None,
    browseable: bool = True,
    guest_ok: bool = False,
) -> MacosSambaShareSpec:
    spec = MacosSambaShareSpec(
        name=name,
        path=path,
        description=description,
        read_only=read_only,
        valid_users=valid_users,
        browseable=browseable,
        guest_ok=guest_ok,
    )
    return spec


_SHARING_LIST_TWO_SHARES = (
    "name:           public\n"
    "path:           /Users/Shared/public\n"
    "smb_shared:     1\n"
    "smb_guestaccess: 0\n"
    "smb_inherit_perms: 1\n"
    "name:           private\n"
    "path:           /Users/Shared/private\n"
    "smb_shared:     1\n"
    "smb_guestaccess: 0\n"
    "smb_inherit_perms: 1\n"
)


# ----------------------------------------------------------------------
# add_share
# ----------------------------------------------------------------------


def test_add_share_invokes_sharing():
    """
        add_share dispatches ``sharing -a <path> -S <name> -s 100
        -g <guest>`` via the runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_sharing_list(runner, stdout="")

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, MacosSambaActionStatus.SUCCESS)
    testbase.assert_equal(result.share_name, "public")

    sharing_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 1
            and call["args"][0] == "sharing"
            and "-a" in call["args"]
        )
    ]
    testbase.assert_equal(len(sharing_calls), 1)
    argv = sharing_calls[0]["args"]
    testbase.assert_equal("-a" in argv, True)
    testbase.assert_equal("/Users/Shared/public" in argv, True)
    testbase.assert_equal("-S" in argv, True)
    testbase.assert_equal("public" in argv, True)
    testbase.assert_equal("-s" in argv, True)
    testbase.assert_equal("100" in argv, True)
    return


def test_add_share_skipped_when_present():
    """
        add_share returns SKIPPED when ``sharing -l`` already lists
        a share with matching name and path.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public", path="/Users/Shared/public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, MacosSambaActionStatus.SKIPPED)

    sharing_add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "sharing"
            and call["args"][1] == "-a"
        )
    ]
    testbase.assert_equal(len(sharing_add_calls), 0)
    return


def test_add_share_dry_run():
    """
        add_share under ``dry_run=True`` returns DRY_RUN and never
        invokes ``sharing -a``.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout="")

    config = MacosSambaManagerConfig(elevated=True, dry_run=True)
    manager = MacosSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, MacosSambaActionStatus.DRY_RUN)
    testbase.assert_equal(result.command[0], "sharing")
    testbase.assert_equal("-a" in result.command, True)

    sharing_add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "sharing"
            and call["args"][1] == "-a"
        )
    ]
    testbase.assert_equal(len(sharing_add_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_share
# ----------------------------------------------------------------------


def test_remove_share_invokes_sharing():
    """
        remove_share dispatches ``sharing -r <name>`` when the
        share is present.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, MacosSambaActionStatus.SUCCESS)
    sharing_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 3
            and call["args"][0] == "sharing"
            and call["args"][1] == "-r"
            and call["args"][2] == "public"
        )
    ]
    testbase.assert_equal(len(sharing_calls), 1)
    return


def test_remove_share_skipped_when_absent():
    """
        remove_share returns SKIPPED when the share is not
        registered.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout="")

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, MacosSambaActionStatus.SKIPPED)
    sharing_remove_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "sharing"
            and call["args"][1] == "-r"
        )
    ]
    testbase.assert_equal(len(sharing_remove_calls), 0)
    return


# ----------------------------------------------------------------------
# list_shares
# ----------------------------------------------------------------------


def test_list_shares_parses_output():
    """
        list_shares parses ``sharing -l`` blocks into
        :class:`MacosSambaShareInfo` records.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_shares()
    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.name: entry for entry in rtnval}
    testbase.assert_equal(by_name["public"].path, "/Users/Shared/public")
    testbase.assert_equal(by_name["public"].is_active, True)
    return


def test_list_shares_filters_non_smb_records():
    """
        list_shares drops records whose ``smb_shared`` flag is 0.
    """
    runner = _MockCommandRunner()
    body = (
        "name:           afp_only\n"
        "path:           /Users/Shared/afp\n"
        "smb_shared:     0\n"
        "name:           public\n"
        "path:           /Users/Shared/public\n"
        "smb_shared:     1\n"
    )
    _seed_sharing_list(runner, stdout=body)

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_shares()
    testbase.assert_equal(len(rtnval), 1)
    testbase.assert_equal(rtnval[0].name, "public")
    return


# ----------------------------------------------------------------------
# get_share
# ----------------------------------------------------------------------


def test_get_share_returns_correct_record():
    """
        get_share returns the matching record and ``None`` for a
        missing share.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    found = manager.get_share(name="private")
    testbase.assert_equal(found is not None, True)
    testbase.assert_equal(found.name, "private")

    missing = manager.get_share(name="absent")
    testbase.assert_equal(missing, None)
    return


# ----------------------------------------------------------------------
# set_share_permissions
# ----------------------------------------------------------------------


def test_set_share_permissions_invokes_dscl():
    """
        set_share_permissions dispatches one ``dscl . -create
        /SharePoints/<name> smb_<key> ...`` invocation per non-
        empty principal list.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    acl = MacosSambaShareAcl(
        read_users=["alice"],
        write_users=["bob"],
        full_access_users=["admin"],
        denied_users=["mallory"],
    )

    result = manager.set_share_permissions(name="public", acl=acl)

    testbase.assert_equal(result.status, MacosSambaActionStatus.SUCCESS)
    dscl_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 1
            and call["args"][0] == "dscl"
            and "-create" in call["args"]
        )
    ]
    testbase.assert_equal(len(dscl_calls), 4)

    keys_seen: list = []
    for call in dscl_calls:
        for token in call["args"]:
            if token.startswith("smb_"):
                keys_seen.append(token)
    testbase.assert_equal("smb_full_access" in keys_seen, True)
    testbase.assert_equal("smb_read_only" in keys_seen, True)
    testbase.assert_equal("smb_read_write" in keys_seen, True)
    testbase.assert_equal("smb_denied" in keys_seen, True)
    return


def test_set_share_permissions_skipped_when_share_absent():
    """
        set_share_permissions returns SKIPPED when the share is
        not registered.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout="")

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    acl = MacosSambaShareAcl(
        read_users=["alice"],
        write_users=[],
        full_access_users=[],
        denied_users=[],
    )
    result = manager.set_share_permissions(name="absent", acl=acl)
    testbase.assert_equal(result.status, MacosSambaActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# reload_config
# ----------------------------------------------------------------------


def test_reload_config_invokes_launchctl():
    """
        reload_config dispatches
        ``launchctl kickstart -k system/com.apple.smbd``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    result = manager.reload_config()

    testbase.assert_equal(result.status, MacosSambaActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command,
        ["launchctl", "kickstart", "-k", "system/com.apple.smbd"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# add_samba_user / remove_samba_user delegation
# ----------------------------------------------------------------------


def test_add_samba_user_returns_failed_with_delegation_message():
    """
        add_samba_user always returns FAILED with the delegation
        message under the default ``delegate_user_mgmt=True``.  The
        password is NEVER passed to the runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    secret = "topSecretP@ss"
    result = manager.add_samba_user(name="alice", password=secret)

    testbase.assert_equal(result.status, MacosSambaActionStatus.FAILED)
    testbase.assert_equal(
        result.detail, "add_samba_user delegated to user-group-management",
    )
    testbase.assert_equal(
        "user-group-management" in result.message, True,
    )
    testbase.assert_equal(result.username, "alice")

    runner_calls = [
        call for call in runner.calls
        if call["stdin"] is not None
    ]
    testbase.assert_equal(len(runner_calls), 0)

    recorded = " ".join(result.command)
    testbase.assert_equal(secret in recorded, False)
    testbase.assert_equal(
        secret in (result.stderr or ""), False,
    )
    return


def test_remove_samba_user_returns_failed_with_delegation_message():
    """
        remove_samba_user always returns FAILED with the delegation
        message.
    """
    runner = _MockCommandRunner()

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    result = manager.remove_samba_user(name="alice")

    testbase.assert_equal(result.status, MacosSambaActionStatus.FAILED)
    testbase.assert_equal(
        result.detail,
        "remove_samba_user delegated to user-group-management",
    )
    testbase.assert_equal(
        "user-group-management" in result.message, True,
    )
    testbase.assert_equal(result.username, "alice")

    testbase.assert_equal(len(runner.calls), 0)
    return


def test_user_mgmt_delegates_with_explanation():
    """
        Both delegated user verbs surface a human-readable
        explanation directing callers to the user-group-management
        feature.
    """
    runner = _MockCommandRunner()
    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    add_result = manager.add_samba_user(name="alice", password="secret")
    remove_result = manager.remove_samba_user(name="alice")

    for result in (add_result, remove_result):
        testbase.assert_equal(
            result.status, MacosSambaActionStatus.FAILED,
        )
        testbase.assert_equal(
            "macOS SMB authentication" in result.message, True,
        )
        testbase.assert_equal(
            "user-group-management" in result.message, True,
        )
    return


# ----------------------------------------------------------------------
# list_samba_users
# ----------------------------------------------------------------------


def test_list_samba_users_parses_pdbedit_output():
    """
        list_samba_users parses ``pdbedit -L -v`` blocks when
        ``pdbedit`` is available.
    """
    runner = _MockCommandRunner()
    _seed_pdbedit_verbose(
        runner,
        stdout=(
            "Unix username:        alice\n"
            "Account Flags:        [U          ]\n"
            "Account desc:         Alice\n"
            "Password last set:    Mon, 01 Jan 2024 00:00:00 UTC\n"
            "\n"
            "Unix username:        bob\n"
            "Account Flags:        [DU         ]\n"
            "Account desc:         Bob\n"
            "Password last set:    Tue, 02 Jan 2024 00:00:00 UTC\n"
        ),
    )

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_samba_users()
    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.username: entry for entry in rtnval}
    testbase.assert_equal(by_name["alice"].enabled, True)
    testbase.assert_equal(by_name["bob"].enabled, False)
    return


def test_list_samba_users_returns_empty_when_pdbedit_absent():
    """
        list_samba_users returns an empty list when the
        ``pdbedit`` binary is not installed.
    """
    runner = _MockCommandRunner()
    _seed_pdbedit_verbose(
        runner,
        stdout="",
        exit_code=127,
        stderr="pdbedit: command not found",
    )

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_samba_users()
    testbase.assert_equal(rtnval, [])
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` to the runner when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_sharing_list(runner, stdout="")

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    spec = _make_spec(name="public")
    manager.add_share(spec=spec)
    manager.reload_config()

    sharing_add_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "sharing"
            and call["args"][1] == "-a"
        )
    ]
    testbase.assert_equal(len(sharing_add_calls), 1)
    testbase.assert_equal(sharing_add_calls[0]["elevated"], True)

    launchctl_calls = [
        call for call in runner.calls
        if call["args"][0] == "launchctl"
    ]
    testbase.assert_equal(len(launchctl_calls), 1)
    testbase.assert_equal(launchctl_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_sharing_list(runner, stdout=_SHARING_LIST_TWO_SHARES)
    _seed_pdbedit_verbose(
        runner,
        stdout=(
            "Unix username:        alice\n"
            "Account Flags:        [U          ]\n"
        ),
    )

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)

    manager.list_shares()
    manager.get_share(name="public")
    manager.list_samba_users()

    for call in runner.calls:
        if call["args"] == ["sharing", "-l"]:
            testbase.assert_equal(call["elevated"], False)
        if call["args"] == ["pdbedit", "-L", "-v"]:
            testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``sharing -a`` produces a FAILED result with the
        runner's stderr, exit code, and command attached.
    """
    runner = _MockCommandRunner()
    _seed_sharing_list(runner, stdout="")
    runner.default_result = _fail(
        stderr="sharing: error", exit_code=1,
    )

    config = MacosSambaManagerConfig(elevated=True)
    manager = MacosSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    # Override the sharing -a default with a failure.
    runner.results[
        ("sharing", "-a", spec.path, "-S", spec.name, "-s", "100", "-g", "0")
    ] = _fail(stderr="sharing: error", exit_code=1)

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, MacosSambaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("sharing: error" in result.stderr, True)
    testbase.assert_equal(result.command[0], "sharing")
    return


def test_list_shares_raises_query_error_on_unexpected_failure():
    """
        list_shares raises :class:`MacosSambaQueryError` when
        ``sharing -l`` exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("sharing", "-l")] = _fail(
        stderr="sharing: permission denied", exit_code=1,
    )

    config = MacosSambaManagerConfig()
    manager = MacosSambaServerManager(runner=runner, config=config)

    raised: Optional[MacosSambaQueryError] = None
    try:
        manager.list_shares()
    except MacosSambaQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Renderer / parser unit tests
# ----------------------------------------------------------------------


def test_parse_sharing_list_output_handles_blocks():
    """
        ``_parse_sharing_list_output`` correctly splits multi-share
        output into per-share blocks.
    """
    parsed = _parse_sharing_list_output(text=_SHARING_LIST_TWO_SHARES)
    testbase.assert_equal(len(parsed), 2)
    testbase.assert_equal(parsed[0].name, "public")
    testbase.assert_equal(parsed[1].name, "private")
    return


def test_parse_pdbedit_output_handles_blocks():
    """
        ``_parse_pdbedit_output`` parses multi-user output blocks.
    """
    text = (
        "Unix username:        alice\n"
        "Account Flags:        [U          ]\n"
        "\n"
        "Unix username:        bob\n"
        "Account Flags:        [DU         ]\n"
    )
    parsed = _parse_pdbedit_output(text=text)
    testbase.assert_equal(len(parsed), 2)
    by_name = {entry.username: entry for entry in parsed}
    testbase.assert_equal(by_name["alice"].enabled, True)
    testbase.assert_equal(by_name["bob"].enabled, False)
    return


def test_render_dscl_acl_changes_emits_per_key_invocations():
    """
        ``_render_dscl_acl_changes`` emits one ``dscl`` invocation
        per non-empty principal list.
    """
    acl = MacosSambaShareAcl(
        read_users=["alice"],
        write_users=[],
        full_access_users=["admin"],
        denied_users=[],
    )
    rendered = _render_dscl_acl_changes(name="public", acl=acl)
    testbase.assert_equal(len(rendered), 2)
    keys: list = []
    for argv in rendered:
        for token in argv:
            if token.startswith("smb_"):
                keys.append(token)
    testbase.assert_equal(keys, ["smb_full_access", "smb_read_only"])
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_share_info_dataclass_shape():
    """
        :class:`MacosSambaShareInfo` exposes the contracted fields.
    """
    info = MacosSambaShareInfo(
        name="public",
        path="/Users/Shared/public",
        description=None,
        read_only=False,
        valid_users=None,
        read_list=None,
        write_list=None,
        browseable=True,
        guest_ok=False,
        is_active=True,
        connection_count=None,
    )
    testbase.assert_equal(info.is_active, True)
    return


def test_user_info_dataclass_shape():
    """
        :class:`MacosSambaUserInfo` exposes the contracted fields.
    """
    info = MacosSambaUserInfo(
        username="alice",
        enabled=True,
        last_password_change=None,
        description=None,
    )
    testbase.assert_equal(info.username, "alice")
    return
