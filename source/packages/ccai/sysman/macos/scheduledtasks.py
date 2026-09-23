"""
    macOS scheduled-tasks feature.

    Implements the cross-OS scheduled-tasks contract for macOS using
    launchd plist files and ``launchctl``.

    File I/O policy:
        Every plist read / write / delete / existence probe / directory
        listing is performed through the injected
        :class:`ICommandRunner` (``cat``, ``tee``, ``rm``, ``test -e``,
        ``ls -1``, ``mkdir -p``).  This module does NOT consume an
        :class:`IFileOperator`.

    Owner-scope mapping:
        ``MacosTaskOwner.SYSTEM`` writes a plist to
        ``/Library/LaunchDaemons/<name>.plist`` and dispatches
        ``launchctl ... system/<name>``.
        ``MacosTaskOwner.USER`` writes a plist to
        ``/Users/<user>/Library/LaunchAgents/<name>.plist`` and
        dispatches ``launchctl ... gui/<uid>/<name>``.

    Elevation policy:
        SYSTEM-owner writes elevate when ``config.elevated`` is True.
        USER-owner writes never elevate (matching the pattern set by
        :class:`MacosServiceManager`).  Reads do not elevate.

    Schedule grammar (deliberately limited):
        * ``"daily HH:MM"`` -> ``StartCalendarInterval`` with ``Hour``
          and ``Minute`` keys.
        * ``"every <N>min"`` -> ``StartInterval`` of ``N * 60``
          seconds.
        * ``"every <N>sec"`` -> ``StartInterval`` of ``N`` seconds.
        * ``"every <N>hour"``/``"hours"`` -> ``StartInterval`` of
          ``N * 3600`` seconds.

        Anything else is rejected by :func:`_translate_schedule`,
        which returns an error string surfaced as a ``FAILED`` result
        from :meth:`MacosScheduledTasksManager.create_task`.  Full
        cron-style parsing is intentionally out of scope; callers
        needing it can subclass and override
        :meth:`_render_plist_body`.
"""

import os
import re

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_LAUNCHCTL = "launchctl"

_DEFAULT_SYSTEM_PLIST_DIR = "/Library/LaunchDaemons"
_DEFAULT_USER_PLIST_DIR_TEMPLATE = "/Users/{user}/Library/LaunchAgents"

# Allowed launchd label characters per Apple's reverse-DNS convention
# plus the underscore for friendliness with engineering naming.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

_PLIST_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n'
)
_PLIST_FOOTER = "</plist>\n"


MacosScheduledTaskActionStatus = ActionStatus


class MacosTaskOwner(Enum):
    """
        Owner-scope selector for scheduled-task verbs.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class MacosScheduledTasksManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosScheduledTasksManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param system_plist_dir: Directory holding system-scope
                                 plists.  Defaults to
                                 ``/Library/LaunchDaemons``.
        :param user_plist_dir_template: Format string used to build
                                        the user-scope plist
                                        directory.  The sole
                                        substitution token is
                                        ``{user}``.  Defaults to
                                        ``"/Users/{user}/Library/LaunchAgents"``.
        :param target_user: Username for user-scope operations.
                            Required when any verb is invoked with
                            :attr:`MacosTaskOwner.USER`.
        :param target_uid: Numeric UID used to construct the
                           ``gui/<uid>`` domain when the caller
                           targets USER scope.  Required when any
                           USER-scope verb is invoked.
    """
    system_plist_dir: str = _DEFAULT_SYSTEM_PLIST_DIR
    user_plist_dir_template: str = _DEFAULT_USER_PLIST_DIR_TEMPLATE
    target_user: Optional[str] = None
    target_uid: Optional[int] = None


