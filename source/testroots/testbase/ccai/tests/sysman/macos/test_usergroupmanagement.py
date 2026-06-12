"""
    Tests for :class:`MacosUserGroupManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``sysadminctl`` / ``dscl`` / ``dseditgroup`` /
    ``pwpolicy`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.usergroupmanagement import (
    MacosGroupInfo,
    MacosGroupSpec,
    MacosPrincipalKind,
    MacosUserChanges,
    MacosUserGroupActionStatus,
    MacosUserGroupManager,
    MacosUserGroupManagerConfig,
    MacosUserGroupQueryError,
    MacosUserInfo,
    MacosUserSpec,
    _materialise_user_attributes,
    _parse_dscl_list_uid_line,
    _parse_dscl_read_output,
    _parse_primary_group_id,
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


def _seed_user_record(
    *,
    runner: _MockCommandRunner,
    name: str,
    uid: int = 501,
    gid: int = 20,
    home: str = "/Users/example",
    shell: str = "/bin/zsh",
    full_name: str = "Example User",
) -> None:
    """
        Seed a ``dscl . -read /Users/<name>`` :class:`RunResult` with a
        canonical attribute block.
    """
    body_lines = [
        "RecordName: {}".format(name),
        "UniqueID: {}".format(uid),
        "PrimaryGroupID: {}".format(gid),
        "NFSHomeDirectory: {}".format(home),
        "UserShell: {}".format(shell),
        "RealName: {}".format(full_name),
    ]
    output = "\n".join(body_lines) + "\n"
    runner.results[("dscl", ".", "-read", "/Users/{}".format(name))] = (
        RunResult(exit_code=0, stdout=output, stderr="")
    )
    return


def _seed_user_missing(
    *,
    runner: _MockCommandRunner,
    name: str,
) -> None:
    """
        Seed a ``dscl . -read /Users/<name>`` :class:`RunResult` that
        reports the user does not exist.
    """
    runner.results[("dscl", ".", "-read", "/Users/{}".format(name))] = (
        RunResult(
            exit_code=1, stdout="", stderr="<dscl_cmd> DS Error",
        )
    )
    return


def _seed_group_record(
    *,
    runner: _MockCommandRunner,
    name: str,
    gid: int = 30,
    members: Optional[list] = None,
) -> None:
    """
        Seed a ``dscl . -read /Groups/<name>`` :class:`RunResult` and a
        ``dscl . -read /Groups/<name> PrimaryGroupID`` companion.
    """
    if members is None:
        members = []

    membership = " ".join(members)
    body_lines = [
        "RecordName: {}".format(name),
        "PrimaryGroupID: {}".format(gid),
    ]
    if len(membership) > 0:
        body_lines.append("GroupMembership: {}".format(membership))
    output = "\n".join(body_lines) + "\n"

    runner.results[("dscl", ".", "-read", "/Groups/{}".format(name))] = (
        RunResult(exit_code=0, stdout=output, stderr="")
    )
    runner.results[
        ("dscl", ".", "-read", "/Groups/{}".format(name), "PrimaryGroupID")
    ] = RunResult(
        exit_code=0,
        stdout="PrimaryGroupID: {}\n".format(gid),
        stderr="",
    )
    return


def _seed_group_missing(
    *,
    runner: _MockCommandRunner,
    name: str,
) -> None:
    """
        Seed a missing group probe.
    """
    runner.results[("dscl", ".", "-read", "/Groups/{}".format(name))] = (
        RunResult(
            exit_code=1, stdout="", stderr="<dscl_cmd> DS Error",
        )
    )
    return


def _seed_account_unlocked(
    *,
    runner: _MockCommandRunner,
    user: str,
) -> None:
    """
        Seed ``pwpolicy -u <user> -authentication-allowed`` to report
        that authentication is allowed.
    """
    runner.results[("pwpolicy", "-u", user, "-authentication-allowed")] = (
        RunResult(
            exit_code=0,
            stdout="Authentication is allowed for {}\n".format(user),
            stderr="",
        )
    )
    return


def _seed_account_locked(
    *,
    runner: _MockCommandRunner,
    user: str,
) -> None:
    """
        Seed ``pwpolicy -u <user> -authentication-allowed`` to report
        that authentication is NOT allowed.
    """
    runner.results[("pwpolicy", "-u", user, "-authentication-allowed")] = (
        RunResult(
            exit_code=0,
            stdout="Authentication is not allowed for {}\n".format(user),
            stderr="",
        )
    )
    return


def _seed_id_groups(
    *,
    runner: _MockCommandRunner,
    user: str,
    groups: list,
) -> None:
    """
        Seed ``id -Gn <user>`` to list the given group names.
    """
    runner.results[("id", "-Gn", user)] = RunResult(
        exit_code=0,
        stdout=" ".join(groups) + "\n",
        stderr="",
    )
    return


def _seed_dseditgroup_member(
    *,
    runner: _MockCommandRunner,
    user: str,
    group: str,
    is_member: bool,
) -> None:
    """
        Seed the ``dseditgroup -o checkmember -m <user> <group>`` exit
        code.
    """
    if is_member:
        exit_code = 0
        stdout = "yes {} is a member of {}\n".format(user, group)
    else:
        exit_code = 64
        stdout = "no {} is NOT a member of {}\n".format(user, group)
    runner.results[
        ("dseditgroup", "-o", "checkmember", "-m", user, group)
    ] = RunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# create_user
# ----------------------------------------------------------------------


def test_create_user_constructs_sysadminctl_argv_with_admin_pair():
    """
        create_user dispatches ``sysadminctl -addUser`` with the
        configured admin pair appended.  Recorded argv redacts every
        password value.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_missing(runner=runner, name="alice")

    config = MacosUserGroupManagerConfig(
        admin_user="adminbob",
        admin_password="adminpw",
    )
    manager = MacosUserGroupManager(runner=runner, config=config)

    spec = MacosUserSpec(
        name="alice",
        full_name="Alice Example",
        password="topsecret",
        uid=701,
        home="/Users/alice",
        shell="/bin/zsh",
    )
    result = manager.create_user(spec=spec)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)
    testbase.assert_equal(result.kind, MacosPrincipalKind.USER)
    testbase.assert_equal(result.name, "alice")

    # The full argv (sent to the runner) carries plaintext values.
    write_call = runner.calls[-1]
    expected_full = [
        "sysadminctl", "-addUser", "alice",
        "-fullName", "Alice Example",
        "-password", "topsecret",
        "-shell", "/bin/zsh",
        "-home", "/Users/alice",
        "-UID", "701",
        "-adminUser", "adminbob",
        "-adminPassword", "adminpw",
    ]
    testbase.assert_equal(write_call["args"], expected_full)

    # The recorded argv (on the result) redacts both password values.
    expected_recorded = [
        "sysadminctl", "-addUser", "alice",
        "-fullName", "Alice Example",
        "-password", "<redacted>",
        "-shell", "/bin/zsh",
        "-home", "/Users/alice",
        "-UID", "701",
        "-adminUser", "adminbob",
        "-adminPassword", "<redacted>",
    ]
    testbase.assert_equal(result.command, expected_recorded)
    return


