"""
    Tests for :class:`MacosBackupRestoreManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values keyed by argv tuple.  No live ``tmutil`` /
    ``restic`` / ``rsync`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.macos.backuprestore import (
    MacosBackupActionStatus,
    MacosBackupJobSpec,
    MacosBackupKind,
    MacosBackupManagerConfig,
    MacosBackupQueryError,
    MacosBackupRestoreManager,
    _serialise_spec,
)


_DEFAULT_JOB_DIR = "/var/db/automojo/backups/jobs"


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
            "env": env,
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


def _seed_test_dash_e(
    runner: _MockCommandRunner,
    *,
    path: str,
    exists: bool,
) -> None:
    if exists:
        result = RunResult(exit_code=0, stdout="", stderr="")
    else:
        result = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[("test", "-e", path)] = result
    return


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


def _seed_ls(
    runner: _MockCommandRunner,
    *,
    path: str,
    listing: Optional[list],
) -> None:
    if listing is None:
        result = RunResult(
            exit_code=2,
            stdout="",
            stderr="ls: No such file or directory",
        )
    else:
        result = RunResult(
            exit_code=0,
            stdout="\n".join(listing),
            stderr="",
        )
    runner.results[("ls", "-1", path)] = result
    return


def _make_spec_tm() -> MacosBackupJobSpec:
    rtnval = MacosBackupJobSpec(
        id="hostA-tm",
        kind=MacosBackupKind.TIME_MACHINE,
        source_paths=["/"],
        destination="/Volumes/TimeMachine",
    )
    return rtnval


def _make_spec_rsync() -> MacosBackupJobSpec:
    rtnval = MacosBackupJobSpec(
        id="users-mirror",
        kind=MacosBackupKind.RSYNC,
        source_paths=["/Users"],
        destination="/Volumes/Backup/Users",
        excludes=[".DS_Store"],
    )
    return rtnval


def _make_spec_restic_encrypted() -> MacosBackupJobSpec:
    rtnval = MacosBackupJobSpec(
        id="repo-restic",
        kind=MacosBackupKind.RESTIC,
        source_paths=["/Users/me/work"],
        destination="/Volumes/Backup/restic-repo",
        encryption_passphrase="topsecret-passphrase",
    )
    return rtnval


# ----------------------------------------------------------------------
# create_backup_job
# ----------------------------------------------------------------------


def test_create_backup_job_persists_spec():
    """
        create_backup_job dispatches ``tee`` with the JSON
        payload on stdin, then ``tmutil setdestination`` for
        TIME_MACHINE jobs.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    tee_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"][1], spec_path)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    # And tmutil setdestination must follow.
    tm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) >= 2
            and c["args"][0] == "tmutil"
            and c["args"][1] == "setdestination"
        )
    ]
    testbase.assert_equal(len(tm_calls), 1)
    testbase.assert_equal(
        tm_calls[0]["args"], ["tmutil", "setdestination", "/Volumes/TimeMachine"],
    )
    return


def test_create_backup_job_skipped_when_present():
    """
        create_backup_job returns SKIPPED when on-disk JSON
        matches the proposed serialisation.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SKIPPED,
    )
    tee_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_create_backup_job_dry_run():
    """
        create_backup_job under dry-run does not invoke the
        runner.
    """
    runner = _MockCommandRunner()
    config = MacosBackupManagerConfig(elevated=True, dry_run=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=_make_spec_tm())

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_backup_job
# ----------------------------------------------------------------------


def test_delete_backup_job_invokes_remove():
    """
        delete_backup_job dispatches ``tmutil removedestination``
        and then ``rm -f <spec_path>`` for TIME_MACHINE specs.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_test_dash_e(runner, path=spec_path, exists=True)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.delete_backup_job(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    rm_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 1)
    tm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) >= 2
            and c["args"][0] == "tmutil"
            and c["args"][1] == "removedestination"
        )
    ]
    testbase.assert_equal(len(tm_calls), 1)
    return


