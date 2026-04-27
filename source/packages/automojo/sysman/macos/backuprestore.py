"""
    macOS backup-restore feature.

    Implements the cross-OS backup-restore contract for macOS by
    wrapping three backend tools available on a stock macOS host:
    Time Machine via the ``tmutil`` CLI (default), ``rsync``, and
    ``restic``.  macOS ships Time Machine as the platform's
    first-party backup framework; the operator chooses one of the
    three backends per job via the :class:`MacosBackupKind` enum,
    and every verb dispatches the right command for the selected
    backend.

    Verb / backend mapping:

    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | Verb                  | TIME_MACHINE                               | RSYNC                                      | RESTIC                                     |
    +=======================+============================================+============================================+============================================+
    | ``create_backup_job`` | ``tee <job-path>`` +                       | ``tee <job-path>``                         | ``tee <job-path>``                         |
    |                       | ``tmutil setdestination <dst>``            |                                            |                                            |
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | ``delete_backup_job`` | ``rm -f <job-path>`` +                     | ``rm -f <job-path>``                       | ``rm -f <job-path>``                       |
    |                       | ``tmutil removedestination <dst>``         |                                            |                                            |
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | ``run_backup_now``    | ``tmutil startbackup --auto``              | ``rsync -aAXv --delete <src> <dst>``       | ``restic backup``                          |
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | ``list_backups``      | ``ls -1`` + ``cat`` +                      | ``ls -1`` + ``cat``                        | ``ls -1`` + ``cat``                        |
    |                       | ``tmutil listbackups``                     |                                            |                                            |
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | ``restore_from_backup``| ``tmutil restore <bp> <target>``          | ``rsync -aAXv --delete <dst> <target>``    | ``restic restore latest --target <target>``|
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+
    | ``verify_backup``     | ``tmutil compare``                         | best-effort ``rsync --checksum``           | ``restic check``                           |
    +-----------------------+--------------------------------------------+--------------------------------------------+--------------------------------------------+

    Time Machine constraints:
        Time Machine imposes constraints that the manager does
        NOT pre-validate.  The destination MUST be an APFS volume
        formatted for Time Machine; the destination MUST be
        writable by ``root``; and Time Machine ignores the
        spec's ``excludes`` field (excludes are managed via
        ``tmutil addexclusion``, which is out of scope for this
        feature).  Constraint violations surface as ``tmutil``
        non-zero exits and the captured stderr is passed
        through verbatim.

    Schedule wiring:
        This feature persists the ``schedule`` field on each spec
        but does NOT itself create ``launchd`` plists, cron
        entries, or any other scheduling primitive.  Schedule
        wiring is delegated to the ``scheduled-tasks`` feature.

    Encryption passphrase handling:
        When the spec carries an ``encryption_passphrase`` and
        the backend is ``RESTIC``, the passphrase is delivered
        via the runner's stdin channel and the argv carries
        ``--password-file=/dev/stdin``.  The persisted on-disk
        spec replaces the passphrase with the sentinel string
        ``"***REDACTED***"``.

    Idempotency:
        * ``create_backup_job``: returns ``SKIPPED`` when the
          on-disk JSON spec is byte-equivalent to the proposed
          serialisation.
        * ``delete_backup_job``: returns ``SKIPPED`` when the
          spec file is absent.
        * ``run_backup_now``, ``restore_from_backup``,
          ``verify_backup``: never skip.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads
        (``list_backups``, ``get_job_status``) never elevate.
"""

import json
import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_TMUTIL = "tmutil"
_RSYNC = "rsync"
_RESTIC = "restic"
_TAR = "tar"
_TEE = "tee"
_CAT = "cat"
_RM = "rm"
_LS = "ls"
_TEST = "test"

_DEFAULT_JOB_DIR = "/var/db/automojo/backups/jobs"

_REDACTION_SENTINEL = "***REDACTED***"

_MODE_RUN = "run"
_MODE_RESTORE = "restore"
_MODE_VERIFY = "verify"


MacosBackupActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enum
# ----------------------------------------------------------------------