def test_create_user_skipped_when_present():
    """
        create_user returns SKIPPED when ``dscl . -read`` already
        reports the user.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(runner=runner, name="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.create_user(spec=MacosUserSpec(name="alice"))

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysadminctl", "-addUser"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# delete_user
# ----------------------------------------------------------------------


def test_delete_user_emits_secure_flag():
    """
        delete_user dispatches ``sysadminctl -deleteUser <name>
        -secure``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(runner=runner, name="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.delete_user(name="alice")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)
    expected = ["sysadminctl", "-deleteUser", "alice", "-secure"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_delete_user_skipped_when_absent():
    """
        delete_user returns SKIPPED when ``dscl . -read`` reports the
        user is missing.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_missing(runner=runner, name="ghost")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.delete_user(name="ghost")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    delete_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysadminctl", "-deleteUser"]
    ]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# modify_user
# ----------------------------------------------------------------------


def test_modify_user_emits_one_dscl_change_per_field():
    """
        modify_user emits one ``dscl . -change`` per scalar attribute
        whose new value differs from the current value.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(
        runner=runner, name="alice",
        uid=501, gid=20,
        home="/Users/alice", shell="/bin/zsh",
        full_name="Alice",
    )
    _seed_account_unlocked(runner=runner, user="alice")
    _seed_id_groups(runner=runner, user="alice", groups=["staff"])

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    changes = MacosUserChanges(
        home="/Users/alicia",
        shell="/bin/bash",
        full_name="Alicia",
    )
    result = manager.modify_user(name="alice", changes=changes)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)

    change_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dscl", ".", "-change"]
    ]
    testbase.assert_equal(len(change_calls), 3)

    # Order: home, shell, full_name (declaration order in plan).
    home_call = change_calls[0]["args"]
    testbase.assert_equal(home_call[3], "/Users/alice")
    testbase.assert_equal(home_call[4], "NFSHomeDirectory")
    testbase.assert_equal(home_call[5], "/Users/alice")
    testbase.assert_equal(home_call[6], "/Users/alicia")

    shell_call = change_calls[1]["args"]
    testbase.assert_equal(shell_call[4], "UserShell")
    testbase.assert_equal(shell_call[6], "/bin/bash")

    name_call = change_calls[2]["args"]
    testbase.assert_equal(name_call[4], "RealName")
    testbase.assert_equal(name_call[6], "Alicia")
    return