# ----------------------------------------------------------------------
# run_backup_now
# ----------------------------------------------------------------------


def test_run_backup_now_dispatches_tmutil():
    """
        run_backup_now with kind=TIME_MACHINE dispatches
        ``tmutil startbackup --auto``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    tm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) >= 2
            and c["args"][0] == "tmutil"
            and c["args"][1] == "startbackup"
        )
    ]
    testbase.assert_equal(len(tm_calls), 1)
    testbase.assert_equal(
        tm_calls[0]["args"],
        ["tmutil", "startbackup", "--auto"],
    )
    testbase.assert_equal(tm_calls[0]["elevated"], True)
    return


def test_run_backup_now_dispatches_rsync():
    """
        run_backup_now with kind=RSYNC dispatches
        ``rsync -aAXv --delete``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    rsync_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "rsync"
    ]
    testbase.assert_equal(len(rsync_calls), 1)
    argv = rsync_calls[0]["args"]
    testbase.assert_equal(argv[1], "-aAXv")
    testbase.assert_equal(argv[2], "--delete")
    testbase.assert_equal(argv[-2], "/Users")
    testbase.assert_equal(argv[-1], "/Volumes/Backup/Users")
    return


def test_run_backup_now_dispatches_restic():
    """
        run_backup_now with kind=RESTIC routes the passphrase via
        stdin, never on argv.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    forged = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_cat(runner, path=spec_path, body=forged)

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "restic"
    ]
    testbase.assert_equal(len(restic_calls), 1)
    argv = restic_calls[0]["args"]
    testbase.assert_true("--password-file=/dev/stdin" in argv)
    testbase.assert_true("backup" in argv)
    for token in argv:
        testbase.assert_true(
            "topsecret-passphrase" not in token,
        )
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    return


# ----------------------------------------------------------------------
# list_backups
# ----------------------------------------------------------------------


def test_list_backups_parses_ls_output():
    """
        list_backups invokes ``ls -1 <job_dir>`` and ``cat`` per
        spec, and additionally invokes ``tmutil listbackups`` when
        any spec is TIME_MACHINE.
    """
    runner = _MockCommandRunner()
    tm_spec = _make_spec_tm()
    rsync_spec = _make_spec_rsync()
    _seed_ls(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=[
            "{}.json".format(tm_spec.id),
            "{}.json".format(rsync_spec.id),
        ],
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, tm_spec.id),
        body=_serialise_spec(spec=tm_spec),
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, rsync_spec.id),
        body=_serialise_spec(spec=rsync_spec),
    )
    runner.results[("tmutil", "listbackups")] = RunResult(
        exit_code=0,
        stdout=(
            "/Volumes/TimeMachine/Backups.backupdb/host/2026-04-24-120000\n"
            "/Volumes/TimeMachine/Backups.backupdb/host/2026-04-23-120000\n"
        ),
        stderr="",
    )

    manager = MacosBackupRestoreManager(runner=runner)
    results = manager.list_backups()

    # Two persisted specs + two live snapshots.
    testbase.assert_equal(len(results), 4)
    persisted_ids = [r.id for r in results]
    testbase.assert_true(tm_spec.id in persisted_ids)
    testbase.assert_true(rsync_spec.id in persisted_ids)
    testbase.assert_true(
        "2026-04-24-120000" in persisted_ids,
    )
    return


# ----------------------------------------------------------------------
# restore_from_backup
# ----------------------------------------------------------------------


def test_restore_from_backup_invokes_tmutil():
    """
        restore_from_backup with kind=TIME_MACHINE dispatches
        ``tmutil restore <backup_path> <target>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target="/restore-root",
    )

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    tm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) >= 2
            and c["args"][0] == "tmutil"
            and c["args"][1] == "restore"
        )
    ]
    testbase.assert_equal(len(tm_calls), 1)
    testbase.assert_equal(
        tm_calls[0]["args"],
        ["tmutil", "restore", "/Volumes/TimeMachine", "/restore-root"],
    )
    return