@dataclass
class MacosScheduledTaskActionResult(FeatureActionResult):
    """
        Result of a scheduled-task write operation.

        :param name: Task / plist label.
        :param owner: Owner scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    owner: Optional[MacosTaskOwner] = None
    message: Optional[str] = None


@dataclass
class MacosTaskSpec:
    """
        Specification for a launchd-managed scheduled task.

        :param name: Task name (will be the plist ``Label``).
        :param command: Argument vector executed by launchd.
                        Rendered into the plist's
                        ``ProgramArguments`` array verbatim.
        :param schedule: Schedule string in the limited grammar
                         documented at module top.
        :param working_dir: Optional ``WorkingDirectory`` for the
                            launchd job.
        :param user: Optional ``UserName`` for the launchd job.
                     Mostly relevant for SYSTEM-owner tasks; USER
                     tasks already run as the logged-in user.
        :param env: Optional ``EnvironmentVariables`` dict.
        :param owner: Owner scope selector.
        :param description: Optional human-readable description; not
                            rendered into the plist (launchd has no
                            description key) but echoed onto the
                            result for callers.
        :param run_at_load: When True, the rendered plist sets
                            ``RunAtLoad`` to ``<true/>``.  Defaults to
                            False to avoid surprising the launchd
                            scheduler.
    """
    name: str
    command: list[str]
    schedule: str
    working_dir: Optional[str] = None
    user: Optional[str] = None
    env: Optional[dict[str, str]] = None
    owner: MacosTaskOwner = MacosTaskOwner.SYSTEM
    description: Optional[str] = None
    run_at_load: bool = False


@dataclass
class MacosTaskInfo:
    """
        Current state of a single launchd-managed task.

        :param name: Task / plist label.
        :param owner: Owner scope queried.
        :param enabled: ``True`` when the task is enabled in the
                        domain (i.e. not present in
                        ``launchctl print-disabled``).
        :param active: ``True`` when ``launchctl print`` reports
                       ``state = running``.
        :param last_run: Last-run timestamp from ``launchctl print``,
                         or ``None`` when not reported.
        :param schedule: Schedule string parsed back from the plist
                         (or ``None`` when unparseable).
        :param program_arguments: Parsed argv from the plist's
                                  ``ProgramArguments`` array, or
                                  ``None`` when unparseable.
    """
    name: str
    owner: MacosTaskOwner
    enabled: bool = False
    active: bool = False
    last_run: Optional[str] = None
    schedule: Optional[str] = None
    program_arguments: Optional[list[str]] = None


class MacosScheduledTaskQueryError(Exception):
    """
        Raised when a scheduled-task read fails for a reason other
        than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
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

        err_msg_lines = ["Scheduled-task query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosScheduledTasksManager(BaseFeatureManager):
    """
        macOS launchd scheduled-task manager.  Every operation is
        dispatched through the injected :class:`ICommandRunner`,
        including all plist file I/O.
    """

    result_class = MacosScheduledTaskActionResult
    status_class = MacosScheduledTaskActionStatus
    feature_name = "scheduled-tasks"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_task(
        self,
        spec: MacosTaskSpec,
    ) -> MacosScheduledTaskActionResult:
        """
            Create or update a scheduled task.

            Renders the plist body, writes it via ``tee`` (after a
            ``mkdir -p`` of the plist directory for the user scope),
            then dispatches
            ``launchctl bootstrap <domain> <plist_path>``.

            Idempotent: when the on-disk plist already matches the
            rendered body byte-for-byte AND the task is enabled in
            the domain, returns ``SKIPPED``.

            :param spec: The :class:`MacosTaskSpec`.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            schedule_dict = _translate_schedule(schedule=spec.schedule)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_schedule",
                stderr=str(exc),
            )
            return failed

        try:
            plist_dir = self._plist_dir(owner=spec.owner)
            domain = self._domain_for(owner=spec.owner)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="scope_misconfigured",
                stderr=str(exc),
            )
            return failed

        plist_path = "{}/{}.plist".format(plist_dir, spec.name)
        plist_body = _render_plist_body(
            spec=spec, schedule_dict=schedule_dict,
        )
        bootstrap_command = [
            _LAUNCHCTL, "bootstrap", domain, plist_path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosScheduledTaskActionStatus.DRY_RUN,
                name=spec.name,
                owner=spec.owner,
                command=bootstrap_command,
            )
            return dry

        existing_body = self._read_file(path=plist_path)
        body_matches = (
            existing_body is not None and existing_body == plist_body
        )

        is_enabled = self._is_label_enabled(
            label=spec.name, domain=domain,
        )
        if body_matches and is_enabled:
            skipped = self._build_result(
                status=MacosScheduledTaskActionStatus.SKIPPED,
                name=spec.name,
                owner=spec.owner,
                command=bootstrap_command,
                message="task plist matches and is already enabled",
            )
            return skipped

        # mkdir -p the plist directory for the USER scope; the system
        # directory is always present on a working macOS install.
        if spec.owner == MacosTaskOwner.USER:
            mkdir_command = ["mkdir", "-p", plist_dir]
            mkdir_run = self._runner.run(
                list(mkdir_command),
                elevated=self._elevation_for_owner(owner=spec.owner),
            )
            if mkdir_run.exit_code != 0:
                failed = MacosScheduledTaskActionResult(
                    status=MacosScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=mkdir_run.exit_code,
                    stdout=mkdir_run.stdout,
                    stderr=mkdir_run.stderr,
                    command=list(mkdir_command),
                    name=spec.name,
                    owner=spec.owner,
                    message="user_plist_dir_create_failed",
                )
                return failed

        if not body_matches:
            write_command = ["tee", plist_path]
            write_run = self._runner.run(
                list(write_command),
                elevated=self._elevation_for_owner(owner=spec.owner),
                stdin=plist_body,
            )
            if write_run.exit_code != 0:
                failed = MacosScheduledTaskActionResult(
                    status=MacosScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=write_run.exit_code,
                    stdout=write_run.stdout,
                    stderr=write_run.stderr,
                    command=list(write_command),
                    name=spec.name,
                    owner=spec.owner,
                    message="plist_write_failed",
                )
                return failed

        # ``launchctl bootstrap`` is idempotent in the sense that
        # running it on an already-bootstrapped target reports a
        # specific error.  We re-dispatch unconditionally because the
        # command is cheap and the easiest way to apply a freshly
        # written plist.
        bootstrap_run = self._runner.run(
            list(bootstrap_command),
            elevated=self._elevation_for_owner(owner=spec.owner),
        )

        # Treat "already bootstrapped" as success: the desired end
        # state is "task is loaded into the domain", which is true
        # whether bootstrap succeeded or whether the task was already
        # there.
        if bootstrap_run.exit_code == 0:
            mapped_status = MacosScheduledTaskActionStatus.SUCCESS
        else:
            stderr_lower = bootstrap_run.stderr.lower()
            already_loaded = (
                "service already loaded" in stderr_lower
                or "already bootstrapped" in stderr_lower
            )
            if already_loaded:
                mapped_status = MacosScheduledTaskActionStatus.SUCCESS
            else:
                mapped_status = MacosScheduledTaskActionStatus.FAILED

        if mapped_status == MacosScheduledTaskActionStatus.FAILED:
            message = "bootstrap_failed"
        else:
            message = None

        rtnval = MacosScheduledTaskActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=bootstrap_run.exit_code,
            stdout=bootstrap_run.stdout,
            stderr=bootstrap_run.stderr,
            command=list(bootstrap_command),
            name=spec.name,
            owner=spec.owner,
            message=message,
        )
        return rtnval

    def delete_task(
        self,
        name: str,
        owner: MacosTaskOwner,
    ) -> MacosScheduledTaskActionResult:
        """
            Delete a scheduled task.

            Dispatches ``launchctl bootout <domain>/<name>`` to remove
            the task from the launchd domain (non-fatal — bootout on
            an already-removed task reports a specific error we
            ignore), then ``rm -f <plist_path>`` to delete the plist.

            Idempotent: when the plist file does not exist, returns
            ``SKIPPED`` without invoking ``launchctl``.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            plist_dir = self._plist_dir(owner=owner)
            domain = self._domain_for(owner=owner)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="scope_misconfigured",
                stderr=str(exc),
            )
            return failed

        plist_path = "{}/{}.plist".format(plist_dir, name)
        target = "{}/{}".format(domain, name)
        bootout_command = [_LAUNCHCTL, "bootout", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=bootout_command,
            )
            return dry

        if not self._path_exists(path=plist_path):
            skipped = self._build_result(
                status=MacosScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=bootout_command,
                message="task plist not found",
            )
            return skipped

        # bootout is informational: a not-loaded task reports non-zero
        # which we ignore so the file delete still runs.
        self._runner.run(
            list(bootout_command),
            elevated=self._elevation_for_owner(owner=owner),
        )

        rm_command = ["rm", "-f", plist_path]
        rm_run = self._runner.run(
            list(rm_command),
            elevated=self._elevation_for_owner(owner=owner),
        )
        if rm_run.exit_code != 0:
            failed = MacosScheduledTaskActionResult(
                status=MacosScheduledTaskActionStatus.FAILED,
                detail=name,
                exit_code=rm_run.exit_code,
                stdout=rm_run.stdout,
                stderr=rm_run.stderr,
                command=list(rm_command),
                name=name,
                owner=owner,
                message="plist_delete_failed",
            )
            return failed

        success = MacosScheduledTaskActionResult(
            status=MacosScheduledTaskActionStatus.SUCCESS,
            detail=name,
            exit_code=rm_run.exit_code,
            stdout=rm_run.stdout,
            stderr=rm_run.stderr,
            command=list(rm_command),
            name=name,
            owner=owner,
        )
        return success

    def enable_task(
        self,
        name: str,
        owner: MacosTaskOwner,
    ) -> MacosScheduledTaskActionResult:
        """
            Enable a launchd-managed task via
            ``launchctl enable <domain>/<name>``.

            Idempotent: when ``launchctl print-disabled <domain>``
            does NOT contain the label, returns ``SKIPPED``.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            domain = self._domain_for(owner=owner)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="scope_misconfigured",
                stderr=str(exc),
            )
            return failed

        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "enable", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        if self._is_label_enabled(label=name, domain=domain):
            skipped = self._build_result(
                status=MacosScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="task is already enabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    def disable_task(
        self,
        name: str,
        owner: MacosTaskOwner,
    ) -> MacosScheduledTaskActionResult:
        """
            Disable a launchd-managed task via
            ``launchctl disable <domain>/<name>``.

            Idempotent: when ``launchctl print-disabled <domain>``
            already contains the label, returns ``SKIPPED``.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            domain = self._domain_for(owner=owner)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="scope_misconfigured",
                stderr=str(exc),
            )
            return failed

        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "disable", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        if not self._is_label_enabled(label=name, domain=domain):
            skipped = self._build_result(
                status=MacosScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="task is already disabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    def run_task_now(
        self,
        name: str,
        owner: MacosTaskOwner,
    ) -> MacosScheduledTaskActionResult:
        """
            Run a task immediately via
            ``launchctl kickstart <domain>/<name>``.

            No idempotency guard — the contract specifies an
            unconditional run.  We deliberately omit the ``-k`` flag
            (which would kill a running instance and restart it):
            ``run_task_now`` should run the task, not interrupt one
            already in progress.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        try:
            domain = self._domain_for(owner=owner)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="scope_misconfigured",
                stderr=str(exc),
            )
            return failed

        target = "{}/{}".format(domain, name)
        command = [_LAUNCHCTL, "kickstart", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_task(
        self,
        name: str,
        owner: MacosTaskOwner,
    ) -> Optional[MacosTaskInfo]:
        """
            Read state for a single task.

            Probes ``launchctl print <domain>/<name>`` for runtime
            state and ``cat <plist_path>`` for the schedule and
            program-arguments.  Reads do not elevate.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosTaskInfo`, or ``None`` when
                      neither the plist file nor the launchd domain
                      knows about this label.
            :raises MacosScheduledTaskQueryError: When ``launchctl
                                                  print`` fails for an
                                                  unexpected reason.
        """
        _ensure_valid_name(name=name)

        plist_dir = self._plist_dir(owner=owner)
        plist_path = "{}/{}.plist".format(plist_dir, name)
        domain = self._domain_for(owner=owner)

        plist_body = self._read_file(path=plist_path)
        target = "{}/{}".format(domain, name)
        print_command = [_LAUNCHCTL, "print", target]
        print_run = self._runner.run(
            list(print_command),
            elevated=False,
        )

        if plist_body is None and print_run.exit_code != 0:
            stderr_lower = print_run.stderr.lower()
            looks_not_found = (
                "could not find service" in stderr_lower
                or "service not found" in stderr_lower
            )
            if looks_not_found:
                rtnval: Optional[MacosTaskInfo] = None
                return rtnval
            raise MacosScheduledTaskQueryError(
                command=print_command,
                exit_code=print_run.exit_code,
                stderr=print_run.stderr,
            )

        if print_run.exit_code != 0:
            stderr_lower = print_run.stderr.lower()
            looks_not_found = (
                "could not find service" in stderr_lower
                or "service not found" in stderr_lower
            )
            if not looks_not_found:
                raise MacosScheduledTaskQueryError(
                    command=print_command,
                    exit_code=print_run.exit_code,
                    stderr=print_run.stderr,
                )

        properties: dict = {}
        if print_run.exit_code == 0:
            properties = _parse_print_output(stdout=print_run.stdout)

        active = (properties.get("state", "").strip() == "running")
        last_run_raw = properties.get("last exit code", "").strip()
        if len(last_run_raw) == 0:
            last_run: Optional[str] = None
        else:
            last_run = last_run_raw

        is_enabled = self._is_label_enabled(label=name, domain=domain)

        if plist_body is not None:
            schedule = _extract_schedule_from_plist(text=plist_body)
            program_arguments = _extract_program_arguments_from_plist(
                text=plist_body,
            )
        else:
            schedule = None
            program_arguments = None

        rtnval = MacosTaskInfo(
            name=name,
            owner=owner,
            enabled=is_enabled,
            active=active,
            last_run=last_run,
            schedule=schedule,
            program_arguments=program_arguments,
        )
        return rtnval

    def list_tasks(
        self,
        owner: MacosTaskOwner,
    ) -> list[MacosTaskInfo]:
        """
            Enumerate scheduled tasks for ``owner``.

            Lists ``<plist_dir>/*.plist`` via ``ls -1`` and surfaces
            one :class:`MacosTaskInfo` per discovered file.  For each
            entry we also probe ``launchctl print`` to populate
            ``active`` and ``enabled``.  Reads do not elevate.

            :param owner: Owner scope.

            :returns: One :class:`MacosTaskInfo` per discovered plist.
            :raises MacosScheduledTaskQueryError: When ``ls -1`` fails
                                                  for a reason other
                                                  than the plist
                                                  directory simply
                                                  being absent.
        """
        plist_dir = self._plist_dir(owner=owner)
        # Resolve the launchd domain up front so a USER scope without a
        # configured 'target_uid' fails before any listing is attempted.
        self._domain_for(owner=owner)
        ls_command = ["ls", "-1", plist_dir]
        ls_run = self._runner.run(list(ls_command), elevated=False)

        if ls_run.exit_code != 0:
            stderr_lower = ls_run.stderr.lower()
            looks_missing = (
                "no such file or directory" in stderr_lower
                or "cannot access" in stderr_lower
            )
            if looks_missing:
                empty: list[MacosTaskInfo] = []
                return empty
            raise MacosScheduledTaskQueryError(
                command=ls_command,
                exit_code=ls_run.exit_code,
                stderr=ls_run.stderr,
            )

        rtnval: list[MacosTaskInfo] = []
        for raw in ls_run.stdout.splitlines():
            entry = raw.strip()
            if len(entry) == 0:
                continue
            if not entry.endswith(".plist"):
                continue
            label = entry[: -len(".plist")]
            try:
                info = self.get_task(name=label, owner=owner)
            except MacosScheduledTaskQueryError:
                # A specific task that fails ``launchctl print`` for
                # an unexpected reason should not poison the entire
                # listing.  We surface a synthetic info entry with
                # ``readable``-equivalent fields cleared so the caller
                # still sees the label.
                info = MacosTaskInfo(
                    name=label,
                    owner=owner,
                    enabled=False,
                    active=False,
                    last_run=None,
                    schedule=None,
                    program_arguments=None,
                )
            if info is None:
                # Plist exists per ``ls`` but ``get_task`` reported
                # neither file nor domain knowledge.  Race condition;
                # surface a placeholder rather than dropping the row.
                info = MacosTaskInfo(
                    name=label,
                    owner=owner,
                    enabled=False,
                    active=False,
                    last_run=None,
                    schedule=None,
                    program_arguments=None,
                )
            rtnval.append(info)

        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosScheduledTasksManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosScheduledTasksManagerConfig`.

            :returns: The typed configuration.
        """
        if isinstance(self._config, MacosScheduledTasksManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosScheduledTasksManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _plist_dir(self, *, owner: MacosTaskOwner) -> str:
        """
            Resolve the absolute plist directory for ``owner``.

            :param owner: Owner scope.

            :returns: Absolute directory path.
            :raises ValueError: When ``owner`` is USER and no
                                ``target_user`` is configured.
        """
        config = self._typed_config()
        if owner == MacosTaskOwner.SYSTEM:
            rtnval = config.system_plist_dir
            return rtnval

        if owner == MacosTaskOwner.USER:
            target_user = config.target_user
            if target_user is None or len(target_user) == 0:
                err_msg_lines = [
                    "USER-scope scheduled-task operations require "
                    "MacosScheduledTasksManagerConfig.target_user.",
                ]
                err_msg_lines.append("    target_user: None")
                err_msg = os.linesep.join(err_msg_lines)
                raise ValueError(err_msg)
            rtnval = config.user_plist_dir_template.format(
                user=target_user,
            )
            return rtnval

        err_msg = "Unhandled task owner: {!r}".format(owner)
        raise ValueError(err_msg)

    def _domain_for(self, *, owner: MacosTaskOwner) -> str:
        """
            Resolve the launchd domain string for ``owner``.

            :param owner: Owner scope.

            :returns: ``"system"`` for SYSTEM, ``"gui/<uid>"`` for USER.
            :raises ValueError: When ``owner`` is USER and no
                                ``target_uid`` is configured.
        """
        if owner == MacosTaskOwner.SYSTEM:
            rtnval = "system"
            return rtnval

        if owner == MacosTaskOwner.USER:
            target_uid = self._typed_config().target_uid
            if target_uid is None:
                err_msg_lines = [
                    "USER-scope scheduled-task operations require "
                    "MacosScheduledTasksManagerConfig.target_uid.",
                ]
                err_msg_lines.append("    target_uid: None")
                err_msg = os.linesep.join(err_msg_lines)
                raise ValueError(err_msg)
            rtnval = "gui/{}".format(target_uid)
            return rtnval

        err_msg = "Unhandled task owner: {!r}".format(owner)
        raise ValueError(err_msg)

    def _elevation_for_owner(self, *, owner: MacosTaskOwner) -> bool:
        """
            Decide whether to elevate a write for ``owner``.

            SYSTEM-owner writes inherit ``config.elevated``; USER-owner
            writes never elevate (matching
            :class:`MacosServiceManager`).

            :param owner: Owner scope.

            :returns: True when the runner should elevate.
        """
        if owner == MacosTaskOwner.SYSTEM:
            rtnval = self._config.elevated
            return rtnval
        rtnval = False
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: Absolute path.

            :returns: File contents on success; ``None`` when ``cat``
                      reports a non-zero exit (i.e. the file is
                      missing).
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: Absolute path.

            :returns: True when ``test -e`` exits zero.
        """
        command = ["test", "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = (run_result.exit_code == 0)
        return rtnval

    def _is_label_enabled(
        self,
        *,
        label: str,
        domain: str,
    ) -> bool:
        """
            Probe ``launchctl print-disabled <domain>`` and report
            whether ``label`` is enabled (i.e. NOT in the disabled
            list).

            ``print-disabled`` prints a brace-delimited block where
            each line of the form ``"<label>" => disabled`` (or
            ``"=> enabled"``) names a known service.  We treat absence
            from the list as "enabled" because launchd's default for
            unknown labels is enabled.  Reads do not elevate.

            :param label: Task / plist label.
            :param domain: Launchd domain string.

            :returns: True when the label is enabled (or unknown).
        """
        command = [_LAUNCHCTL, "print-disabled", domain]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            # When the probe fails outright (e.g. unknown domain), we
            # assume "enabled" so callers do not get spurious skips.
            rtnval = True
            return rtnval

        disabled_set = _parse_print_disabled(stdout=run_result.stdout)
        rtnval = (label not in disabled_set)
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: str,
        owner: MacosTaskOwner,
    ) -> MacosScheduledTaskActionResult:
        """
            Dispatch a non-file write command, applying the
            owner-based elevation policy.

            :param command: Full argv.
            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._elevation_for_owner(owner=owner),
        )
        if run_result.exit_code == 0:
            mapped_status = MacosScheduledTaskActionStatus.SUCCESS
        else:
            mapped_status = MacosScheduledTaskActionStatus.FAILED

        rtnval = MacosScheduledTaskActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            owner=owner,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosScheduledTaskActionStatus,
        name: Optional[str],
        owner: Optional[MacosTaskOwner],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosScheduledTaskActionResult:
        """
            Construct a :class:`MacosScheduledTaskActionResult`
            without invoking the runner.

            :param status: Outcome status.
            :param name: Task name.
            :param owner: Owner scope.
            :param command: Would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`MacosScheduledTaskActionResult`.
        """
        rtnval = MacosScheduledTaskActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            owner=owner,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level translators / renderers / parsers
# ----------------------------------------------------------------------


def _ensure_valid_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` contains disallowed
        characters.

        :param name: Candidate task name.
        :raises ValueError: When ``name`` does not match the allowed
                            character class.
    """
    if name is None or len(name) == 0:
        err_msg = "task name must be non-empty"
        raise ValueError(err_msg)
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid task name: {!r}".format(name)
        raise ValueError(err_msg)
    return