def test_modify_user_skipped_when_noop():
    """
        modify_user returns SKIPPED when every requested change already
        matches the user's observed state.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(
        runner=runner, name="alice",
        home="/Users/alice", shell="/bin/zsh", full_name="Alice",
    )
    _seed_account_unlocked(runner=runner, user="alice")
    _seed_id_groups(runner=runner, user="alice", groups=["staff"])

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    changes = MacosUserChanges(
        home="/Users/alice",
        shell="/bin/zsh",
    )
    result = manager.modify_user(name="alice", changes=changes)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    change_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dscl", ".", "-change"]
    ]
    testbase.assert_equal(change_calls, [])
    return


def test_modify_user_returns_failed_when_user_missing():
    """
        modify_user returns FAILED with a descriptive message when the
        target user does not exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_missing(runner=runner, name="ghost")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.modify_user(
        name="ghost",
        changes=MacosUserChanges(shell="/bin/bash"),
    )

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.message, "user does not exist")
    change_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dscl", ".", "-change"]
    ]
    testbase.assert_equal(change_calls, [])
    return


def test_modify_user_stops_on_first_failure():
    """
        modify_user returns FAILED at the first non-zero ``dscl
        -change`` and does NOT attempt subsequent steps.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(
        runner=runner, name="alice",
        home="/Users/alice", shell="/bin/zsh", full_name="Alice",
    )
    _seed_account_unlocked(runner=runner, user="alice")
    _seed_id_groups(runner=runner, user="alice", groups=["staff"])

    # Force the FIRST change call (home) to fail.
    runner.results[(
        "dscl", ".", "-change", "/Users/alice", "NFSHomeDirectory",
        "/Users/alice", "/Users/alicia",
    )] = RunResult(
        exit_code=2,
        stdout="",
        stderr="permission denied",
    )

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    changes = MacosUserChanges(
        home="/Users/alicia",
        shell="/bin/bash",
    )
    result = manager.modify_user(name="alice", changes=changes)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    # Only the home change should have been attempted.
    change_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dscl", ".", "-change"]
    ]
    testbase.assert_equal(len(change_calls), 1)
    testbase.assert_equal(change_calls[0]["args"][4], "NFSHomeDirectory")
    return


# ----------------------------------------------------------------------
# set_password / lock_account / unlock_account
# ----------------------------------------------------------------------


def test_set_password_argv_sent_to_runner_but_command_sanitised():
    """
        set_password sends the plaintext password to the runner but
        records ``"<redacted>"`` for every password value on the
        result.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    config = MacosUserGroupManagerConfig(
        admin_user="adminbob",
        admin_password="adminpw",
    )
    manager = MacosUserGroupManager(runner=runner, config=config)

    result = manager.set_password(user="alice", password="topsecret")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)

    write_call = runner.calls[-1]
    expected_full = [
        "sysadminctl", "-resetPasswordFor", "alice",
        "-newPassword", "topsecret",
        "-adminUser", "adminbob",
        "-adminPassword", "adminpw",
    ]
    testbase.assert_equal(write_call["args"], expected_full)

    expected_recorded = [
        "sysadminctl", "-resetPasswordFor", "alice",
        "-newPassword", "<redacted>",
        "-adminUser", "adminbob",
        "-adminPassword", "<redacted>",
    ]
    testbase.assert_equal(result.command, expected_recorded)
    return