class MacosBackupKind(Enum):
    """
        Backend selector for a :class:`MacosBackupJobSpec`.

        +---------------+----------------------------------------+
        | Value         | Backend                                |
        +===============+========================================+
        | ``TIME_MACHINE``| Apple Time Machine via ``tmutil``    |
        +---------------+----------------------------------------+
        | ``RSYNC``     | ``rsync -aAXv --delete`` mirror        |
        +---------------+----------------------------------------+
        | ``RESTIC``    | ``restic`` deduplicating snapshot      |
        +---------------+----------------------------------------+
    """
    TIME_MACHINE = "time_machine"
    RSYNC = "rsync"
    RESTIC = "restic"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosBackupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosBackupRestoreManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param default_kind: Backend selected when a
                             :class:`MacosBackupJobSpec` does not
                             pin its own ``kind``.  Defaults to
                             :attr:`MacosBackupKind.TIME_MACHINE`.
        :param job_dir: Directory holding persisted job specs.
                        Defaults to
                        ``/var/db/automojo/backups/jobs``.
    """
    default_kind: MacosBackupKind = MacosBackupKind.TIME_MACHINE
    job_dir: str = _DEFAULT_JOB_DIR


@dataclass
class MacosBackupActionResult(FeatureActionResult):
    """
        Result of a :class:`MacosBackupRestoreManager` write
        verb.

        :param job_id: The job identifier the action targeted,
                       when applicable.
        :param target_path: The on-disk target the action
                            produced (archive path / spec path /
                            restore root), when applicable.
    """
    job_id: Optional[str] = None
    target_path: Optional[str] = None


@dataclass
class MacosBackupJobSpec:
    """
        Declarative specification for a single backup job.

        :param id: Caller-chosen deterministic job identifier.
        :param kind: The backend to dispatch.
        :param source_paths: Filesystem paths to back up.  The
                             ``TIME_MACHINE`` backend ignores
                             this field — Time Machine backs up
                             the entire volume by default.
        :param destination: Destination volume / repository /
                            archive path (interpreted per
                            backend).
        :param excludes: Optional list of exclude patterns.  The
                         ``TIME_MACHINE`` backend ignores this
                         field.
        :param compression: When ``True`` (default) the ``TAR``
                            (n/a on macOS) and ``RESTIC`` backends
                            apply their default compression.
                            Ignored by the ``TIME_MACHINE``
                            backend.
        :param encryption_passphrase: When set, used as the
                                      ``RESTIC`` repository
                                      passphrase.  Ignored by
                                      the ``TIME_MACHINE`` and
                                      ``RSYNC`` backends.
        :param retention_days: Optional retention horizon (in
                               days).
        :param schedule: Optional schedule expression.  Persisted
                         but not wired by this feature.
    """
    id: str
    kind: MacosBackupKind
    source_paths: list
    destination: str
    excludes: Optional[list] = None
    compression: bool = True
    encryption_passphrase: Optional[str] = None
    retention_days: Optional[int] = None
    schedule: Optional[str] = None


# Alias for cross-spec compatibility.
MacosBackupSpec = MacosBackupJobSpec


@dataclass
class MacosBackupInfo:
    """
        One row from
        :meth:`MacosBackupRestoreManager.list_backups`.

        :param id: Job identifier.
        :param kind: Backend kind.
        :param created: ISO-8601 timestamp of the backup, when
                        known; ``None`` otherwise.
        :param size_bytes: On-disk size in bytes when known;
                           ``None`` when not.
        :param source_count: Length of the ``source_paths`` list.
        :param destination: Destination path / volume from the
                            persisted spec.
    """
    id: str
    kind: MacosBackupKind
    created: Optional[str]
    size_bytes: Optional[int]
    source_count: Optional[int]
    destination: str


@dataclass
class MacosBackupVerifyResult:
    """
        Result of :meth:`MacosBackupRestoreManager.verify_backup`.

        :param backup_id: The archive identifier verified.
        :param ok: True when verification passed.
        :param recorded_checksum: Recorded checksum, when known.
        :param computed_checksum: Computed checksum, when known.
        :param detail: Human-readable detail on failure.
    """
    backup_id: str
    ok: bool
    recorded_checksum: Optional[str] = None
    computed_checksum: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class MacosBackupJobStatus:
    """
        Result of :meth:`MacosBackupRestoreManager.get_job_status`.

        :param job_id: The job identifier.
        :param kind: Backend kind.
        :param enabled: True when the spec is on disk.
        :param last_run_at: Reserved for future runtime tracking.
        :param last_run_status: Reserved for future runtime
                                tracking.
        :param next_run_at: Reserved for future runtime tracking.
    """
    job_id: str
    kind: MacosBackupKind
    enabled: bool
    last_run_at: Optional[str] = None
    last_run_status: Optional[MacosBackupActionStatus] = None
    next_run_at: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosBackupQueryError(Exception):
    """
        Raised when a backup-restore read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Backup-restore query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosBackupNotFoundError(Exception):
    """
        Raised when a ``job_id`` is supplied to a read verb and
        the spec does not exist on disk.
    """

    def __init__(self, *, job_id: str) -> None:
        """
            :param job_id: The missing job identifier.
        """
        self.job_id = job_id

        err_msg_lines = ["Backup job not found."]
        err_msg_lines.append("    JOB_ID: {}".format(job_id))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosBackupVerifyError(Exception):
    """
        Raised when verification logic encounters a structural
        error distinct from a mismatched checksum.
    """

    def __init__(self, *, message: str) -> None:
        """
            :param message: Description of the structural failure.
        """
        super().__init__(message)
        return