def test_restore_from_backup_invokes_rsync():
    """
        restore_from_backup with kind=RSYNC reverses direction
        (destination -> target).
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target="/restore/Users",
    )

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    rsync_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "rsync"
    ]
    argv = rsync_calls[0]["args"]
    testbase.assert_equal(argv[-2], "/Volumes/Backup/Users")
    testbase.assert_equal(argv[-1], "/restore/Users")
    return


# ----------------------------------------------------------------------
# verify_backup
# ----------------------------------------------------------------------


def test_verify_backup_invokes_tmutil_compare():
    """
        verify_backup with kind=TIME_MACHINE dispatches
        ``tmutil compare``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    tm_calls = [
        c for c in runner.calls
        if (
            len(c["args"]) >= 2
            and c["args"][0] == "tmutil"
            and c["args"][1] == "compare"
        )
    ]
    testbase.assert_equal(len(tm_calls), 1)
    return


def test_verify_backup_invokes_restic_check():
    """
        verify_backup with kind=RESTIC dispatches ``restic check``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    forged = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_cat(runner, path=spec_path, body=forged)

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "restic"
    ]
    testbase.assert_equal(len(restic_calls), 1)
    testbase.assert_true("check" in restic_calls[0]["args"])
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    manager.create_backup_job(spec=spec)

    write_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "tee"
    ]
    testbase.assert_true(len(write_calls) >= 1)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    rsync_spec = _make_spec_rsync()
    _seed_ls(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=["{}.json".format(rsync_spec.id)],
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, rsync_spec.id),
        body=_serialise_spec(spec=rsync_spec),
    )

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    manager.list_backups()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        FAILED results carry the captured stderr unchanged.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))
    runner.default_result = RunResult(
        exit_code=23,
        stdout="",
        stderr="rsync: connection unexpectedly closed",
    )

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 23)
    testbase.assert_true(
        result.stderr is not None
        and "connection unexpectedly closed" in result.stderr,
    )
    return


def test_passphrase_redacted_when_restic_encrypted():
    """
        The persisted JSON replaces the plaintext passphrase
        with the redaction sentinel.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, MacosBackupActionStatus.SUCCESS,
    )
    tee_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    persisted_text = tee_calls[0]["stdin"]
    testbase.assert_true(persisted_text is not None)
    testbase.assert_true(
        "topsecret-passphrase" not in persisted_text,
    )
    testbase.assert_true("***REDACTED***" in persisted_text)
    return


def test_time_machine_dispatch_uses_tmutil():
    """
        Canonical Time Machine dispatch test.
        Confirms that every TIME_MACHINE-kind verb routes
        through ``tmutil``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tm()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = MacosBackupManagerConfig(elevated=True)
    manager = MacosBackupRestoreManager(runner=runner, config=config)
    manager.run_backup_now(job_id=spec.id)
    manager.verify_backup(job_id=spec.id)

    tmutil_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "tmutil"
    ]
    # One for startbackup, one for compare.
    testbase.assert_equal(len(tmutil_calls), 2)
    return


def test_list_backups_query_error_when_ls_fails_unexpectedly():
    """
        list_backups raises MacosBackupQueryError on unexpected
        ``ls`` failure.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", _DEFAULT_JOB_DIR)] = RunResult(
        exit_code=2,
        stdout="",
        stderr="ls: permission denied",
    )

    manager = MacosBackupRestoreManager(runner=runner)
    raised = False
    try:
        manager.list_backups()
    except MacosBackupQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_action_result_carries_target_path():
    """
        Successful results populate the ``target_path`` field.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    manager = MacosBackupRestoreManager(
        runner=runner,
        config=MacosBackupManagerConfig(elevated=True),
    )
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(result.target_path, spec_path)
    testbase.assert_equal(result.job_id, spec.id)
    return
