"""
    Tests for :class:`MacosScheduledTasksManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``launchctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.scheduledtasks import (
    MacosScheduledTaskActionStatus,
    MacosScheduledTaskQueryError,
    MacosScheduledTasksManager,
    MacosScheduledTasksManagerConfig,
    MacosTaskInfo,
    MacosTaskOwner,
    MacosTaskSpec,
    _extract_program_arguments_from_plist,
    _extract_schedule_from_plist,
    _parse_print_disabled,
    _parse_print_output,
    _render_plist_body,
    _translate_schedule,
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
# Helpers
# ----------------------------------------------------------------------


def _system_spec(
    *,
    name: str = "com.example.task",
    schedule: str = "daily 02:00",
) -> MacosTaskSpec:
    """
        Build a representative SYSTEM-owner spec.
    """
    rtnval = MacosTaskSpec(
        name=name,
        command=["/usr/local/bin/example", "--flag"],
        schedule=schedule,
        owner=MacosTaskOwner.SYSTEM,
    )
    return rtnval


def _user_spec(
    *,
    name: str = "com.example.task",
    schedule: str = "every 5min",
) -> MacosTaskSpec:
    """
        Build a representative USER-owner spec.
    """
    rtnval = MacosTaskSpec(
        name=name,
        command=["/usr/local/bin/example"],
        schedule=schedule,
        owner=MacosTaskOwner.USER,
    )
    return rtnval


# ----------------------------------------------------------------------
# Schedule translation
# ----------------------------------------------------------------------


def test_translate_schedule_daily_form():
    """
        ``_translate_schedule`` converts ``"daily HH:MM"`` into the
        calendar dict.
    """
    parsed = _translate_schedule(schedule="daily 02:00")
    testbase.assert_equal(parsed["kind"], "calendar")
    testbase.assert_equal(parsed["Hour"], 2)
    testbase.assert_equal(parsed["Minute"], 0)
    return


def test_translate_schedule_every_min_form():
    """
        ``_translate_schedule`` converts ``"every Nmin"`` into a
        seconds interval.
    """
    parsed = _translate_schedule(schedule="every 5min")
    testbase.assert_equal(parsed["kind"], "interval")
    testbase.assert_equal(parsed["seconds"], 300)
    return


def test_translate_schedule_rejects_unrecognised_input():
    """
        ``_translate_schedule`` raises ``ValueError`` on bad input.
    """
    raised = False
    try:
        _translate_schedule(schedule="every wednesday")
    except ValueError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# Plist rendering
# ----------------------------------------------------------------------


def test_render_plist_body_emits_label_and_program_arguments():
    """
        ``_render_plist_body`` emits the canonical Label and
        ProgramArguments keys for a SYSTEM spec.
    """
    spec = _system_spec(name="com.example.task")
    schedule = _translate_schedule(schedule=spec.schedule)
    body = _render_plist_body(spec=spec, schedule_dict=schedule)
    testbase.assert_true("<key>Label</key>" in body)
    testbase.assert_true("<string>com.example.task</string>" in body)
    testbase.assert_true("<key>ProgramArguments</key>" in body)
    testbase.assert_true(
        "<string>/usr/local/bin/example</string>" in body,
    )
    return


def test_render_plist_body_calendar_form_for_daily_schedule():
    """
        ``"daily 02:00"`` renders into a ``StartCalendarInterval``
        dict with ``Hour=2`` and ``Minute=0``.
    """
    spec = _system_spec(schedule="daily 02:00")
    schedule = _translate_schedule(schedule=spec.schedule)
    body = _render_plist_body(spec=spec, schedule_dict=schedule)
    testbase.assert_true("<key>StartCalendarInterval</key>" in body)
    testbase.assert_true("<key>Hour</key>" in body)
    testbase.assert_true("<integer>2</integer>" in body)
    testbase.assert_true("<key>Minute</key>" in body)
    testbase.assert_true("<integer>0</integer>" in body)
    return


def test_render_plist_body_interval_form_for_every_5min():
    """
        ``"every 5min"`` renders into a ``StartInterval`` integer of
        300 seconds.
    """
    spec = _user_spec(schedule="every 5min")
    schedule = _translate_schedule(schedule=spec.schedule)
    body = _render_plist_body(spec=spec, schedule_dict=schedule)
    testbase.assert_true("<key>StartInterval</key>" in body)
    testbase.assert_true("<integer>300</integer>" in body)
    return


# ----------------------------------------------------------------------
# create_task
# ----------------------------------------------------------------------


def test_create_task_writes_plist_and_dispatches_bootstrap():
    """
        ``create_task`` writes the plist via ``tee`` and then
        dispatches ``launchctl bootstrap``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=1, stdout="", stderr="No such file\n")
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=_system_spec())
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SUCCESS,
    )

    tee_calls = [
        c for c in runner.calls
        if c["args"] == [
            "tee", "/Library/LaunchDaemons/com.example.task.plist",
        ]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true(
        "<key>Label</key>" in (tee_calls[0]["stdin"] or ""),
    )

    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(
        bootstrap_calls[0]["args"],
        [
            "launchctl", "bootstrap", "system",
            "/Library/LaunchDaemons/com.example.task.plist",
        ],
    )
    return


def test_create_task_skipped_when_plist_matches_and_enabled():
    """
        ``create_task`` returns SKIPPED when the on-disk plist
        already matches the rendered body byte-for-byte AND the task
        is enabled.
    """
    spec = _system_spec()
    schedule_dict = _translate_schedule(schedule=spec.schedule)
    rendered = _render_plist_body(spec=spec, schedule_dict=schedule_dict)

    runner = _MockCommandRunner()
    runner.results[("cat", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=0, stdout=rendered, stderr="")
    )
    runner.results[("launchctl", "print-disabled", "system")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SKIPPED,
    )
    tee_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(tee_calls, [])
    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(bootstrap_calls, [])
    return


def test_create_task_failed_when_schedule_invalid():
    """
        ``create_task`` returns FAILED with no runner calls when the
        schedule string is unrecognised.
    """
    runner = _MockCommandRunner()
    spec = _system_spec(schedule="every wednesday")
    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=spec)
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_schedule")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_create_task_dry_run_does_not_invoke_runner():
    """
        Dry-run short-circuits ``create_task`` without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(dry_run=True),
    )
    result = manager.create_task(spec=_system_spec())
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_task
# ----------------------------------------------------------------------


def test_delete_task_dispatches_bootout_then_rm():
    """
        ``delete_task`` dispatches ``launchctl bootout`` and then
        ``rm -f`` on the plist file.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("test", "-e", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=0, stdout="", stderr="")
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SUCCESS,
    )

    bootout_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootout"]
    ]
    testbase.assert_equal(len(bootout_calls), 1)
    testbase.assert_equal(
        bootout_calls[0]["args"],
        ["launchctl", "bootout", "system/com.example.task"],
    )

    rm_calls = [c for c in runner.calls if c["args"][:1] == ["rm"]]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"],
        ["rm", "-f", "/Library/LaunchDaemons/com.example.task.plist"],
    )
    return