def test_lock_account_skipped_when_already_locked():
    """
        lock_account returns SKIPPED when ``pwpolicy
        -authentication-allowed`` reports the account is already
        locked.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_account_locked(runner=runner, user="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.lock_account(user="alice")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    disable_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 4 and c["args"][3] == "-disableuser"
    ]
    testbase.assert_equal(disable_calls, [])
    return


def test_unlock_account_skipped_when_already_unlocked():
    """
        unlock_account returns SKIPPED when the account is already
        unlocked.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_account_unlocked(runner=runner, user="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.unlock_account(user="alice")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 4 and c["args"][3] == "-enableuser"
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_lock_account_dispatches_pwpolicy_disableuser():
    """
        lock_account on an unlocked account dispatches
        ``pwpolicy -u <user> -disableuser``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_account_unlocked(runner=runner, user="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.lock_account(user="alice")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)
    expected = ["pwpolicy", "-u", "alice", "-disableuser"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# create_group / delete_group
# ----------------------------------------------------------------------


def test_create_group_constructs_dseditgroup_argv():
    """
        create_group dispatches ``dseditgroup -o create [-i <gid>] [-r
        <description>] <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_missing(runner=runner, name="hackers")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    spec = MacosGroupSpec(
        name="hackers",
        gid=2001,
        description="The hacker collective",
    )
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)
    testbase.assert_equal(result.kind, MacosPrincipalKind.GROUP)
    expected = [
        "dseditgroup", "-o", "create",
        "-i", "2001",
        "-r", "The hacker collective",
        "hackers",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_delete_group_skipped_when_absent():
    """
        delete_group returns SKIPPED when the group does not exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_missing(runner=runner, name="ghosts")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.delete_group(name="ghosts")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    delete_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dseditgroup", "-o", "delete"]
    ]
    testbase.assert_equal(delete_calls, [])
    return


# ----------------------------------------------------------------------
# add_to_group / remove_from_group
# ----------------------------------------------------------------------


def test_add_to_group_skipped_when_already_member():
    """
        add_to_group returns SKIPPED when ``dseditgroup
        -o checkmember`` reports the user is already a member.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_record(runner=runner, name="developers", members=["alice"])
    _seed_dseditgroup_member(
        runner=runner, user="alice", group="developers", is_member=True,
    )

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.add_to_group(user="alice", group="developers")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    edit_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dseditgroup", "-o", "edit"]
    ]
    testbase.assert_equal(edit_calls, [])
    return


def test_add_to_group_dispatches_dseditgroup_edit():
    """
        add_to_group on a non-member dispatches the canonical
        ``dseditgroup -o edit -a <user> -t user <group>`` argv.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_record(runner=runner, name="developers", members=[])
    _seed_dseditgroup_member(
        runner=runner, user="alice", group="developers", is_member=False,
    )

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.add_to_group(user="alice", group="developers")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SUCCESS)
    expected = [
        "dseditgroup", "-o", "edit",
        "-a", "alice", "-t", "user",
        "developers",
    ]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_remove_from_group_skipped_when_not_member():
    """
        remove_from_group returns SKIPPED when ``dseditgroup
        -o checkmember`` reports the user is not a member.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_record(runner=runner, name="developers", members=[])
    _seed_dseditgroup_member(
        runner=runner, user="alice", group="developers", is_member=False,
    )

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.remove_from_group(user="alice", group="developers")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.SKIPPED)
    edit_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dseditgroup", "-o", "edit"]
    ]
    testbase.assert_equal(edit_calls, [])
    return