# ----------------------------------------------------------------------
# MacosBackupRestoreManager
# ----------------------------------------------------------------------


class MacosBackupRestoreManager(BaseFeatureManager):
    """
        macOS backup-restore manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  File I/O is performed via
        ``cat`` / ``tee`` / ``rm``.

        See the module docstring for the full verb-to-tool table
        and the Time-Machine constraint notes.
    """

    result_class = MacosBackupActionResult
    status_class = MacosBackupActionStatus
    feature_name = "backup-restore"

    def __init__(
        self,
        runner,
        config: Optional[MacosBackupManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to
                           a fresh
                           :class:`MacosBackupManagerConfig`.
        """
        if config is None:
            config = MacosBackupManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_backup_job(
        self,
        spec: MacosBackupJobSpec,
    ) -> MacosBackupActionResult:
        """
            Persist a backup-job spec to disk.  When the spec is
            ``TIME_MACHINE``, also invoke
            ``tmutil setdestination <destination>``.

            Idempotent: when the on-disk spec matches the
            proposed serialisation, returns ``SKIPPED``.

            :param spec: The :class:`MacosBackupJobSpec`.

            :returns: The :class:`MacosBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=spec.id)
        new_payload = _serialise_spec(spec=spec)
        write_command = [_TEE, job_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBackupActionStatus.DRY_RUN,
                job_id=spec.id,
                target_path=job_path,
                command=write_command,
            )
            return dry

        existing = self._read_spec_text(path=job_path)
        if existing is not None and existing.strip() == new_payload.strip():
            skipped = self._build_result(
                status=MacosBackupActionStatus.SKIPPED,
                job_id=spec.id,
                target_path=job_path,
                command=write_command,
                detail="spec already persisted",
            )
            return skipped

        write_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_payload,
        )
        if write_result.exit_code != 0:
            failed = self._wrap_run_result(
                run_result=write_result,
                command=write_command,
                job_id=spec.id,
                target_path=job_path,
            )
            return failed

        if spec.kind == MacosBackupKind.TIME_MACHINE:
            tm_command = [_TMUTIL, "setdestination", spec.destination]
            tm_result = self._runner.run(
                list(tm_command),
                elevated=self._config.elevated,
            )
            rtnval = self._wrap_run_result(
                run_result=tm_result,
                command=tm_command,
                job_id=spec.id,
                target_path=job_path,
            )
            return rtnval

        rtnval = self._wrap_run_result(
            run_result=write_result,
            command=write_command,
            job_id=spec.id,
            target_path=job_path,
        )
        return rtnval

    def delete_backup_job(
        self,
        job_id: str,
    ) -> MacosBackupActionResult:
        """
            Remove a persisted backup-job spec.  When the spec is
            ``TIME_MACHINE``, also invoke
            ``tmutil removedestination <destination>``.

            Idempotent: when the spec file is absent, returns
            ``SKIPPED``.

            :param job_id: The job identifier to remove.

            :returns: The :class:`MacosBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        rm_command = [_RM, "-f", job_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=job_path,
                command=rm_command,
            )
            return dry

        present = self._path_exists(path=job_path)
        if not present:
            skipped = self._build_result(
                status=MacosBackupActionStatus.SKIPPED,
                job_id=job_id,
                target_path=job_path,
                command=rm_command,
                detail="spec already absent",
            )
            return skipped

        # Read the spec to determine whether to call tmutil.
        text = self._read_spec_text(path=job_path)
        spec: Optional[MacosBackupJobSpec] = None
        if text is not None:
            try:
                spec = _deserialise_spec(text=text)
            except (ValueError, KeyError):
                spec = None

        if spec is not None and spec.kind == MacosBackupKind.TIME_MACHINE:
            tm_command = [_TMUTIL, "removedestination", spec.destination]
            tm_result = self._runner.run(
                list(tm_command),
                elevated=self._config.elevated,
            )
            if tm_result.exit_code != 0:
                failed = self._wrap_run_result(
                    run_result=tm_result,
                    command=tm_command,
                    job_id=job_id,
                    target_path=job_path,
                )
                return failed

        rm_result = self._runner.run(
            list(rm_command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=rm_result,
            command=rm_command,
            job_id=job_id,
            target_path=job_path,
        )
        return rtnval

    def run_backup_now(
        self,
        job_id: str,
    ) -> MacosBackupActionResult:
        """
            Dispatch a backup run for an already-persisted job.

            :param job_id: The job identifier to run.

            :returns: The :class:`MacosBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RUN,
            target=None,
        )
        return rtnval

    def restore_from_backup(
        self,
        job_id: str,
        target: str,
    ) -> MacosBackupActionResult:
        """
            Restore from a previously-taken backup.

            :param job_id: The job identifier to restore from.
            :param target: The on-disk target / restore root.

            :returns: The :class:`MacosBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RESTORE,
            target=target,
        )
        return rtnval

    def verify_backup(
        self,
        job_id: str,
    ) -> MacosBackupActionResult:
        """
            Verify a backup's integrity.

            :param job_id: The job identifier to verify.

            :returns: The :class:`MacosBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_VERIFY,
            target=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_backups(self) -> list:
        """
            Enumerate every persisted backup-job spec.  When any
            persisted spec carries ``kind=TIME_MACHINE``, also
            invoke ``tmutil listbackups`` and append the live
            snapshot rows.

            :returns: A list of :class:`MacosBackupInfo`.

            :raises MacosBackupQueryError: When ``ls`` exits
                                           non-zero with
                                           non-empty stderr that
                                           does not match
                                           ENOENT.
        """
        cfg = self._typed_config()
        ls_command = [_LS, "-1", cfg.job_dir]

        ls_result = self._runner.run(
            list(ls_command), elevated=False,
        )

        rtnval: list = []
        any_time_machine = False

        if ls_result.exit_code != 0:
            stderr_text = ls_result.stderr or ""
            lowered = stderr_text.lower()
            if (
                len(stderr_text.strip()) > 0
                and "no such file" not in lowered
                and "not found" not in lowered
            ):
                raise MacosBackupQueryError(
                    command=ls_command,
                    exit_code=ls_result.exit_code,
                    stderr=stderr_text,
                )
        else:
            for raw_name in ls_result.stdout.splitlines():
                name = raw_name.strip()
                if len(name) == 0:
                    continue
                if not name.endswith(".json"):
                    continue
                job_id = name[: -len(".json")]
                spec_path = _job_path(
                    job_dir=cfg.job_dir, job_id=job_id,
                )
                text = self._read_spec_text(path=spec_path)
                if text is None:
                    continue
                try:
                    spec = _deserialise_spec(text=text)
                except (ValueError, KeyError):
                    continue
                if spec.kind == MacosBackupKind.TIME_MACHINE:
                    any_time_machine = True
                info = MacosBackupInfo(
                    id=spec.id,
                    kind=spec.kind,
                    created=None,
                    size_bytes=None,
                    source_count=len(spec.source_paths),
                    destination=spec.destination,
                )
                rtnval.append(info)

        if any_time_machine is True:
            tm_command = [_TMUTIL, "listbackups"]
            tm_result = self._runner.run(
                list(tm_command), elevated=False,
            )
            if tm_result.exit_code == 0:
                for raw_path in tm_result.stdout.splitlines():
                    snapshot_path = raw_path.strip()
                    if len(snapshot_path) == 0:
                        continue
                    snapshot_id = snapshot_path.rsplit("/", 1)[-1]
                    info = MacosBackupInfo(
                        id=snapshot_id,
                        kind=MacosBackupKind.TIME_MACHINE,
                        created=snapshot_id,
                        size_bytes=None,
                        source_count=None,
                        destination=snapshot_path,
                    )
                    rtnval.append(info)

        return rtnval

    def get_job_status(self, job_id: str) -> MacosBackupJobStatus:
        """
            Report the persistence-and-runtime status of a job.

            :param job_id: The job identifier.

            :returns: The :class:`MacosBackupJobStatus`.

            :raises MacosBackupNotFoundError: When the spec is
                                              absent.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        text = self._read_spec_text(path=spec_path)
        if text is None:
            raise MacosBackupNotFoundError(job_id=job_id)
        spec = _deserialise_spec(text=text)
        rtnval = MacosBackupJobStatus(
            job_id=spec.id,
            kind=spec.kind,
            enabled=True,
            last_run_at=None,
            last_run_status=None,
            next_run_at=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch_backend_verb(
        self,
        *,
        job_id: str,
        mode: str,
        target: Optional[str],
    ) -> MacosBackupActionResult:
        """
            Shared dispatch path for ``run_backup_now``,
            ``restore_from_backup``, and ``verify_backup``.

            :param job_id: The job identifier whose spec to load.
            :param mode: One of ``_MODE_RUN``, ``_MODE_RESTORE``,
                         ``_MODE_VERIFY``.
            :param target: The restore-target path.

            :returns: The :class:`MacosBackupActionResult`.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
            )
            return dry

        text = self._read_spec_text(path=spec_path)
        if text is None:
            failed = self._build_result(
                status=MacosBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail="job not found",
                stderr="job not found",
            )
            return failed

        try:
            spec = _deserialise_spec(text=text)
        except (ValueError, KeyError) as parse_error:
            failed = self._build_result(
                status=MacosBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail="malformed spec",
                stderr=str(parse_error),
            )
            return failed

        spec.encryption_passphrase = (
            self._resolve_runtime_passphrase(persisted=spec)
        )

        argv, stdin_payload = self._build_backend_command(
            spec=spec,
            mode=mode,
            target=target,
        )
        if argv is None:
            failed = self._build_result(
                status=MacosBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail=(
                    "unsupported backup kind: {}".format(spec.kind)
                ),
                stderr="unsupported_kind",
            )
            return failed

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=stdin_payload,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=argv,
            job_id=job_id,
            target_path=target,
        )
        return rtnval

    def _build_backend_command(
        self,
        *,
        spec: MacosBackupJobSpec,
        mode: str,
        target: Optional[str],
    ) -> tuple:
        """
            Build the argv and optional stdin payload for a
            backend dispatch.

            :param spec: The :class:`MacosBackupJobSpec`.
            :param mode: One of the ``_MODE_*`` constants.
            :param target: The restore-target path; required
                           when ``mode == _MODE_RESTORE``.

            :returns: A two-tuple ``(argv, stdin)``.  ``argv``
                      is ``None`` for unsupported kinds.
        """
        if spec.kind == MacosBackupKind.TIME_MACHINE:
            argv = _build_tmutil_argv(
                spec=spec, mode=mode, target=target,
            )
            rtnval = (argv, None)
            return rtnval
        if spec.kind == MacosBackupKind.RSYNC:
            argv = _build_rsync_argv(
                spec=spec, mode=mode, target=target,
            )
            rtnval = (argv, None)
            return rtnval
        if spec.kind == MacosBackupKind.RESTIC:
            argv, needs_passphrase = _build_restic_argv(
                spec=spec, mode=mode, target=target,
            )
            stdin_payload: Optional[str] = None
            if (
                needs_passphrase is True
                and spec.encryption_passphrase is not None
                and len(spec.encryption_passphrase) > 0
            ):
                stdin_payload = spec.encryption_passphrase
            rtnval = (argv, stdin_payload)
            return rtnval
        rtnval = (None, None)
        return rtnval

    def _resolve_runtime_passphrase(
        self,
        *,
        persisted: MacosBackupJobSpec,
    ) -> Optional[str]:
        """
            Resolve the runtime passphrase to forward to
            ``restic``.

            :param persisted: The deserialised on-disk spec.

            :returns: The runtime passphrase string, or
                      ``None``.
        """
        if persisted.encryption_passphrase is None:
            rtnval: Optional[str] = None
            return rtnval
        if persisted.encryption_passphrase == _REDACTION_SENTINEL:
            rtnval = None
            return rtnval
        rtnval = persisted.encryption_passphrase
        return rtnval

    def _typed_config(self) -> MacosBackupManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosBackupManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, MacosBackupManagerConfig):
            rtnval = self._config
            return rtnval
        rtnval = MacosBackupManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
        )
        return rtnval

    def _read_spec_text(self, *, path: str) -> Optional[str]:
        """
            Read a spec file via ``cat``.

            :param path: The absolute spec file path.

            :returns: The file contents, or ``None``.
        """
        command = [_CAT, path]
        run_result = self._runner.run(
            list(command), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero.
        """
        command = [_TEST, "-e", path]
        run_result = self._runner.run(
            list(command), elevated=False,
        )
        rtnval = run_result.exit_code == 0
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosBackupActionStatus,
        job_id: Optional[str],
        target_path: Optional[str],
        command: list,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosBackupActionResult:
        """
            Construct a :class:`MacosBackupActionResult` without
            invoking the runner.
        """
        rtnval = MacosBackupActionResult(
            status=status,
            detail=detail,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
            stderr=stderr,
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        run_result,
        command: list,
        job_id: Optional[str],
        target_path: Optional[str],
    ) -> MacosBackupActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosBackupActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosBackupActionStatus.SUCCESS
        else:
            mapped_status = MacosBackupActionStatus.FAILED

        rtnval = MacosBackupActionResult(
            status=mapped_status,
            detail=job_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — serialisation
# ----------------------------------------------------------------------


def _serialise_spec(*, spec: MacosBackupJobSpec) -> str:
    """
        Serialise a :class:`MacosBackupJobSpec` to JSON with the
        encryption passphrase redacted.

        :param spec: The :class:`MacosBackupJobSpec`.

        :returns: The JSON document body.
    """
    redacted = _redact_passphrase(spec=spec)
    payload = {
        "id": redacted.id,
        "kind": redacted.kind.value,
        "source_paths": list(redacted.source_paths),
        "destination": redacted.destination,
        "excludes": (
            None if redacted.excludes is None
            else list(redacted.excludes)
        ),
        "compression": redacted.compression,
        "encryption_passphrase": redacted.encryption_passphrase,
        "retention_days": redacted.retention_days,
        "schedule": redacted.schedule,
    }
    rtnval = json.dumps(payload, indent=2, sort_keys=True)
    return rtnval


def _deserialise_spec(*, text: str) -> MacosBackupJobSpec:
    """
        Parse a JSON spec body into a
        :class:`MacosBackupJobSpec`.

        :param text: The JSON body.

        :returns: The :class:`MacosBackupJobSpec`.
    """
    payload = json.loads(text)

    raw_kind = payload["kind"]
    matched_kind: Optional[MacosBackupKind] = None
    for member in MacosBackupKind:
        if member.value == raw_kind:
            matched_kind = member
            break
    if matched_kind is None:
        err_msg_lines = ["Unknown MacosBackupKind value."]
        err_msg_lines.append("    GOT: {}".format(raw_kind))
        err_msg_lines.append(
            "    EXPECTED: one of {}".format(
                [member.value for member in MacosBackupKind],
            ),
        )
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    raw_excludes = payload.get("excludes")
    excludes_value: Optional[list] = None
    if raw_excludes is not None:
        excludes_value = list(raw_excludes)

    rtnval = MacosBackupJobSpec(
        id=payload["id"],
        kind=matched_kind,
        source_paths=list(payload["source_paths"]),
        destination=payload["destination"],
        excludes=excludes_value,
        compression=payload.get("compression", True),
        encryption_passphrase=payload.get("encryption_passphrase"),
        retention_days=payload.get("retention_days"),
        schedule=payload.get("schedule"),
    )
    return rtnval


def _redact_passphrase(
    *,
    spec: MacosBackupJobSpec,
) -> MacosBackupJobSpec:
    """
        Return a copy of ``spec`` with the encryption
        passphrase replaced by the redaction sentinel.

        :param spec: The :class:`MacosBackupJobSpec`.

        :returns: A new spec with the passphrase redacted.
    """
    if spec.encryption_passphrase is None:
        passphrase_field: Optional[str] = None
    else:
        passphrase_field = _REDACTION_SENTINEL

    redacted_excludes: Optional[list] = None
    if spec.excludes is not None:
        redacted_excludes = list(spec.excludes)

    rtnval = MacosBackupJobSpec(
        id=spec.id,
        kind=spec.kind,
        source_paths=list(spec.source_paths),
        destination=spec.destination,
        excludes=redacted_excludes,
        compression=spec.compression,
        encryption_passphrase=passphrase_field,
        retention_days=spec.retention_days,
        schedule=spec.schedule,
    )
    return rtnval


def _job_path(*, job_dir: str, job_id: str) -> str:
    """
        Compose the absolute path to a persisted spec file.
    """
    rtnval = "{}/{}.json".format(job_dir, job_id)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — argv builders
# ----------------------------------------------------------------------


def _build_tmutil_argv(
    *,
    spec: MacosBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> list:
    """
        Build the ``tmutil`` argv for the requested mode.

        :param spec: The :class:`MacosBackupJobSpec`.
        :param mode: One of the ``_MODE_*`` constants.
        :param target: The restore-target path.

        :returns: The argv as a list of strings.
    """
    if mode == _MODE_RUN:
        # ``tmutil startbackup --auto`` queues a backup against
        # the configured destination.  Time Machine returns
        # immediately; the backup runs asynchronously.
        rtnval: list = [_TMUTIL, "startbackup", "--auto"]
        return rtnval
    if mode == _MODE_RESTORE:
        # ``tmutil restore <backup_path> <target>`` restores a
        # specific snapshot directory into ``target``.  The
        # destination is interpreted as the snapshot directory
        # path (the operator can resolve the snapshot path via
        # ``list_backups`` first).
        rtnval = [_TMUTIL, "restore", spec.destination]
        if target is not None:
            rtnval.append(target)
        return rtnval
    if mode == _MODE_VERIFY:
        # ``tmutil compare`` reports per-file differences against
        # the source.  This is best-effort; macOS provides no
        # round-trip checksum verification for Time Machine
        # snapshots.
        rtnval = [_TMUTIL, "compare"]
        return rtnval
    err_msg_lines = ["Unknown tmutil dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _build_rsync_argv(
    *,
    spec: MacosBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> list:
    """
        Build the ``rsync`` argv for the requested mode.
    """
    if mode == _MODE_RUN:
        argv: list = [_RSYNC, "-aAXv", "--delete"]
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        for source in spec.source_paths:
            argv.append(source)
        argv.append(spec.destination)
        rtnval = argv
        return rtnval
    if mode == _MODE_RESTORE:
        argv = [_RSYNC, "-aAXv", "--delete"]
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        argv.append(spec.destination)
        if target is not None:
            argv.append(target)
        rtnval = argv
        return rtnval
    if mode == _MODE_VERIFY:
        argv = [_RSYNC, "-an", "--checksum", "--delete"]
        for source in spec.source_paths:
            argv.append(source)
        argv.append(spec.destination)
        rtnval = argv
        return rtnval
    err_msg_lines = ["Unknown rsync dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _build_restic_argv(
    *,
    spec: MacosBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> tuple:
    """
        Build the ``restic`` argv for the requested mode.

        The passphrase is NEVER placed on argv.
    """
    needs_passphrase = (
        spec.encryption_passphrase is not None
        and len(spec.encryption_passphrase) > 0
        and spec.encryption_passphrase != _REDACTION_SENTINEL
    )

    base: list = [_RESTIC, "--repo", spec.destination]
    if needs_passphrase is True:
        base.append("--password-file=/dev/stdin")

    if mode == _MODE_RUN:
        argv = list(base)
        argv.append("backup")
        for source in spec.source_paths:
            argv.append(source)
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        rtnval = (argv, needs_passphrase)
        return rtnval
    if mode == _MODE_RESTORE:
        argv = list(base)
        argv.append("restore")
        argv.append("latest")
        argv.append("--target")
        if target is not None:
            argv.append(target)
        rtnval = (argv, needs_passphrase)
        return rtnval
    if mode == _MODE_VERIFY:
        argv = list(base)
        argv.append("check")
        rtnval = (argv, needs_passphrase)
        return rtnval

    err_msg_lines = ["Unknown restic dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)