def _translate_schedule(*, schedule: str) -> dict:
    """
        Translate a schedule string into a launchd-friendly dict.

        Returned shape is one of:
            * ``{"kind": "interval", "seconds": <int>}``
            * ``{"kind": "calendar", "Hour": <int>, "Minute": <int>}``

        Supported grammar (case-insensitive on the keyword):
            * ``"daily HH:MM"`` -> calendar form
            * ``"every <N>min"`` / ``"every <N>mins"`` -> interval (60*N)
            * ``"every <N>sec"`` / ``"every <N>secs"`` -> interval (N)
            * ``"every <N>hour"`` / ``"every <N>hours"`` -> interval (3600*N)

        :param schedule: Schedule string.

        :returns: Translation dict.
        :raises ValueError: When the input does not match the grammar.
    """
    if schedule is None:
        err_msg = "schedule must not be None"
        raise ValueError(err_msg)

    candidate = schedule.strip()
    if len(candidate) == 0:
        err_msg = "schedule must not be empty"
        raise ValueError(err_msg)

    lower = candidate.lower()

    # daily HH:MM
    daily_match = re.match(
        r"^daily\s+(\d{1,2}):(\d{1,2})$",
        lower,
    )
    if daily_match is not None:
        hour = int(daily_match.group(1))
        minute = int(daily_match.group(2))
        if hour < 0 or hour > 23:
            err_msg = "daily hour must be in [0, 23]: {}".format(hour)
            raise ValueError(err_msg)
        if minute < 0 or minute > 59:
            err_msg = "daily minute must be in [0, 59]: {}".format(minute)
            raise ValueError(err_msg)
        rtnval = {"kind": "calendar", "Hour": hour, "Minute": minute}
        return rtnval

    # every <N><unit>
    interval_match = re.match(
        r"^every\s+(\d+)\s*(sec|secs|min|mins|hour|hours)$",
        lower,
    )
    if interval_match is not None:
        amount = int(interval_match.group(1))
        unit = interval_match.group(2)
        if amount <= 0:
            err_msg = "interval amount must be positive: {}".format(amount)
            raise ValueError(err_msg)
        if unit in ("sec", "secs"):
            seconds = amount
        elif unit in ("min", "mins"):
            seconds = amount * 60
        else:
            seconds = amount * 3600
        rtnval = {"kind": "interval", "seconds": seconds}
        return rtnval

    err_msg = (
        "unrecognised schedule {!r}; "
        "supported grammar: 'daily HH:MM' or 'every <N>{{sec,min,hour}}'"
    ).format(schedule)
    raise ValueError(err_msg)


