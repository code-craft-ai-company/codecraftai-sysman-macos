"""
    Tests for :class:`MacosAclManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``ls`` / ``chmod`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.aclmanagement import (
    MacosAceSpec,
    MacosAcePrincipalKind,
    MacosAcePermission,
    MacosAclActionStatus,
    MacosAclManager,
    MacosAclManagerConfig,
    MacosAclQueryError,
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


def _ls_le_full_payload() -> str:
    """
        Build a representative ``ls -lde`` payload for a directory
        carrying three extended ACEs (one allow, one allow on a group,
        one deny).
    """
    rtnval = (
        "drwxr-xr-x+ 1 alice staff 64 Jan  1 12:00 /srv/data\n"
        " 0: user:alice allow read,write,execute\n"
        " 1: group:wheel allow read,execute\n"
        " 2: user:bob deny delete\n"
    )
    return rtnval


def _ls_le_minimal_payload() -> str:
    """
        Build a representative ``ls -lde`` payload with no extended
        ACEs.
    """
    rtnval = (
        "drwxr-xr-x 1 root wheel 64 Jan  1 12:00 /srv/data\n"
    )
    return rtnval


def _seed_full_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed ``ls -lde <path>`` to return the full payload above.
    """
    runner.results[("ls", "-lde", path)] = RunResult(
        exit_code=0,
        stdout=_ls_le_full_payload(),
        stderr="",
    )
    return


def _seed_minimal_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed ``ls -lde <path>`` to return the minimal payload.
    """
    runner.results[("ls", "-lde", path)] = RunResult(
        exit_code=0,
        stdout=_ls_le_minimal_payload(),
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# get_acl / list_aces
# ----------------------------------------------------------------------


def test_get_acl_parses_ls_le_output():
    """
        get_acl returns a MacosAclInfo populated from the parsed
        ls -le output.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(),
    )
    info = manager.get_acl(path="/srv/data")

    testbase.assert_equal(info.path, "/srv/data")
    testbase.assert_equal(info.owner, "alice")
    testbase.assert_equal(info.group, "staff")
    testbase.assert_equal(len(info.aces), 3)

    first = info.aces[0]
    testbase.assert_equal(first.principal_kind, MacosAcePrincipalKind.USER)
    testbase.assert_equal(first.principal, "alice")
    testbase.assert_equal(first.permission, MacosAcePermission.ALLOW)
    testbase.assert_equal(first.rights, ["read", "write", "execute"])
    testbase.assert_equal(first.index, 0)

    deny_ace = info.aces[2]
    testbase.assert_equal(deny_ace.permission, MacosAcePermission.DENY)
    testbase.assert_equal(deny_ace.principal, "bob")
    return


def test_list_aces_returns_full_set():
    """
        list_aces returns all parsed ACEs as a flat list.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(),
    )
    aces = manager.list_aces(path="/srv/data")

    testbase.assert_equal(len(aces), 3)
    return


# ----------------------------------------------------------------------
# set_acl
# ----------------------------------------------------------------------


def test_set_acl_invokes_chmod():
    """
        set_acl dispatches chmod -N followed by chmod +a per ACE.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = [
        MacosAceSpec(
            principal="alice",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.ALLOW,
            rights=["read", "write", "execute"],
        ),
        MacosAceSpec(
            principal="wheel",
            principal_kind=MacosAcePrincipalKind.GROUP,
            permission=MacosAcePermission.ALLOW,
            rights=["read", "execute"],
        ),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SUCCESS)

    chmod_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    # One -N clear, two +a writes.
    testbase.assert_equal(len(chmod_calls), 3)
    testbase.assert_equal(chmod_calls[0]["args"], ["chmod", "-N", "/srv/data"])
    testbase.assert_equal(
        chmod_calls[1]["args"],
        ["chmod", "+a", "user:alice allow read,write,execute", "/srv/data"],
    )
    testbase.assert_equal(
        chmod_calls[2]["args"],
        ["chmod", "+a", "group:wheel allow read,execute", "/srv/data"],
    )
    for call in chmod_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_set_acl_skipped_when_match():
    """
        set_acl returns SKIPPED when the parsed ACL already matches.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = [
        MacosAceSpec(
            principal="alice",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.ALLOW,
            rights=["read", "write", "execute"],
        ),
        MacosAceSpec(
            principal="wheel",
            principal_kind=MacosAcePrincipalKind.GROUP,
            permission=MacosAcePermission.ALLOW,
            rights=["read", "execute"],
        ),
        MacosAceSpec(
            principal="bob",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.DENY,
            rights=["delete"],
        ),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SKIPPED)
    chmod_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(chmod_calls, [])
    return


def test_set_acl_dry_run_short_circuits():
    """
        set_acl returns DRY_RUN without dispatching any chmod when
        config.dry_run is True.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(dry_run=True, elevated=True),
    )
    spec = [
        MacosAceSpec(
            principal="alice",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.ALLOW,
            rights=["read"],
        ),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# add_ace
# ----------------------------------------------------------------------


def test_add_ace_invokes_chmod():
    """
        add_ace dispatches chmod +a "<rendered>" <path>.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = MacosAceSpec(
        principal="charlie",
        principal_kind=MacosAcePrincipalKind.USER,
        permission=MacosAcePermission.ALLOW,
        rights=["read", "write"],
    )
    result = manager.add_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "chmod" and c["args"][1] == "+a"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["chmod", "+a", "user:charlie allow read,write", "/srv/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_add_ace_skipped_when_present():
    """
        add_ace returns SKIPPED when the same ACE is already present.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = MacosAceSpec(
        principal="alice",
        principal_kind=MacosAcePrincipalKind.USER,
        permission=MacosAcePermission.ALLOW,
        rights=["read", "write", "execute"],
    )
    result = manager.add_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# remove_ace
# ----------------------------------------------------------------------


def test_remove_ace_invokes_chmod():
    """
        remove_ace dispatches chmod -a "<rendered>" <path> for each
        matching ACE in the parsed ACL.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = MacosAceSpec(
        principal="alice",
        principal_kind=MacosAcePrincipalKind.USER,
        permission=MacosAcePermission.ALLOW,
        rights=[],
    )
    result = manager.remove_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SUCCESS)
    remove_calls = [
        c for c in runner.calls
        if c["args"][0] == "chmod" and c["args"][1] == "-a"
    ]
    testbase.assert_equal(len(remove_calls), 1)
    testbase.assert_equal(
        remove_calls[0]["args"],
        ["chmod", "-a", "user:alice allow read,write,execute", "/srv/data"],
    )
    testbase.assert_equal(remove_calls[0]["elevated"], True)
    return


def test_remove_ace_skipped_when_absent():
    """
        remove_ace returns SKIPPED when no ACE matches the principal.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    spec = MacosAceSpec(
        principal="ghost",
        principal_kind=MacosAcePrincipalKind.USER,
        permission=MacosAcePermission.ALLOW,
        rights=[],
    )
    result = manager.remove_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, MacosAclActionStatus.SKIPPED)
    remove_calls = [
        c for c in runner.calls if c["args"][0] == "chmod"
    ]
    testbase.assert_equal(remove_calls, [])
    return


