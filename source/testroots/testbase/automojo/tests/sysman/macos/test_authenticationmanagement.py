"""
    Tests for :class:`MacosAuthenticationManager`.

    A local ``_MockCommandRunner`` records every ``run`` call and
    returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    All file I/O (``cat`` / ``tee``) is exercised entirely through
    the runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.authenticationmanagement import (
    MACOS_AUTH_LOGIN,
    MACOS_AUTH_SUDO,
    MacosAuthActionStatus,
    MacosAuthConfig,
    MacosAuthEntry,
    MacosAuthenticationManager,
    MacosAuthenticationManagerConfig,
    MacosAuthenticationQueryError,
    MacosAuthenticationSubsystemNotFoundError,
    _parse_pam_stack,
    _render_pam_stack,
    _toggle_module_state_in_content,
)


# ----------------------------------------------------------------------
# Mock runner
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
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failing :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_SAMPLE_PAM_LOGIN = (
    "# /etc/pam.d/login\n"
    "auth       required   pam_securetty.so\n"
    "auth       requisite  pam_nologin.so\n"
    "@include   common-auth\n"
    "account    required   pam_unix.so\n"
    "password   [success=ok default=ignore]   pam_unix.so try_first_pass\n"
    "session    optional   pam_motd.so\n"
)


_SAMPLE_PAM_SUDO = (
    "auth       sufficient pam_smartcard.so\n"
    "auth       required   pam_opendirectory.so\n"
    "account    required   pam_permit.so\n"
    "# password   required   pam_deny.so\n"
    "session    required   pam_permit.so\n"
)


_SAMPLE_PAM_SU = (
    "auth       sufficient pam_rootok.so\n"
    "auth       required   pam_opendirectory.so\n"
    "session    required   pam_permit.so\n"
)


_SAMPLE_PAM_SSHD = (
    "auth       required   pam_opendirectory.so\n"
    "session    required   pam_permit.so\n"
)


_SAMPLE_PAM_CRON = (
    "auth       required   pam_permit.so\n"
    "session    required   pam_permit.so\n"
)


def _build_default_runner() -> _MockCommandRunner:
    """
        Build a runner pre-seeded with canonical PAM service files.

        :returns: The mock runner.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pam.d/login")] = _ok(
        stdout=_SAMPLE_PAM_LOGIN,
    )
    runner.results[("cat", "/etc/pam.d/sudo")] = _ok(
        stdout=_SAMPLE_PAM_SUDO,
    )
    runner.results[("cat", "/etc/pam.d/su")] = _ok(
        stdout=_SAMPLE_PAM_SU,
    )
    runner.results[("cat", "/etc/pam.d/sshd")] = _ok(
        stdout=_SAMPLE_PAM_SSHD,
    )
    runner.results[("cat", "/etc/pam.d/cron")] = _ok(
        stdout=_SAMPLE_PAM_CRON,
    )
    return runner


def _build_manager(
    *,
    runner: _MockCommandRunner,
    dry_run: bool = False,
    elevated: bool = True,
) -> MacosAuthenticationManager:
    """
        Build a manager with the supplied runner and config flags.

        :returns: The manager.
    """
    config = MacosAuthenticationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
    )
    rtnval = MacosAuthenticationManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# Test cases
# ----------------------------------------------------------------------


def test_get_auth_config_parses_cat_output():
    """
        ``get_auth_config`` parses ``cat`` output into a structured
        :class:`MacosAuthConfig`.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.get_auth_config(subsystem=MACOS_AUTH_LOGIN)

    testbase.assert_equal(result.subsystem, "login")
    testbase.assert_equal(len(result.entries), 6)
    testbase.assert_equal(result.entries[0].module_type, "auth")
    testbase.assert_equal(result.entries[0].control, "required")
    testbase.assert_equal(result.entries[0].module_path, "pam_securetty.so")
    return


def test_get_auth_config_raises_subsystem_not_found_when_missing():
    """
        ``get_auth_config`` raises when the file is missing.
    """
    runner = _build_default_runner()
    runner.results[("cat", "/etc/pam.d/missing")] = _fail(
        stderr="cat: /etc/pam.d/missing: No such file or directory",
        exit_code=1,
    )
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.get_auth_config(subsystem="missing")
    except MacosAuthenticationSubsystemNotFoundError:
        raised = True
    testbase.assert_true(raised)
    return


def test_set_auth_config_invokes_tee():
    """
        ``set_auth_config`` invokes ``tee`` with the rendered content
        on stdin and ``elevated=True``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    new_config = MacosAuthConfig(
        subsystem="login",
        entries=[
            MacosAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=MACOS_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, MacosAuthActionStatus.SUCCESS)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["tee", "/etc/pam.d/login"],
    )
    testbase.assert_true(write_calls[0]["elevated"])
    testbase.assert_equal(
        write_calls[0]["stdin"], "auth required pam_unix.so\n",
    )
    return