def _render_plist_body(
    *,
    spec: MacosTaskSpec,
    schedule_dict: dict,
) -> str:
    """
        Render the launchd plist body for ``spec``.

        Output is byte-identical for matching inputs so that
        :meth:`MacosScheduledTasksManager.create_task` can compare
        against the existing file content for idempotency.

        :param spec: The task spec.
        :param schedule_dict: Dict produced by
                              :func:`_translate_schedule`.

        :returns: The full plist body as a string ending in ``\\n``.
    """
    lines: list[str] = [_PLIST_HEADER.rstrip("\n")]
    lines.append("<dict>")

    lines.append("    <key>Label</key>")
    lines.append("    <string>{}</string>".format(_xml_escape(spec.name)))

    lines.append("    <key>ProgramArguments</key>")
    lines.append("    <array>")
    for token in spec.command:
        lines.append("        <string>{}</string>".format(_xml_escape(token)))
    lines.append("    </array>")

    if spec.working_dir is not None and len(spec.working_dir) > 0:
        lines.append("    <key>WorkingDirectory</key>")
        lines.append(
            "    <string>{}</string>".format(_xml_escape(spec.working_dir)),
        )

    if spec.user is not None and len(spec.user) > 0:
        lines.append("    <key>UserName</key>")
        lines.append(
            "    <string>{}</string>".format(_xml_escape(spec.user)),
        )

    if spec.env is not None and len(spec.env) > 0:
        lines.append("    <key>EnvironmentVariables</key>")
        lines.append("    <dict>")
        for env_key in sorted(spec.env.keys()):
            env_val = spec.env[env_key]
            lines.append(
                "        <key>{}</key>".format(_xml_escape(env_key)),
            )
            lines.append(
                "        <string>{}</string>".format(_xml_escape(env_val)),
            )
        lines.append("    </dict>")

    if schedule_dict.get("kind") == "interval":
        seconds = int(schedule_dict["seconds"])
        lines.append("    <key>StartInterval</key>")
        lines.append("    <integer>{}</integer>".format(seconds))
    elif schedule_dict.get("kind") == "calendar":
        hour = int(schedule_dict["Hour"])
        minute = int(schedule_dict["Minute"])
        lines.append("    <key>StartCalendarInterval</key>")
        lines.append("    <dict>")
        lines.append("        <key>Hour</key>")
        lines.append("        <integer>{}</integer>".format(hour))
        lines.append("        <key>Minute</key>")
        lines.append("        <integer>{}</integer>".format(minute))
        lines.append("    </dict>")

    if spec.run_at_load:
        lines.append("    <key>RunAtLoad</key>")
        lines.append("    <true/>")

    lines.append("</dict>")
    lines.append(_PLIST_FOOTER.rstrip("\n"))

    body = "\n".join(lines) + "\n"
    return body