def test_add_to_group_failed_when_group_missing():
    """
        add_to_group returns FAILED when the group does not exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_missing(runner=runner, name="ghosts")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    result = manager.add_to_group(user="alice", group="ghosts")

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.message, "group does not exist")
    edit_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dseditgroup", "-o", "edit"]
    ]
    testbase.assert_equal(edit_calls, [])
    return


# ----------------------------------------------------------------------
# Reads — get_user / list_users / get_group / list_groups
# ----------------------------------------------------------------------


def test_get_user_parses_dscl_id_and_pwpolicy():
    """
        get_user merges ``dscl -read``, ``id -Gn``, and
        ``pwpolicy`` into a :class:`MacosUserInfo`.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(
        runner=runner, name="alice",
        uid=501, gid=20,
        home="/Users/alice", shell="/bin/zsh",
        full_name="Alice Example",
    )
    _seed_id_groups(
        runner=runner, user="alice",
        groups=["staff", "admin", "developers"],
    )
    _seed_account_locked(runner=runner, user="alice")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    info = manager.get_user(name="alice")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "alice")
    testbase.assert_equal(info.uid, 501)
    testbase.assert_equal(info.gid, 20)
    testbase.assert_equal(info.home, "/Users/alice")
    testbase.assert_equal(info.shell, "/bin/zsh")
    testbase.assert_equal(info.full_name, "Alice Example")
    testbase.assert_equal(info.groups, ["staff", "admin", "developers"])
    testbase.assert_equal(info.locked, True)
    return