def test_set_auth_config_skipped_when_match():
    """
        ``set_auth_config`` returns ``SKIPPED`` when the rendered
        content equals the live file content.
    """
    runner = _MockCommandRunner()
    rendered = "auth required pam_unix.so\n"
    runner.results[("cat", "/etc/pam.d/login")] = _ok(stdout=rendered)
    manager = _build_manager(runner=runner)

    config = MacosAuthConfig(
        subsystem="login",
        entries=[
            MacosAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=MACOS_AUTH_LOGIN, config=config,
    )

    testbase.assert_equal(result.status, MacosAuthActionStatus.SKIPPED)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    return


def test_set_auth_config_dry_run():
    """
        ``set_auth_config`` returns ``DRY_RUN`` when dry-run is
        enabled.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, dry_run=True)

    new_config = MacosAuthConfig(
        subsystem="login",
        entries=[
            MacosAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=MACOS_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, MacosAuthActionStatus.DRY_RUN)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    testbase.assert_equal(result.command, ["tee", "/etc/pam.d/login"])
    return


def test_list_auth_methods_parses_pam_stack_output():
    """
        ``list_auth_methods`` walks the canonical subsystems and
        aggregates referenced modules.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    methods = manager.list_auth_methods()
    names = [info.name for info in methods]

    testbase.assert_true("pam_unix.so" in names)
    testbase.assert_true("pam_opendirectory.so" in names)
    testbase.assert_true("pam_smartcard.so" in names)

    # pam_deny.so is comment-prefixed in /etc/pam.d/sudo, so it
    # should be enabled=False.
    deny_info = [info for info in methods if info.name == "pam_deny.so"]
    testbase.assert_equal(len(deny_info), 1)
    testbase.assert_false(deny_info[0].enabled)

    # pam_unix.so is active in /etc/pam.d/login.
    unix_info = [info for info in methods if info.name == "pam_unix.so"]
    testbase.assert_equal(len(unix_info), 1)
    testbase.assert_true(unix_info[0].enabled)
    return


def test_enable_method_invokes_tee():
    """
        ``enable_method`` invokes ``tee`` with the un-commented
        content for any file that contains comment-only references.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="pam_deny.so")

    testbase.assert_equal(result.status, MacosAuthActionStatus.SUCCESS)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["tee", "/etc/pam.d/sudo"])
    testbase.assert_true(write_calls[0]["elevated"])
    testbase.assert_false("# password" in write_calls[0]["stdin"])
    return


def test_enable_method_skipped_when_enabled():
    """
        ``enable_method`` returns ``SKIPPED`` when the module is
        already active in every reference.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="pam_unix.so")

    testbase.assert_equal(result.status, MacosAuthActionStatus.SKIPPED)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    return


def test_enable_method_raises_when_not_found():
    """
        ``enable_method`` raises
        :class:`MacosAuthenticationSubsystemNotFoundError` when the
        module is not present in any canonical subsystem.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.enable_method(name="pam_ghost.so")
    except MacosAuthenticationSubsystemNotFoundError:
        raised = True
    testbase.assert_true(raised)
    return


def test_disable_method_invokes_tee():
    """
        ``disable_method`` invokes ``tee`` with content that has the
        module commented out.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="pam_unix.so")

    testbase.assert_equal(result.status, MacosAuthActionStatus.SUCCESS)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    # pam_unix.so appears twice in /etc/pam.d/login (account &
    # password), so the login file gets one tee.
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["tee", "/etc/pam.d/login"],
    )
    testbase.assert_true(write_calls[0]["elevated"])
    return


def test_disable_method_skipped_when_disabled():
    """
        ``disable_method`` returns ``SKIPPED`` when the module is
        already commented or absent.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="pam_deny.so")

    testbase.assert_equal(result.status, MacosAuthActionStatus.SKIPPED)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    return


def test_validate_auth_config_returns_result():
    """
        ``validate_auth_config`` returns a populated
        :class:`MacosAuthValidationResult` for a clean stack.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.validate_auth_config(subsystem=MACOS_AUTH_LOGIN)

    testbase.assert_true(result.valid)
    return