def test_delete_task_skipped_when_plist_absent():
    """
        ``delete_task`` returns SKIPPED without invoking ``launchctl``
        when the plist file is absent.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SKIPPED,
    )
    bootout_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootout"]
    ]
    testbase.assert_equal(bootout_calls, [])
    rm_calls = [c for c in runner.calls if c["args"][:1] == ["rm"]]
    testbase.assert_equal(rm_calls, [])
    return


# ----------------------------------------------------------------------
# enable_task / disable_task
# ----------------------------------------------------------------------


def test_enable_task_dispatches_launchctl_enable():
    """
        ``enable_task`` dispatches ``launchctl enable
        <domain>/<name>`` when the task is currently disabled.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("launchctl", "print-disabled", "system")] = RunResult(
        exit_code=0,
        stdout='"com.example.task" => true\n',
        stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SUCCESS,
    )
    expected = ["launchctl", "enable", "system/com.example.task"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


def test_enable_task_skipped_when_already_enabled():
    """
        ``enable_task`` returns SKIPPED when the label is not in the
        domain's disabled set.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "print-disabled", "system")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SKIPPED,
    )
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "enable"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_task_skipped_when_already_disabled():
    """
        ``disable_task`` returns SKIPPED when the label is already in
        the domain's disabled set.
    """
    runner = _MockCommandRunner()
    runner.results[("launchctl", "print-disabled", "system")] = RunResult(
        exit_code=0,
        stdout='"com.example.task" => true\n',
        stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.disable_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SKIPPED,
    )
    disable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "disable"]
    ]
    testbase.assert_equal(disable_calls, [])
    return


# ----------------------------------------------------------------------
# run_task_now
# ----------------------------------------------------------------------


def test_run_task_now_dispatches_launchctl_kickstart_without_k_flag():
    """
        ``run_task_now`` dispatches ``launchctl kickstart`` WITHOUT
        the ``-k`` flag — we want to run, not restart.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.run_task_now(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SUCCESS,
    )
    expected = ["launchctl", "kickstart", "system/com.example.task"]
    testbase.assert_equal(runner.calls[-1]["args"], expected)
    return


# ----------------------------------------------------------------------
# get_task / list_tasks
# ----------------------------------------------------------------------


def test_get_task_returns_populated_info_when_plist_and_print_succeed():
    """
        ``get_task`` populates the :class:`MacosTaskInfo` from the
        plist body and the parsed ``launchctl print`` output.
    """
    spec = _system_spec(name="com.example.task", schedule="daily 02:00")
    schedule_dict = _translate_schedule(schedule=spec.schedule)
    rendered = _render_plist_body(spec=spec, schedule_dict=schedule_dict)

    runner = _MockCommandRunner()
    runner.results[("cat", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=0, stdout=rendered, stderr="")
    )
    runner.results[("launchctl", "print", "system/com.example.task")] = (
        RunResult(
            exit_code=0,
            stdout=(
                "system/com.example.task = {\n"
                "\tstate = running\n"
                "\tlast exit code = 0\n"
                "}\n"
            ),
            stderr="",
        )
    )
    runner.results[("launchctl", "print-disabled", "system")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    info = manager.get_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "com.example.task")
    testbase.assert_equal(info.active, True)
    testbase.assert_equal(info.enabled, True)
    testbase.assert_equal(info.schedule, "daily 02:00")
    testbase.assert_equal(
        info.program_arguments,
        ["/usr/local/bin/example", "--flag"],
    )
    return


def test_get_task_returns_none_when_plist_and_domain_unknown():
    """
        ``get_task`` returns None when neither the plist nor the
        launchd domain knows about the label.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=1, stdout="", stderr="No such file\n")
    )
    runner.results[("launchctl", "print", "system/com.example.task")] = (
        RunResult(
            exit_code=1,
            stdout="",
            stderr="Could not find service com.example.task\n",
        )
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    info = manager.get_task(
        name="com.example.task", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(info, None)
    return


def test_list_tasks_enumerates_plist_directory():
    """
        ``list_tasks`` enumerates the plist directory via ``ls -1``
        and surfaces one info per ``.plist`` entry.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("ls", "-1", "/Library/LaunchDaemons")] = RunResult(
        exit_code=0,
        stdout=(
            "com.example.one.plist\n"
            "com.example.two.plist\n"
            "README.txt\n"
        ),
        stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    listed = manager.list_tasks(owner=MacosTaskOwner.SYSTEM)
    names = [info.name for info in listed]
    testbase.assert_equal(
        sorted(names),
        ["com.example.one", "com.example.two"],
    )
    return


# ----------------------------------------------------------------------
# Owner-scope mapping
# ----------------------------------------------------------------------


def test_user_owner_uses_user_plist_dir_and_gui_domain():
    """
        USER-owner verbs target ``/Users/<user>/Library/LaunchAgents/``
        and the ``gui/<uid>`` launchd domain.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat",
        "/Users/alice/Library/LaunchAgents/com.example.task.plist",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(
            target_user="alice", target_uid=501,
        ),
    )
    result = manager.create_task(spec=_user_spec())
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.SUCCESS,
    )
    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(
        bootstrap_calls[0]["args"],
        [
            "launchctl", "bootstrap", "gui/501",
            "/Users/alice/Library/LaunchAgents/com.example.task.plist",
        ],
    )
    return


def test_user_owner_without_target_user_returns_failed():
    """
        A USER-owner write without a configured ``target_user`` /
        ``target_uid`` returns FAILED with ``scope_misconfigured``.
    """
    runner = _MockCommandRunner()
    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=_user_spec())
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "scope_misconfigured")
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_system_owner_writes_pass_elevated_when_configured():
    """
        SYSTEM-owner writes pass ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/Library/LaunchDaemons/com.example.task.plist")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(elevated=True),
    )
    manager.create_task(spec=_system_spec())

    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)

    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(bootstrap_calls[0]["elevated"], True)
    return