# ----------------------------------------------------------------------
# clear_acl
# ----------------------------------------------------------------------


def test_clear_acl_invokes_chmod():
    """
        clear_acl dispatches chmod -N <path> when ACEs exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    result = manager.clear_acl(path="/srv/data")

    testbase.assert_equal(result.status, MacosAclActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["chmod", "-N", "/srv/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_clear_acl_skipped_when_already_clear():
    """
        clear_acl returns SKIPPED when the parsed ACL has zero ACEs.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    result = manager.clear_acl(path="/srv/data")

    testbase.assert_equal(result.status, MacosAclActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# copy_acl
# ----------------------------------------------------------------------


def test_copy_acl_invokes_chmod():
    """
        copy_acl reads source and dispatches chmod -N + chmod +a per
        ACE on the destination.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/source")
    _seed_minimal_acl(runner, "/srv/dest")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="/srv/source", dst="/srv/dest")

    testbase.assert_equal(result.status, MacosAclActionStatus.SUCCESS)

    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "chmod" and c["args"][-1] == "/srv/dest"
    ]
    # One -N clear, three +a writes (matching the three ACEs in the
    # full payload).
    testbase.assert_equal(len(write_calls), 4)
    testbase.assert_equal(write_calls[0]["args"][1], "-N")
    for write_call in write_calls[1:]:
        testbase.assert_equal(write_call["args"][1], "+a")
        testbase.assert_equal(write_call["elevated"], True)
    return


def test_copy_acl_skipped_when_match():
    """
        copy_acl returns SKIPPED when the destination ACL already
        matches the source ACL.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/source")
    _seed_full_acl(runner, "/srv/dest")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="/srv/source", dst="/srv/dest")

    testbase.assert_equal(result.status, MacosAclActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation, error surface, contract corners
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes elevated=True to the runner when the
        manager is configured with elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    manager.add_ace(
        path="/srv/data",
        principal=MacosAceSpec(
            principal="alice",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.ALLOW,
            rights=["read"],
        ),
    )

    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "chmod" and c["args"][1] == "+a"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Every read verb passes elevated=False to the runner regardless
        of config.elevated.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=True),
    )
    manager.get_acl(path="/srv/data")
    manager.list_aces(path="/srv/data")

    read_calls = [c for c in runner.calls if c["args"][0] == "ls"]
    testbase.assert_not_equal(len(read_calls), 0)
    for call in read_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write surfaces FAILED with the runner's
        captured stderr / exit_code on the result.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")
    failure = RunResult(
        exit_code=1,
        stdout="",
        stderr="chmod: Operation not permitted",
    )
    runner.results[(
        "chmod",
        "+a",
        "user:alice allow read",
        "/srv/data",
    )] = failure

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(elevated=False),
    )
    result = manager.add_ace(
        path="/srv/data",
        principal=MacosAceSpec(
            principal="alice",
            principal_kind=MacosAcePrincipalKind.USER,
            permission=MacosAcePermission.ALLOW,
            rights=["read"],
        ),
    )

    testbase.assert_equal(result.status, MacosAclActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "chmod: Operation not permitted")
    return


def test_get_acl_raises_on_failure():
    """
        get_acl raises MacosAclQueryError when ls -le exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-lde", "/missing")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="ls: /missing: No such file or directory",
    )

    manager = MacosAclManager(
        runner=runner,
        config=MacosAclManagerConfig(),
    )
    raised = False
    try:
        manager.get_acl(path="/missing")
    except MacosAclQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