def test_writes_elevate():
    """
        ``set_auth_config``, ``enable_method``, and ``disable_method``
        invoke the runner with ``elevated=True``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.set_auth_config(
        subsystem=MACOS_AUTH_SUDO,
        config=MacosAuthConfig(
            subsystem="sudo",
            entries=[
                MacosAuthEntry(
                    module_type="auth",
                    control="required",
                    module_path="pam_unix.so",
                    args=["nullok"],
                ),
            ],
        ),
    )

    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    for call in write_calls:
        testbase.assert_true(call["elevated"])
    return


def test_reads_never_elevate():
    """
        Reads invoke the runner with ``elevated=False``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.get_auth_config(subsystem=MACOS_AUTH_LOGIN)
    manager.list_auth_methods()
    manager.validate_auth_config(subsystem=MACOS_AUTH_LOGIN)

    for call in runner.calls:
        args = call["args"]
        if args[0] == "cat":
            testbase.assert_false(call["elevated"])
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero runner exit on a write surfaces as
        ``status=FAILED`` with ``stderr`` populated.
    """
    runner = _build_default_runner()
    runner.results[("tee", "/etc/pam.d/login")] = _fail(
        stderr="tee: permission denied",
        exit_code=1,
    )
    manager = _build_manager(runner=runner)

    new_config = MacosAuthConfig(
        subsystem="login",
        entries=[
            MacosAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=MACOS_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, MacosAuthActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in result.stderr)
    return


def test_authz_database_documented_as_oos():
    """
        The macOS authorization database surface is documented as
        out-of-scope.  The pilot exposes ``authz_path`` on the
        manager config but does NOT read or write it on any verb.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    # Exercise every read verb.
    manager.get_auth_config(subsystem=MACOS_AUTH_LOGIN)
    manager.list_auth_methods()
    manager.validate_auth_config(subsystem=MACOS_AUTH_LOGIN)

    # No call should reference the authz_path or its dirname.
    for call in runner.calls:
        for arg in call["args"]:
            testbase.assert_false("authorization" in arg.lower())
            testbase.assert_false("authorizationdb" in arg.lower())

    # And the config field itself must exist for forward compat.
    testbase.assert_equal(manager.config.authz_path, "/etc/authorization")
    return


def test_pam_stack_parser_handles_includes():
    """
        ``_parse_pam_stack`` produces a single ``@include`` entry for
        an ``@include common-auth`` directive.
    """
    content = "@include common-auth\nauth required pam_unix.so\n"
    entries = _parse_pam_stack(content=content)

    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(entries[0].module_type, "@include")
    testbase.assert_equal(entries[0].module_path, "common-auth")
    return


def test_render_round_trip_is_stable():
    """
        Parse / render round-trip is stable.
    """
    original = (
        "auth required pam_unix.so\n"
        "@include common-auth\n"
        "password [success=ok default=ignore] pam_unix.so try_first_pass\n"
    )
    entries = _parse_pam_stack(content=original)
    rendered = _render_pam_stack(entries=entries)
    re_parsed = _parse_pam_stack(content=rendered)

    testbase.assert_equal(len(entries), len(re_parsed))
    for original_entry, parsed_entry in zip(entries, re_parsed):
        testbase.assert_equal(
            original_entry.module_type, parsed_entry.module_type,
        )
        testbase.assert_equal(
            original_entry.control, parsed_entry.control,
        )
        testbase.assert_equal(
            original_entry.module_path, parsed_entry.module_path,
        )
        testbase.assert_equal(original_entry.args, parsed_entry.args)
    return


def test_toggle_module_state_is_idempotent():
    """
        ``_toggle_module_state_in_content`` returns unchanged content
        when the module is already in the requested state.
    """
    content = "auth required pam_unix.so\n"
    new_content, found = _toggle_module_state_in_content(
        content=content,
        module_name="pam_unix.so",
        enable=True,
    )
    testbase.assert_equal(new_content, content)
    testbase.assert_true(found)
    return


def test_query_error_raised_for_unexpected_failure():
    """
        A ``cat`` failure with non-"not found" stderr surfaces as
        :class:`MacosAuthenticationQueryError`.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pam.d/login")] = _fail(
        stderr="cat: I/O error reading file",
        exit_code=2,
    )
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.get_auth_config(subsystem=MACOS_AUTH_LOGIN)
    except MacosAuthenticationQueryError:
        raised = True
    testbase.assert_true(raised)
    return