def test_user_owner_writes_never_elevate():
    """
        USER-owner writes never pass ``elevated=True`` even when the
        config is configured to elevate system writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat",
        "/Users/alice/Library/LaunchAgents/com.example.task.plist",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(
            elevated=True, target_user="alice", target_uid=501,
        ),
    )
    manager.create_task(spec=_user_spec())

    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], False)

    bootstrap_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["launchctl", "bootstrap"]
    ]
    testbase.assert_equal(len(bootstrap_calls), 1)
    testbase.assert_equal(bootstrap_calls[0]["elevated"], False)
    return


def test_reads_never_elevate():
    """
        ``get_task`` and ``list_tasks`` never pass ``elevated=True``
        even when the config is configured to elevate writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("ls", "-1", "/Library/LaunchDaemons")] = RunResult(
        exit_code=0, stdout="com.example.task.plist\n", stderr="",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(elevated=True),
    )
    manager.list_tasks(owner=MacosTaskOwner.SYSTEM)
    elevated_reads = [c for c in runner.calls if c["elevated"]]
    testbase.assert_equal(elevated_reads, [])
    return


# ----------------------------------------------------------------------
# Parser tests
# ----------------------------------------------------------------------


def test_parse_print_output_extracts_state_and_last_exit_code():
    """
        ``_parse_print_output`` parses ``launchctl print`` text into a
        ``key -> value`` dict.
    """
    text = (
        "system/com.example.task = {\n"
        "\tstate = running\n"
        "\tlast exit code = 0\n"
        "\tprogram = /usr/bin/example\n"
        "}\n"
    )
    parsed = _parse_print_output(stdout=text)
    testbase.assert_equal(parsed.get("state"), "running")
    testbase.assert_equal(parsed.get("last exit code"), "0")
    testbase.assert_equal(parsed.get("program"), "/usr/bin/example")
    return


