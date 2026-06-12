"""
    Tests for :class:`MacosIdentityManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``dsconfigad`` / ``dscl`` / ``dscacheutil`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.identitymanagement import (
    MacosIdentityActionStatus,
    MacosIdentityManager,
    MacosIdentityManagerConfig,
    MacosIdentityQueryError,
    MacosRealmJoinSpec,
    MacosRealmKind,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# Canonical dsconfigad / dscl output samples
# ----------------------------------------------------------------------


_DSCONFIGAD_BOUND = (
    "Active Directory Forest = AD.EXAMPLE.COM\n"
    "Active Directory Domain = ad.example.com\n"
    "Computer Account = mac-host\n"
    "Advanced Options - User Experience\n"
    "  Create mobile account at login = Disabled\n"
    "  Force home to startup disk = Enabled\n"
    "Advanced Options - Mappings\n"
    "  Mapping UID to attribute = not set\n"
)


_DSCONFIGAD_UNBOUND = (
    "Active Directory Forest = -\n"
    "Active Directory Domain = -\n"
    "Computer Account = -\n"
)


_DSCL_USER_AD = (
    "AppleMetaNodeLocation: /Active Directory/AD.EXAMPLE.COM/All Domains\n"
    "GeneratedUID: 12345678-90AB-CDEF-1234-567890ABCDEF\n"
    "NFSHomeDirectory: /Users/alice\n"
    "OriginalNodeName: /Active Directory/AD.EXAMPLE.COM/All Domains\n"
    "PrimaryGroupID: 5678\n"
    "RealName:\n"
    " Alice Doe\n"
    "RecordName: alice\n"
    "RecordType: dsRecTypeStandard:Users\n"
    "UniqueID: 1234\n"
    "UserShell: /bin/zsh\n"
)


_DSCL_USER_LOCAL = (
    "AppleMetaNodeLocation: /Local/Default\n"
    "NFSHomeDirectory: /Users/bob\n"
    "OriginalNodeName: /Local/Default\n"
    "PrimaryGroupID: 20\n"
    "RealName: Bob Smith\n"
    "RecordName: bob\n"
    "RecordType: dsRecTypeStandard:Users\n"
    "UniqueID: 501\n"
    "UserShell: /bin/zsh\n"
)


_DSCL_GROUP_AD = (
    "AppleMetaNodeLocation: /Active Directory/AD.EXAMPLE.COM/All Domains\n"
    "GroupMembership: alice bob\n"
    "OriginalNodeName: /Active Directory/AD.EXAMPLE.COM/All Domains\n"
    "PrimaryGroupID: 5678\n"
    "RecordName: staff\n"
    "RecordType: dsRecTypeStandard:Groups\n"
)


def _show_bound() -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=_DSCONFIGAD_BOUND, stderr="")
    return rtnval


def _show_unbound() -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=_DSCONFIGAD_UNBOUND, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# join_realm
# ----------------------------------------------------------------------


def test_join_realm_invokes_dsconfigad():
    """
        join_realm dispatches ``dsconfigad -add <domain> -username
        <admin> -password <pw> [-force]``; the recorded argv carries
        ``<redacted>`` in the password slot.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_unbound()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    spec = MacosRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="s3cret",
        computer_name="mac-host",
        ou_path="OU=Macs,DC=ad,DC=example,DC=com",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, MacosIdentityActionStatus.SUCCESS)

    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2
        and c["args"][0] == "dsconfigad"
        and c["args"][1] == "-add"
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(
        add_calls[0]["args"],
        [
            "dsconfigad",
            "-add", "ad.example.com",
            "-username", "admin",
            "-password", "s3cret",
            "-computer", "mac-host",
            "-ou", "OU=Macs,DC=ad,DC=example,DC=com",
            "-force",
        ],
    )
    testbase.assert_equal(add_calls[0]["elevated"], True)
    # The result.command is the recorded (sanitised) argv.
    testbase.assert_true(result.command is not None)
    testbase.assert_true("s3cret" not in result.command)
    testbase.assert_true("<redacted>" in result.command)
    return