def _xml_escape(value: str) -> str:
    """
        Escape ``value`` for safe inclusion as XML text.

        :param value: Raw text.

        :returns: Escaped text.
    """
    rtnval = (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return rtnval


def _parse_print_output(*, stdout: str) -> dict:
    """
        Parse ``launchctl print <domain>/<label>`` output into a
        flat ``key -> value`` dict.

        Reuses the same pattern as
        :class:`MacosServiceManager._parse_print_output` so the
        scheduled-tasks parser stays in sync with the service-manager
        parser.

        :param stdout: Raw stdout.

        :returns: ``dict[str, str]`` of stripped key-value pairs.
    """
    rtnval: dict = {}
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped == "{" or stripped == "}":
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        cleaned_key = key.strip()
        cleaned_value = value.strip().rstrip(";")
        if len(cleaned_key) == 0:
            continue
        cleaned_value = cleaned_value.rstrip("{").strip()
        rtnval[cleaned_key] = cleaned_value
    return rtnval


def _parse_print_disabled(*, stdout: str) -> set:
    """
        Parse ``launchctl print-disabled <domain>`` output into a set
        of disabled label strings.

        Output shape::

            com.apple.disabled-thing => disabled
            "com.example.user-thing" => enabled

        We collect labels whose verdict is ``disabled``.  Labels are
        unquoted to match what the caller passes in.

        :param stdout: Raw stdout.

        :returns: ``set[str]`` of disabled labels.
    """
    rtnval: set = set()
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if "=>" not in stripped:
            continue
        label_part, _, verdict_part = stripped.partition("=>")
        label = label_part.strip().strip('"')
        verdict = verdict_part.strip().rstrip(";").strip()
        if verdict.lower() != "true" and verdict.lower() != "disabled":
            # ``launchctl print-disabled`` prints either "=> true"
            # (disabled) or "=> false" (enabled) on modern macOS.
            # Older builds use the literal words.  We accept both.
            continue
        if len(label) > 0:
            rtnval.add(label)
    return rtnval


def _extract_schedule_from_plist(*, text: str) -> Optional[str]:
    """
        Parse the schedule out of a rendered plist body.

        Recognises the two shapes emitted by
        :func:`_render_plist_body`: a ``StartInterval`` integer or a
        ``StartCalendarInterval`` dict carrying ``Hour`` and
        ``Minute`` keys.  This is best-effort — third-party plists may
        carry different shapes that surface as ``None``.

        :param text: Raw plist body.

        :returns: Schedule string, or ``None`` when not derivable.
    """
    interval_match = re.search(
        r"<key>StartInterval</key>\s*<integer>(\d+)</integer>",
        text,
    )
    if interval_match is not None:
        seconds = int(interval_match.group(1))
        if seconds % 3600 == 0:
            rtnval: Optional[str] = "every {}hour".format(seconds // 3600)
        elif seconds % 60 == 0:
            rtnval = "every {}min".format(seconds // 60)
        else:
            rtnval = "every {}sec".format(seconds)
        return rtnval

    calendar_match = re.search(
        r"<key>StartCalendarInterval</key>\s*<dict>(.*?)</dict>",
        text,
        re.DOTALL,
    )
    if calendar_match is not None:
        inner = calendar_match.group(1)
        hour_match = re.search(
            r"<key>Hour</key>\s*<integer>(\d+)</integer>", inner,
        )
        minute_match = re.search(
            r"<key>Minute</key>\s*<integer>(\d+)</integer>", inner,
        )
        if hour_match is not None and minute_match is not None:
            hour = int(hour_match.group(1))
            minute = int(minute_match.group(1))
            rtnval = "daily {:02d}:{:02d}".format(hour, minute)
            return rtnval

    rtnval = None
    return rtnval


def _extract_program_arguments_from_plist(
    *,
    text: str,
) -> Optional[list[str]]:
    """
        Parse the ``ProgramArguments`` array out of a rendered plist
        body.

        :param text: Raw plist body.

        :returns: List of argv tokens, or ``None`` when the array is
                  not found.
    """
    array_match = re.search(
        r"<key>ProgramArguments</key>\s*<array>(.*?)</array>",
        text,
        re.DOTALL,
    )
    if array_match is None:
        rtnval: Optional[list[str]] = None
        return rtnval

    rtnval = []
    for token_match in re.finditer(
        r"<string>(.*?)</string>", array_match.group(1), re.DOTALL,
    ):
        token = token_match.group(1)
        unescaped = (
            token
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
        )
        rtnval.append(unescaped)
    return rtnval