def test_parse_print_disabled_extracts_disabled_labels():
    """
        ``_parse_print_disabled`` returns a set of disabled labels.
    """
    text = (
        '"com.example.alpha" => true\n'
        '"com.example.bravo" => false\n'
        '"com.example.charlie" => true\n'
    )
    parsed = _parse_print_disabled(stdout=text)
    testbase.assert_true("com.example.alpha" in parsed)
    testbase.assert_false("com.example.bravo" in parsed)
    testbase.assert_true("com.example.charlie" in parsed)
    return


def test_extract_schedule_from_plist_recovers_daily_form():
    """
        ``_extract_schedule_from_plist`` round-trips a daily-form
        plist back into the canonical input string.
    """
    spec = _system_spec(schedule="daily 02:00")
    schedule_dict = _translate_schedule(schedule=spec.schedule)
    body = _render_plist_body(spec=spec, schedule_dict=schedule_dict)
    recovered = _extract_schedule_from_plist(text=body)
    testbase.assert_equal(recovered, "daily 02:00")
    return


def test_extract_program_arguments_from_plist_returns_argv():
    """
        ``_extract_program_arguments_from_plist`` recovers the argv
        list from the rendered plist body.
    """
    spec = _system_spec()
    schedule_dict = _translate_schedule(schedule=spec.schedule)
    body = _render_plist_body(spec=spec, schedule_dict=schedule_dict)
    argv = _extract_program_arguments_from_plist(text=body)
    testbase.assert_equal(argv, ["/usr/local/bin/example", "--flag"])
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_list_tasks_raises_on_unexpected_ls_failure():
    """
        ``list_tasks`` raises :class:`MacosScheduledTaskQueryError`
        when ``ls`` fails for a reason other than the directory
        being absent.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/Library/LaunchDaemons")] = RunResult(
        exit_code=1, stdout="", stderr="permission denied\n",
    )

    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    raised = False
    try:
        manager.list_tasks(owner=MacosTaskOwner.SYSTEM)
    except MacosScheduledTaskQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_invalid_task_name_returns_failed():
    """
        Verbs that take a name reject names that contain illegal
        characters with FAILED ``invalid_task_name`` and no runner
        calls.
    """
    runner = _MockCommandRunner()
    manager = MacosScheduledTasksManager(
        runner=runner,
        config=MacosScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(
        name="bad name with spaces", owner=MacosTaskOwner.SYSTEM,
    )
    testbase.assert_equal(
        result.status, MacosScheduledTaskActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "invalid_task_name")
    testbase.assert_equal(len(runner.calls), 0)
    return