def test_join_realm_skipped_when_joined():
    """
        join_realm returns SKIPPED when ``dsconfigad -show`` already
        reports the host bound to the requested realm.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    spec = MacosRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="s3cret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, MacosIdentityActionStatus.SKIPPED)
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["dsconfigad", "-add"]
    ]
    testbase.assert_equal(add_calls, [])
    return


def test_join_realm_dry_run():
    """
        join_realm returns DRY_RUN under ``dry_run=True`` and never
        invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(dry_run=True, elevated=True),
    )
    spec = MacosRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="s3cret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, MacosIdentityActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_true("s3cret" not in result.command)
    return


# ----------------------------------------------------------------------
# leave_realm
# ----------------------------------------------------------------------


def test_leave_realm_invokes_dsconfigad():
    """
        leave_realm dispatches ``dsconfigad -remove -username <admin>
        -password <pw> -force`` when the host is currently bound.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="admin",
        admin_password="topsecret",
    )

    testbase.assert_equal(result.status, MacosIdentityActionStatus.SUCCESS)
    remove_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["dsconfigad", "-remove"]
    ]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(
        remove_calls[0]["args"],
        [
            "dsconfigad", "-remove",
            "-username", "admin",
            "-password", "topsecret",
            "-force",
        ],
    )
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_leave_realm_skipped_when_not_joined():
    """
        leave_realm returns SKIPPED when the host is not bound to any
        realm.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_unbound()

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="admin",
        admin_password="pw",
    )

    testbase.assert_equal(result.status, MacosIdentityActionStatus.SKIPPED)
    remove_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["dsconfigad", "-remove"]
    ]
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# get_realm_status / list_joined_realms
# ----------------------------------------------------------------------


def test_get_realm_status_parses_dsconfigad_output():
    """
        get_realm_status parses ``dsconfigad -show`` and reports the
        bound realm + kind + computer name.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, True)
    testbase.assert_equal(status.current_realm, "ad.example.com")
    testbase.assert_equal(status.realm_kind, MacosRealmKind.ACTIVE_DIRECTORY)
    testbase.assert_equal(status.computer_name, "mac-host")
    return


def test_get_realm_status_returns_unjoined_when_unbound():
    """
        get_realm_status reports unjoined when ``dsconfigad -show``
        reports dash placeholders.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_unbound()

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, False)
    testbase.assert_equal(status.current_realm, None)
    return


def test_list_joined_realms_returns_list():
    """
        list_joined_realms returns a single-element list when the host
        is bound, and an empty list otherwise.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    realms = manager.list_joined_realms()

    testbase.assert_equal(len(realms), 1)
    testbase.assert_equal(realms[0].realm, "ad.example.com")
    testbase.assert_equal(realms[0].realm_kind, MacosRealmKind.ACTIVE_DIRECTORY)
    testbase.assert_equal(realms[0].is_default, True)
    return


# ----------------------------------------------------------------------
# sync_users
# ----------------------------------------------------------------------


def test_sync_users_invokes_dscacheutil_and_killall():
    """
        sync_users dispatches ``dscacheutil -flushcache`` followed by
        ``killall -HUP mDNSResponder``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    result = manager.sync_users()

    testbase.assert_equal(result.status, MacosIdentityActionStatus.SUCCESS)
    flush_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "dscacheutil"
    ]
    kick_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "killall"
    ]
    testbase.assert_equal(len(flush_calls), 1)
    testbase.assert_equal(
        flush_calls[0]["args"],
        ["dscacheutil", "-flushcache"],
    )
    testbase.assert_equal(len(kick_calls), 1)
    testbase.assert_equal(
        kick_calls[0]["args"],
        ["killall", "-HUP", "mDNSResponder"],
    )
    testbase.assert_equal(flush_calls[0]["elevated"], True)
    testbase.assert_equal(kick_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# resolve_user / resolve_group
# ----------------------------------------------------------------------


def test_resolve_user_parses_dscl_output():
    """
        resolve_user parses ``dscl /Search read /Users/<u>`` output
        into a populated :class:`MacosUserResolveResult`.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("dscl", "/Search", "-read", "/Users/alice")
    ] = RunResult(exit_code=0, stdout=_DSCL_USER_AD, stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="alice")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.uid, 1234)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.home, "/Users/alice")
    testbase.assert_equal(result.shell, "/bin/zsh")
    testbase.assert_equal(result.realm, "AD.EXAMPLE.COM")
    return


def test_resolve_user_local_no_realm():
    """
        resolve_user returns ``realm=None`` for local accounts whose
        ``OriginalNodeName`` points at ``/Local/Default``.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("dscl", "/Search", "-read", "/Users/bob")
    ] = RunResult(exit_code=0, stdout=_DSCL_USER_LOCAL, stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="bob")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.realm, None)
    testbase.assert_equal(result.uid, 501)
    return