def test_get_user_returns_none_when_record_missing():
    """
        get_user returns ``None`` when ``dscl . -read`` reports the
        user does not exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_missing(runner=runner, name="ghost")

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    info = manager.get_user(name="ghost")

    testbase.assert_equal(info, None)
    return


def test_list_users_filters_by_uid_range():
    """
        list_users honours the configured regular-UID range and
        materialises every retained entry via ``get_user``.
    """
    list_output = (
        "_appleeventsd        55\n"
        "alice                501\n"
        "bob                  502\n"
        "_uucp                4\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dscl", ".", "-list", "/Users", "UniqueID")] = (
        RunResult(exit_code=0, stdout=list_output, stderr="")
    )
    _seed_user_record(
        runner=runner, name="alice",
        uid=501, gid=20,
        home="/Users/alice", shell="/bin/zsh", full_name="Alice",
    )
    _seed_user_record(
        runner=runner, name="bob",
        uid=502, gid=20,
        home="/Users/bob", shell="/bin/zsh", full_name="Bob",
    )
    _seed_id_groups(runner=runner, user="alice", groups=["staff"])
    _seed_id_groups(runner=runner, user="bob", groups=["staff"])
    _seed_account_unlocked(runner=runner, user="alice")
    _seed_account_unlocked(runner=runner, user="bob")

    manager = MacosUserGroupManager(
        runner=runner,
        config=MacosUserGroupManagerConfig(
            regular_uid_min=501, regular_uid_max=65535,
        ),
    )

    users = manager.list_users()

    testbase.assert_equal(len(users), 2)
    listed_names = sorted([u.name for u in users])
    testbase.assert_equal(listed_names, ["alice", "bob"])
    return


def test_list_users_raises_query_error_on_dscl_failure():
    """
        list_users raises :class:`MacosUserGroupQueryError` when the
        ``dscl -list`` call exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("dscl", ".", "-list", "/Users", "UniqueID")] = (
        RunResult(exit_code=2, stdout="", stderr="something exploded")
    )
    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    raised = False
    try:
        manager.list_users()
    except MacosUserGroupQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_get_group_parses_dscl_read_output():
    """
        get_group parses ``dscl . -read /Groups/<name>`` into a
        :class:`MacosGroupInfo`, including the ``GroupMembership``
        field.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_group_record(
        runner=runner, name="developers",
        gid=2001, members=["alice", "bob"],
    )

    manager = MacosUserGroupManager(
        runner=runner, config=MacosUserGroupManagerConfig(),
    )

    info = manager.get_group(name="developers")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "developers")
    testbase.assert_equal(info.gid, 2001)
    testbase.assert_equal(info.members, ["alice", "bob"])
    return


# ----------------------------------------------------------------------
# Dry-run + elevation policy
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_create_user():
    """
        Dry-run short-circuits create_user without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    config = MacosUserGroupManagerConfig(
        dry_run=True,
        admin_user="adminbob",
        admin_password="adminpw",
    )
    manager = MacosUserGroupManager(runner=runner, config=config)

    spec = MacosUserSpec(name="alice", password="pw")
    result = manager.create_user(spec=spec)

    testbase.assert_equal(result.status, MacosUserGroupActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    # The recorded argv on the dry-run result must STILL be sanitised.
    testbase.assert_true("<redacted>" in result.command)
    testbase.assert_true("pw" not in result.command)
    return


def test_writes_pass_elevated_when_configured():
    """
        Writes pass ``elevated=True`` to the runner when the config
        elevates.  Reads (the existence probe) must NOT elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_missing(runner=runner, name="alice")

    config = MacosUserGroupManagerConfig(elevated=True)
    manager = MacosUserGroupManager(runner=runner, config=config)

    manager.create_user(spec=MacosUserSpec(name="alice"))

    addUser_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysadminctl", "-addUser"]
    ]
    testbase.assert_equal(len(addUser_calls), 1)
    testbase.assert_equal(addUser_calls[0]["elevated"], True)

    read_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["dscl", ".", "-read"]
    ]
    testbase.assert_true(len(read_calls) >= 1)
    for call in read_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_id_call_never_elevates():
    """
        ``id -Gn`` reads (used by get_user) never pass ``elevated=True``,
        even when the config elevates writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_user_record(runner=runner, name="alice")
    _seed_account_unlocked(runner=runner, user="alice")
    _seed_id_groups(runner=runner, user="alice", groups=["staff"])

    config = MacosUserGroupManagerConfig(elevated=True)
    manager = MacosUserGroupManager(runner=runner, config=config)

    manager.get_user(name="alice")

    id_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["id"]
    ]
    testbase.assert_equal(len(id_calls), 1)
    testbase.assert_equal(id_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Module-level parser unit tests
# ----------------------------------------------------------------------


def test_parse_dscl_read_output_handles_continuation_lines():
    """
        ``_parse_dscl_read_output`` joins indented continuation lines
        into the previous attribute's value, separated by spaces.
    """
    output = (
        "RecordName: alice\n"
        "GroupMembership:\n"
        " staff\n"
        " admin\n"
        "UniqueID: 501\n"
    )
    parsed = _parse_dscl_read_output(output=output)
    testbase.assert_equal(parsed["RecordName"], "alice")
    testbase.assert_equal(parsed["GroupMembership"], "staff admin")
    testbase.assert_equal(parsed["UniqueID"], "501")
    return


def test_parse_dscl_list_uid_line_handles_padded_columns():
    """
        ``_parse_dscl_list_uid_line`` splits the padded ``<name> <uid>``
        format into structured fields.
    """
    parsed = _parse_dscl_list_uid_line(line="alice                501")
    testbase.assert_true(parsed is not None)
    testbase.assert_equal(parsed["name"], "alice")
    testbase.assert_equal(parsed["uid"], 501)

    bad = _parse_dscl_list_uid_line(line="bogus_no_uid")
    testbase.assert_equal(bad, None)
    return


def test_parse_primary_group_id_extracts_gid():
    """
        ``_parse_primary_group_id`` returns the integer GID from a
        ``PrimaryGroupID: 20`` style block.
    """
    gid = _parse_primary_group_id(output="PrimaryGroupID: 20\n")
    testbase.assert_equal(gid, 20)

    missing = _parse_primary_group_id(output="UniqueID: 501\n")
    testbase.assert_equal(missing, None)
    return


def test_materialise_user_attributes_defaults_on_missing_uid():
    """
        ``_materialise_user_attributes`` falls back to ``0`` when the
        UID / GID attributes cannot be parsed.
    """
    materialised = _materialise_user_attributes(
        attributes={"RecordName": "alice"},
        fallback_name="alice",
    )
    testbase.assert_equal(materialised["uid"], 0)
    testbase.assert_equal(materialised["gid"], 0)
    testbase.assert_equal(materialised["name"], "alice")
    return


# ----------------------------------------------------------------------
# Smoke tests — ensure the dataclasses compose
# ----------------------------------------------------------------------


def test_user_info_dataclass_round_trip():
    """
        :class:`MacosUserInfo` accepts every documented field and
        defaults the optional ones.
    """
    info = MacosUserInfo(
        name="alice",
        uid=501,
        gid=20,
        home="/Users/alice",
        shell="/bin/zsh",
        full_name="Alice",
    )
    testbase.assert_equal(info.groups, [])
    testbase.assert_equal(info.locked, False)
    return


def test_group_info_dataclass_round_trip():
    """
        :class:`MacosGroupInfo` accepts every documented field and
        defaults the optional ones.
    """
    info = MacosGroupInfo(name="developers", gid=2001)
    testbase.assert_equal(info.members, [])
    return