def test_resolve_user_returns_not_found_when_unknown():
    """
        resolve_user returns ``found=False`` when ``dscl read`` exits
        non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("dscl", "/Search", "-read", "/Users/ghost")
    ] = RunResult(
        exit_code=64, stdout="",
        stderr="<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)\n",
    )

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="ghost")

    testbase.assert_equal(result.found, False)
    testbase.assert_equal(result.uid, None)
    return


def test_resolve_group_parses_dscl_output():
    """
        resolve_group parses ``dscl /Search read /Groups/<g>`` output.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("dscl", "/Search", "-read", "/Groups/staff")
    ] = RunResult(exit_code=0, stdout=_DSCL_GROUP_AD, stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )
    result = manager.resolve_group(groupname="staff")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.members, ["alice", "bob"])
    testbase.assert_equal(result.realm, "AD.EXAMPLE.COM")
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_password_redacted_in_recorded_command():
    """
        The recorded ``command`` field after ``join_realm`` MUST NOT
        contain the plaintext password — every ``-password <pw>`` pair
        is sanitised to ``-password <redacted>``.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_unbound()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    spec = MacosRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="topsecret-password-value",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_true(result.command is not None)
    for token in result.command:
        testbase.assert_true("topsecret-password-value" not in token)
    testbase.assert_true("<redacted>" in result.command)
    # The runner DID see the plaintext password on argv.
    add_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["dsconfigad", "-add"]
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_true("topsecret-password-value" in add_calls[0]["args"])
    return


def test_password_redacted_for_leave_realm():
    """
        leave_realm sanitises the ``-password`` value in the recorded
        argv as well.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(
        admin_user="admin",
        admin_password="leave-secret",
    )

    for token in result.command:
        testbase.assert_true("leave-secret" not in token)
    testbase.assert_true("<redacted>" in result.command)
    return


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    manager.leave_realm(admin_user="admin", admin_password="pw")
    manager.sync_users()

    elevated_writes = [
        c for c in runner.calls
        if c["elevated"] is True
        and len(c["args"]) >= 1
        and c["args"][0] in ("dsconfigad", "dscacheutil", "killall")
        and not (c["args"][:2] == ["dsconfigad", "-show"])
    ]
    testbase.assert_true(len(elevated_writes) >= 2)
    return


def test_reads_never_elevate():
    """
        Read verbs never elevate, even when ``elevated=True`` is set
        in the config.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_bound()
    runner.results[
        ("dscl", "/Search", "-read", "/Users/alice")
    ] = RunResult(exit_code=0, stdout=_DSCL_USER_AD, stderr="")
    runner.results[
        ("dscl", "/Search", "-read", "/Groups/staff")
    ] = RunResult(exit_code=0, stdout=_DSCL_GROUP_AD, stderr="")

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    manager.list_joined_realms()
    manager.resolve_user(username="alice")
    manager.resolve_group(groupname="staff")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``dsconfigad -add`` exit produces ``status=FAILED``
        with the captured ``stderr`` and ``exit_code`` on the result.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = _show_unbound()
    failure = RunResult(
        exit_code=2,
        stdout="",
        stderr="dsconfigad: invalid credential\n",
    )
    runner.default_result = failure

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(elevated=True),
    )
    spec = MacosRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="bad",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, MacosIdentityActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_true(
        result.stderr is not None and "invalid credential" in result.stderr,
    )
    return


def test_get_realm_status_raises_on_query_failure():
    """
        get_realm_status raises :class:`MacosIdentityQueryError` when
        ``dsconfigad -show`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("dsconfigad", "-show")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="dsconfigad: not authorised\n",
    )

    manager = MacosIdentityManager(
        runner=runner,
        config=MacosIdentityManagerConfig(),
    )

    raised = False
    try:
        manager.get_realm_status()
    except MacosIdentityQueryError:
        raised = True

    testbase.assert_true(raised)
    return
