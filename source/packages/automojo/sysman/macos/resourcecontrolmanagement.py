"""
    macOS resource-control-management feature.

    Implements the cross-OS resource-control-management contract
    for macOS using launchd plist files plus best-effort
    ``taskpolicy`` per-PID QoS adjustment.

    macOS has NO cgroup-equivalent kernel substrate.  The cross-
    OS verb set is therefore satisfied with a mix of:

    * **launchd plists** — for persistent, per-group declared
      state.  Each group is represented by a plist at
      ``<plist_dir>/<label_prefix><name>.plist`` with a
      ``HardResourceLimits.RLIMIT_AS`` key when a memory ceiling
      is set, an ``AttachedPIDs`` array (manager-side
      bookkeeping), and a ``Disabled=true`` key so launchd does
      not try to start the placeholder daemon.
    * **``taskpolicy``** — for best-effort per-PID QoS class
      adjustment on attach / detach.  This is NOT a true
      kernel-side container — the PID is not placed in any
      group; only its scheduling QoS class is changed.

    macOS LIMITATIONS — PROMINENT:

    * ``set_cpu_limit`` returns ``FAILED`` with
      ``detail="macos_no_per_group_cpu_limit"`` because macOS
      has no per-group CPU-quota knob.
    * ``set_io_limits`` returns ``FAILED`` with
      ``detail="macos_no_per_group_io_limit"`` because macOS
      has no per-group IO bandwidth limit.
    * ``set_memory_limit`` only applies the cap to processes
      launchd spawns; running processes attached via
      ``attach_process`` are NOT retroactively limited.
    * ``attach_process`` / ``detach_process`` adjusts QoS class
      only; the PID is NOT a member of any kernel-side
      container.

    File I/O policy:
        Every read / write / probe is dispatched through the
        injected :class:`ICommandRunner` (``cat``, ``tee``,
        ``ls -1``, ``rm``, ``test -e``).  This module does NOT
        consume an :class:`IFileOperator`.

    Elevation:
        Writes honour ``config.elevated``.  Internal probes
        (``cat``, ``ls``, ``test -e``) and the dedicated read
        verbs (``list_groups``, ``get_group_usage``) NEVER
        elevate.
"""

import os
import re

from dataclasses import dataclass, field
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

_LAUNCHCTL = "launchctl"
_TASKPOLICY = "taskpolicy"
_TEE = "tee"
_CAT = "cat"
_LS = "ls"
_RM = "rm"
_TEST = "test"
_TOP = "top"

_DEFAULT_PLIST_DIR = "/Library/LaunchDaemons"
_DEFAULT_LABEL_PREFIX = "automojo-rc-"

_LAUNCHD_DOMAIN_SYSTEM = "system"

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

_PLIST_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0">\n'
)
_PLIST_FOOTER = "</plist>\n"

_DETAIL_NO_CPU_LIMIT = "macos_no_per_group_cpu_limit"
_DETAIL_NO_IO_LIMIT = "macos_no_per_group_io_limit"


MacosResourceControlActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosResourceControlManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`MacosResourceControlManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param plist_dir: Directory holding the manager's group
                          plists.  Defaults to
                          ``/Library/LaunchDaemons``.
        :param label_prefix: Prefix applied to every group's
                             launchd label.  Used by
                             :meth:`MacosResourceControlManager.list_groups`
                             to filter the directory listing.
                             Defaults to ``"automojo-rc-"``.
    """
    plist_dir: str = _DEFAULT_PLIST_DIR
    label_prefix: str = _DEFAULT_LABEL_PREFIX


@dataclass
class MacosResourceControlActionResult(FeatureActionResult):
    """
        Result of a resource-control write operation.

        :param group_name: Group name acted upon.
        :param pid: Process ID acted upon (only populated by
                    :meth:`MacosResourceControlManager.attach_process`
                    and :meth:`detach_process`).
        :param message: Optional short reason token for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None


@dataclass
class MacosIoLimitSpec:
    """
        IO-limit specification carried by
        :meth:`MacosResourceControlManager.set_io_limits`.

        Every field is recorded for cross-OS API parity; macOS
        ignores all of them and returns ``FAILED`` with
        ``detail="macos_no_per_group_io_limit"``.

        :param weight: Cross-OS IO weight; ignored on macOS.
        :param read_bps: Cross-OS read-bps cap; ignored.
        :param write_bps: Cross-OS write-bps cap; ignored.
    """
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None


def _empty_pid_list() -> list[int]:
    """
        Default-factory for
        :class:`MacosResourceGroupSpec.attached_pids`.

        :returns: A fresh empty list per call.
    """
    rtnval: list[int] = []
    return rtnval


@dataclass
class MacosResourceGroupSpec:
    """
        Specification used by
        :meth:`MacosResourceControlManager.create_group`.

        :param name: Group identifier.  Must match
                     ``^[A-Za-z0-9][A-Za-z0-9._\\-]*$``.
        :param cpu_percent: Recorded for cross-OS API parity;
                            ignored by the manager (macOS has no
                            per-group CPU quota).
        :param memory_bytes: Initial ``HardResourceLimits.RLIMIT_AS``
                             value in bytes.  ``None`` skips the
                             key from the rendered plist body.
        :param io_weight: Recorded for parity; ignored.
        :param io_read_bps: Recorded for parity; ignored.
        :param io_write_bps: Recorded for parity; ignored.
        :param attached_pids: PIDs whose QoS class has been
                              adjusted via ``taskpolicy``.  The
                              manager rewrites this list into the
                              plist's ``AttachedPIDs`` array on
                              every attach / detach.
    """
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=_empty_pid_list)


@dataclass
class MacosResourceGroupInfo:
    """
        Current state of a single resource group.

        :param name: Group name.
        :param cpu_percent: Always ``None`` on macOS.
        :param memory_bytes: ``HardResourceLimits.RLIMIT_AS``
                             parsed from the plist, or ``None``.
        :param io_weight: Always ``None``.
        :param io_read_bps: Always ``None``.
        :param io_write_bps: Always ``None``.
        :param attached_pids: PIDs from the plist's
                              ``AttachedPIDs`` array.
        :param is_active: ``True`` when the plist is present.
        :param effective_cpu_percent: Always ``None``.
        :param effective_memory_bytes: Same as ``memory_bytes``.
    """
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]


@dataclass
class MacosResourceGroupUsage:
    """
        Live usage snapshot for a single resource group.

        :param name: Group name.
        :param cpu_usage_seconds: Always ``None`` (``top -l 1``
                                  on macOS reports CPU percent,
                                  not seconds).
        :param memory_used_bytes: Sum of ``MEM`` columns parsed
                                  from per-PID ``top``
                                  snapshots.  ``None`` when
                                  there are no attached PIDs.
        :param io_read_bytes: Always ``None`` (macOS ``top``
                              does not report per-PID IO
                              bytes).
        :param io_write_bytes: Always ``None``.
        :param process_count: Number of attached PIDs.
    """
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int


class MacosResourceControlQueryError(Exception):
    """
        Raised when a resource-control read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        detail: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param detail: Optional short reason token (e.g.
                           ``"unknown_group"``).
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr
        self.detail = detail

        err_msg_lines = ["Resource-control query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        if detail is not None:
            err_msg_lines.append("    DETAIL: {}".format(detail))

        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosResourceControlManager(BaseFeatureManager):
    """
        macOS launchd-and-taskpolicy resource-control manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  See module docstring for the
        documented macOS limitations.
    """

    result_class = MacosResourceControlActionResult
    status_class = MacosResourceControlActionStatus
    feature_name = "resource-control-management"

    def __init__(
        self,
        runner,
        config: Optional[MacosResourceControlManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`MacosResourceControlManagerConfig`
                           when omitted so the verb
                           implementations can rely on
                           ``self._config.plist_dir`` and
                           ``self._config.label_prefix`` always
                           being present.
        """
        if config is None:
            config = MacosResourceControlManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: MacosResourceGroupSpec,
    ) -> MacosResourceControlActionResult:
        """
            Create or update a resource group.

            Renders the launchd plist body, writes it via
            ``tee``, then dispatches
            ``launchctl bootstrap system <plist_path>``.

            Idempotent: when the on-disk plist matches the
            rendered body byte-for-byte, returns ``SKIPPED``.

            :param spec: The :class:`MacosResourceGroupSpec`.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        try:
            _validate_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=spec.name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        plist_path = self._plist_path(name=spec.name)
        rendered_body = _render_plist_body(
            spec=spec, label_prefix=self._config.label_prefix,
        )
        write_command = [_TEE, plist_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=spec.name,
                command=write_command,
            )
            return dry

        existing_body = self._read_file(path=plist_path)
        if existing_body is not None and existing_body == rendered_body:
            skipped = self._build_result(
                status=MacosResourceControlActionStatus.SKIPPED,
                group_name=spec.name,
                command=write_command,
                message="group plist already matches",
            )
            return skipped

        # Write the plist body.
        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_body,
        )
        if write_run.exit_code != 0:
            failed = MacosResourceControlActionResult(
                status=MacosResourceControlActionStatus.FAILED,
                detail=spec.name,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                group_name=spec.name,
                message="plist_write_failed",
            )
            return failed

        # launchctl bootstrap.  Tolerate "already loaded" — the
        # desired end state is "the plist is loaded into the
        # domain", which holds whether bootstrap created the
        # entry or it was already present.
        bootstrap_command = [
            _LAUNCHCTL, "bootstrap", _LAUNCHD_DOMAIN_SYSTEM, plist_path,
        ]
        bootstrap_run = self._runner.run(
            list(bootstrap_command), elevated=self._config.elevated,
        )

        if bootstrap_run.exit_code == 0:
            mapped_status = MacosResourceControlActionStatus.SUCCESS
            message = None
        else:
            stderr_lower = bootstrap_run.stderr.lower()
            already_loaded = (
                "service already loaded" in stderr_lower
                or "already bootstrapped" in stderr_lower
            )
            if already_loaded:
                mapped_status = MacosResourceControlActionStatus.SUCCESS
                message = None
            else:
                mapped_status = MacosResourceControlActionStatus.FAILED
                message = "bootstrap_failed"

        rtnval = MacosResourceControlActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=bootstrap_run.exit_code,
            stdout=bootstrap_run.stdout,
            stderr=bootstrap_run.stderr,
            command=list(bootstrap_command),
            group_name=spec.name,
            message=message,
        )
        return rtnval

    def delete_group(
        self,
        name: str,
    ) -> MacosResourceControlActionResult:
        """
            Delete a resource group.

            Dispatches ``launchctl bootout system/<label>`` then
            ``rm -f <plist_path>``.

            Idempotent: when the plist file does not exist,
            returns ``SKIPPED`` without invoking the runner.

            :param name: Group name.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        plist_path = self._plist_path(name=name)
        target = "{}/{}".format(
            _LAUNCHD_DOMAIN_SYSTEM, self._label_for(name=name),
        )
        bootout_command = [_LAUNCHCTL, "bootout", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=bootout_command,
            )
            return dry

        if not self._path_exists(path=plist_path):
            skipped = self._build_result(
                status=MacosResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=bootout_command,
                message="group plist not found",
            )
            return skipped

        # bootout is informational; an unloaded label reports a
        # non-zero exit which we ignore so the rm can proceed.
        self._runner.run(
            list(bootout_command), elevated=self._config.elevated,
        )

        rm_command = [_RM, "-f", plist_path]
        rm_run = self._runner.run(
            list(rm_command), elevated=self._config.elevated,
        )

        if rm_run.exit_code == 0:
            mapped_status = MacosResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = MacosResourceControlActionStatus.FAILED
            message = "plist_delete_failed"

        rtnval = MacosResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=rm_run.exit_code,
            stdout=rm_run.stdout,
            stderr=rm_run.stderr,
            command=list(rm_command),
            group_name=name,
            message=message,
        )
        return rtnval

    def set_cpu_limit(
        self,
        name: str,
        percent: int,
    ) -> MacosResourceControlActionResult:
        """
            Set the group's CPU ceiling.

            macOS LIMITATION: macOS has NO per-group CPU-quota
            knob.  This method always returns ``FAILED`` with
            ``detail="macos_no_per_group_cpu_limit"`` and does
            NOT invoke the runner.  The ``command`` field on
            the result carries the documented best-effort
            per-PID argv (``["taskpolicy", "-c", "utility"]``)
            for audit.

            :param name: Group name.
            :param percent: Ignored (recorded on the result for
                            audit).

            :returns: The
                      :class:`MacosResourceControlActionResult`
                      with ``status=FAILED``.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        documented_argv = [_TASKPOLICY, "-c", "utility"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=documented_argv,
            )
            return dry

        failed = MacosResourceControlActionResult(
            status=MacosResourceControlActionStatus.FAILED,
            detail=_DETAIL_NO_CPU_LIMIT,
            exit_code=None,
            stdout=None,
            stderr=(
                "macOS launchd has no per-group CPU quota; "
                "use taskpolicy -c <class> -p <pid> per process "
                "for QoS adjustment only."
            ),
            command=list(documented_argv),
            group_name=name,
            message="macOS launchd has no per-group CPU quota",
        )
        return failed

    def set_memory_limit(
        self,
        name: str,
        bytes_: int,
    ) -> MacosResourceControlActionResult:
        """
            Set the group's memory ceiling.

            Re-renders the plist with the new
            ``HardResourceLimits.RLIMIT_AS`` value, writes it via
            ``tee``, then re-bootstraps the launchd label
            (``launchctl bootout`` + ``launchctl bootstrap``).

            macOS LIMITATION: ``RLIMIT_AS`` only applies to
            processes launchd spawns under the daemon's
            ``ProgramArguments``.  Running processes attached
            via :meth:`attach_process` are NOT retroactively
            limited.

            Idempotent: when the on-disk plist's
            ``RLIMIT_AS`` already equals ``bytes_``, returns
            ``SKIPPED``.

            :param name: Group name.
            :param bytes_: New memory ceiling in bytes.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        plist_path = self._plist_path(name=name)
        write_command = [_TEE, plist_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=write_command,
            )
            return dry

        existing_body = self._read_file(path=plist_path)
        if existing_body is None:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                command=write_command,
                message="group_not_found",
                stderr="group plist not found: {}".format(plist_path),
            )
            return failed

        existing_memory = _parse_plist_memory_bytes(body=existing_body)
        if existing_memory == bytes_:
            skipped = self._build_result(
                status=MacosResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=write_command,
                message="memory limit already at requested value",
            )
            return skipped

        existing_pids = _parse_plist_attached_pids(body=existing_body)
        new_spec = MacosResourceGroupSpec(
            name=name,
            memory_bytes=bytes_,
            attached_pids=existing_pids,
        )
        new_body = _render_plist_body(
            spec=new_spec, label_prefix=self._config.label_prefix,
        )

        rtnval = self._write_and_rebootstrap(
            name=name,
            plist_path=plist_path,
            body=new_body,
            failure_token="memory_limit_apply_failed",
        )
        return rtnval

    def set_io_limits(
        self,
        name: str,
        spec: MacosIoLimitSpec,
    ) -> MacosResourceControlActionResult:
        """
            Set per-group IO ceilings.

            macOS LIMITATION: macOS has NO per-group IO
            bandwidth limit.  This method always returns
            ``FAILED`` with
            ``detail="macos_no_per_group_io_limit"`` and does
            NOT invoke the runner.

            :param name: Group name.
            :param spec: Ignored (recorded on the result for
                         audit).

            :returns: The
                      :class:`MacosResourceControlActionResult`
                      with ``status=FAILED``.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        documented_argv = [_TASKPOLICY, "-d", "background"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=documented_argv,
            )
            return dry

        failed = MacosResourceControlActionResult(
            status=MacosResourceControlActionStatus.FAILED,
            detail=_DETAIL_NO_IO_LIMIT,
            exit_code=None,
            stdout=None,
            stderr=(
                "macOS has no per-group IO bandwidth limit; "
                "use taskpolicy -d <class> -p <pid> per process "
                "for I/O priority class only."
            ),
            command=list(documented_argv),
            group_name=name,
            message=(
                "macOS has no per-group IO bandwidth limit; "
                "per-PID I/O priority via taskpolicy -d only"
            ),
        )
        return failed

    def attach_process(
        self,
        name: str,
        pid: int,
    ) -> MacosResourceControlActionResult:
        """
            Attach an existing PID to the resource group via
            ``taskpolicy -p <pid> -c utility``.

            macOS LIMITATION: ``taskpolicy`` adjusts the QoS
            class only; the PID is NOT placed in any
            kernel-side container, and the group's policy
            ceilings (e.g. memory) are NOT retroactively
            applied.  The plist's ``AttachedPIDs`` array is
            purely a manager-side bookkeeping artefact.

            Idempotent: when the PID is already in the plist's
            ``AttachedPIDs``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_attach_arguments",
                stderr=str(exc),
            )
            return failed

        plist_path = self._plist_path(name=name)
        taskpolicy_command = [
            _TASKPOLICY, "-p", "{}".format(pid), "-c", "utility",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
            )
            return dry

        existing_body = self._read_file(path=plist_path)
        if existing_body is None:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
                message="group_not_found",
                stderr="group plist not found: {}".format(plist_path),
            )
            return failed

        attached = _parse_plist_attached_pids(body=existing_body)
        if pid in attached:
            skipped = self._build_result(
                status=MacosResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
                message="pid already attached",
            )
            return skipped

        # Run taskpolicy first; its failure short-circuits the
        # plist update so we don't record an unrealised attach.
        tp_run = self._runner.run(
            list(taskpolicy_command), elevated=self._config.elevated,
        )
        if tp_run.exit_code != 0:
            failed = MacosResourceControlActionResult(
                status=MacosResourceControlActionStatus.FAILED,
                detail=name,
                exit_code=tp_run.exit_code,
                stdout=tp_run.stdout,
                stderr=tp_run.stderr,
                command=list(taskpolicy_command),
                group_name=name,
                pid=pid,
                message="taskpolicy_failed",
            )
            return failed

        # Re-render the plist with the new pid in AttachedPIDs.
        new_pids = list(attached) + [pid]
        existing_memory = _parse_plist_memory_bytes(body=existing_body)
        new_spec = MacosResourceGroupSpec(
            name=name,
            memory_bytes=existing_memory,
            attached_pids=new_pids,
        )
        new_body = _render_plist_body(
            spec=new_spec, label_prefix=self._config.label_prefix,
        )

        rtnval = self._write_and_rebootstrap(
            name=name,
            plist_path=plist_path,
            body=new_body,
            failure_token="attach_apply_failed",
            pid=pid,
        )
        return rtnval

    def detach_process(
        self,
        name: str,
        pid: int,
    ) -> MacosResourceControlActionResult:
        """
            Detach a PID from the resource group via
            ``taskpolicy -p <pid> -c default``.

            Idempotent: when the PID is NOT currently in the
            plist's ``AttachedPIDs``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_detach_arguments",
                stderr=str(exc),
            )
            return failed

        plist_path = self._plist_path(name=name)
        taskpolicy_command = [
            _TASKPOLICY, "-p", "{}".format(pid), "-c", "default",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
            )
            return dry

        existing_body = self._read_file(path=plist_path)
        if existing_body is None:
            failed = self._build_result(
                status=MacosResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
                message="group_not_found",
                stderr="group plist not found: {}".format(plist_path),
            )
            return failed

        attached = _parse_plist_attached_pids(body=existing_body)
        if pid not in attached:
            skipped = self._build_result(
                status=MacosResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=taskpolicy_command,
                message="pid not currently attached",
            )
            return skipped

        tp_run = self._runner.run(
            list(taskpolicy_command), elevated=self._config.elevated,
        )
        if tp_run.exit_code != 0:
            failed = MacosResourceControlActionResult(
                status=MacosResourceControlActionStatus.FAILED,
                detail=name,
                exit_code=tp_run.exit_code,
                stdout=tp_run.stdout,
                stderr=tp_run.stderr,
                command=list(taskpolicy_command),
                group_name=name,
                pid=pid,
                message="taskpolicy_failed",
            )
            return failed

        new_pids = [p for p in attached if p != pid]
        existing_memory = _parse_plist_memory_bytes(body=existing_body)
        new_spec = MacosResourceGroupSpec(
            name=name,
            memory_bytes=existing_memory,
            attached_pids=new_pids,
        )
        new_body = _render_plist_body(
            spec=new_spec, label_prefix=self._config.label_prefix,
        )

        rtnval = self._write_and_rebootstrap(
            name=name,
            plist_path=plist_path,
            body=new_body,
            failure_token="detach_apply_failed",
            pid=pid,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_groups(self) -> list[MacosResourceGroupInfo]:
        """
            Enumerate every resource group present under the
            plist directory.

            Lists ``<plist_dir>`` via ``ls -1`` and selects every
            entry matching ``<label_prefix>*.plist``.  Reads do
            not elevate.

            :returns: One
                      :class:`MacosResourceGroupInfo` per
                      detected plist.  Empty list when no
                      matching plist is present.
            :raises MacosResourceControlQueryError: When ``ls``
                                                    fails with a
                                                    non-zero exit
                                                    and non-empty
                                                    stderr.
        """
        ls_command = [_LS, "-1", self._config.plist_dir]
        ls_run = self._runner.run(list(ls_command), elevated=False)
        if ls_run.exit_code != 0 and len(ls_run.stderr.strip()) > 0:
            raise MacosResourceControlQueryError(
                command=ls_command,
                exit_code=ls_run.exit_code,
                stderr=ls_run.stderr,
            )

        rtnval: list[MacosResourceGroupInfo] = []
        prefix = self._config.label_prefix
        for raw in ls_run.stdout.splitlines():
            entry = raw.strip()
            if len(entry) == 0:
                continue
            if not entry.startswith(prefix):
                continue
            if not entry.endswith(".plist"):
                continue

            label = entry[: -len(".plist")]
            name = label[len(prefix):]

            plist_path = "{}/{}".format(self._config.plist_dir, entry)
            body = self._read_file(path=plist_path)
            if body is None:
                continue

            memory_bytes = _parse_plist_memory_bytes(body=body)
            attached_pids = _parse_plist_attached_pids(body=body)

            info = MacosResourceGroupInfo(
                name=name,
                cpu_percent=None,
                memory_bytes=memory_bytes,
                io_weight=None,
                io_read_bps=None,
                io_write_bps=None,
                attached_pids=attached_pids,
                is_active=True,
                effective_cpu_percent=None,
                effective_memory_bytes=memory_bytes,
            )
            rtnval.append(info)

        return rtnval

    def get_group_usage(
        self,
        name: str,
    ) -> MacosResourceGroupUsage:
        """
            Read live usage counters for a single resource
            group.

            Aggregates ``top -l 1 -stats pid,cpu,mem -pid <pid>``
            output across every PID in the plist's
            ``AttachedPIDs`` array.  Reads do not elevate.

            :param name: Group name.

            :returns: The
                      :class:`MacosResourceGroupUsage`.
            :raises MacosResourceControlQueryError: When the
                                                    plist file
                                                    does not
                                                    exist
                                                    (raised with
                                                    ``detail="unknown_group"``).
        """
        _validate_name(name=name)

        plist_path = self._plist_path(name=name)
        body = self._read_file(path=plist_path)
        if body is None:
            raise MacosResourceControlQueryError(
                command=[_CAT, plist_path],
                exit_code=1,
                stderr="group plist not found: {}".format(plist_path),
                detail="unknown_group",
            )

        attached_pids = _parse_plist_attached_pids(body=body)

        if len(attached_pids) == 0:
            rtnval = MacosResourceGroupUsage(
                name=name,
                cpu_usage_seconds=None,
                memory_used_bytes=None,
                io_read_bytes=None,
                io_write_bytes=None,
                process_count=0,
            )
            return rtnval

        total_memory = 0
        memory_seen = False
        for pid in attached_pids:
            top_command = [
                _TOP, "-l", "1", "-stats", "pid,cpu,mem",
                "-pid", "{}".format(pid),
            ]
            top_run = self._runner.run(list(top_command), elevated=False)
            if top_run.exit_code != 0 and len(top_run.stderr.strip()) > 0:
                raise MacosResourceControlQueryError(
                    command=top_command,
                    exit_code=top_run.exit_code,
                    stderr=top_run.stderr,
                )
            cpu_percent, memory_bytes = _parse_top_output(
                stdout=top_run.stdout,
            )
            if memory_bytes is not None:
                total_memory = total_memory + memory_bytes
                memory_seen = True

        if memory_seen:
            memory_used: Optional[int] = total_memory
        else:
            memory_used = None

        rtnval = MacosResourceGroupUsage(
            name=name,
            cpu_usage_seconds=None,
            memory_used_bytes=memory_used,
            io_read_bytes=None,
            io_write_bytes=None,
            process_count=len(attached_pids),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _plist_path(self, *, name: str) -> str:
        """
            :returns: ``<plist_dir>/<label_prefix><name>.plist``.
        """
        rtnval = "{}/{}{}.plist".format(
            self._config.plist_dir, self._config.label_prefix, name,
        )
        return rtnval

    def _label_for(self, *, name: str) -> str:
        """
            :returns: ``<label_prefix><name>``.
        """
        rtnval = "{}{}".format(self._config.label_prefix, name)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.  Reads do not elevate.

            :param path: Absolute path.

            :returns: File contents, or ``None`` when the file
                      is missing.
        """
        command = [_CAT, path]
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
        command = [_TEST, "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _write_and_rebootstrap(
        self,
        *,
        name: str,
        plist_path: str,
        body: str,
        failure_token: str,
        pid: Optional[int] = None,
    ) -> MacosResourceControlActionResult:
        """
            Write the plist body, then bootout + bootstrap to
            apply.

            :param name: Group name.
            :param plist_path: Absolute plist path.
            :param body: Rendered plist body.
            :param failure_token: Short reason token used in the
                                  ``message`` field on FAILED.
            :param pid: Optional PID to echo onto the result.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        write_command = [_TEE, plist_path]
        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=body,
        )
        if write_run.exit_code != 0:
            failed = MacosResourceControlActionResult(
                status=MacosResourceControlActionStatus.FAILED,
                detail=name,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                group_name=name,
                pid=pid,
                message="plist_write_failed",
            )
            return failed

        # bootout (informational; ignore non-zero).
        target = "{}/{}".format(
            _LAUNCHD_DOMAIN_SYSTEM, self._label_for(name=name),
        )
        self._runner.run(
            [_LAUNCHCTL, "bootout", target],
            elevated=self._config.elevated,
        )

        # bootstrap.
        bootstrap_command = [
            _LAUNCHCTL, "bootstrap", _LAUNCHD_DOMAIN_SYSTEM, plist_path,
        ]
        bootstrap_run = self._runner.run(
            list(bootstrap_command), elevated=self._config.elevated,
        )

        if bootstrap_run.exit_code == 0:
            mapped_status = MacosResourceControlActionStatus.SUCCESS
            message = None
        else:
            stderr_lower = bootstrap_run.stderr.lower()
            already_loaded = (
                "service already loaded" in stderr_lower
                or "already bootstrapped" in stderr_lower
            )
            if already_loaded:
                mapped_status = MacosResourceControlActionStatus.SUCCESS
                message = None
            else:
                mapped_status = MacosResourceControlActionStatus.FAILED
                message = failure_token

        rtnval = MacosResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=bootstrap_run.exit_code,
            stdout=bootstrap_run.stdout,
            stderr=bootstrap_run.stderr,
            command=list(bootstrap_command),
            group_name=name,
            pid=pid,
            message=message,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosResourceControlActionStatus,
        group_name: Optional[str],
        command: list[str],
        pid: Optional[int] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosResourceControlActionResult:
        """
            Construct a
            :class:`MacosResourceControlActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param group_name: Group name.
            :param command: Would-have-been argv (copied for
                            capture).
            :param pid: Optional PID echoed onto the result.
            :param message: Optional short reason token.
            :param stderr: Optional synthetic stderr payload.

            :returns: The
                      :class:`MacosResourceControlActionResult`.
        """
        rtnval = MacosResourceControlActionResult(
            status=status,
            detail=group_name,
            command=list(command),
            group_name=group_name,
            pid=pid,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — validation, rendering, parsing
# ----------------------------------------------------------------------


def _validate_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` does not match the
        allowed character set.

        :param name: Candidate group name.
        :raises ValueError: When ``name`` does not match
                            ``[A-Za-z0-9][A-Za-z0-9._\\-]*``.
    """
    if name is None:
        err_msg = "group name must not be None"
        raise ValueError(err_msg)
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid group name: {!r}".format(name)
        raise ValueError(err_msg)
    return


def _validate_pid(*, pid: int) -> None:
    """
        Raise ``ValueError`` when ``pid`` is not a positive
        integer.

        :param pid: Candidate PID.
        :raises ValueError: When ``pid`` is None or non-positive.
    """
    if pid is None:
        err_msg = "pid must not be None"
        raise ValueError(err_msg)
    if pid <= 0:
        err_msg = "pid must be a positive integer: {}".format(pid)
        raise ValueError(err_msg)
    return


def _render_plist_body(
    *,
    spec: MacosResourceGroupSpec,
    label_prefix: str,
) -> str:
    """
        Render the launchd plist body for ``spec``.

        Body shape::

            <?xml ... ?>
            <!DOCTYPE ... >
            <plist version="1.0">
            <dict>
              <key>Label</key>
              <string><label_prefix><name></string>
              <key>HardResourceLimits</key>
              <dict>
                <key>RLIMIT_AS</key>
                <integer><memory_bytes></integer>
              </dict>
              <key>ProgramArguments</key>
              <array>
                <string>/usr/bin/true</string>
              </array>
              <key>Disabled</key>
              <true/>
              <key>AttachedPIDs</key>
              <array>
                <integer>...</integer>
              </array>
            </dict>
            </plist>

        ``HardResourceLimits`` is omitted entirely when
        ``spec.memory_bytes`` is None.

        :param spec: The resource group spec.
        :param label_prefix: Prefix prepended to ``spec.name`` to
                             form the launchd label.

        :returns: The rendered plist body ending in ``\\n``.
    """
    label = "{}{}".format(label_prefix, spec.name)
    lines: list[str] = []
    lines.append(_PLIST_HEADER.rstrip("\n"))
    lines.append("<dict>")
    lines.append("    <key>Label</key>")
    lines.append("    <string>{}</string>".format(label))

    if spec.memory_bytes is not None:
        lines.append("    <key>HardResourceLimits</key>")
        lines.append("    <dict>")
        lines.append("        <key>RLIMIT_AS</key>")
        lines.append("        <integer>{}</integer>".format(spec.memory_bytes))
        lines.append("    </dict>")

    lines.append("    <key>ProgramArguments</key>")
    lines.append("    <array>")
    lines.append("        <string>/usr/bin/true</string>")
    lines.append("    </array>")
    lines.append("    <key>Disabled</key>")
    lines.append("    <true/>")
    lines.append("    <key>AttachedPIDs</key>")
    lines.append("    <array>")
    for pid in spec.attached_pids:
        lines.append("        <integer>{}</integer>".format(pid))
    lines.append("    </array>")
    lines.append("</dict>")
    lines.append(_PLIST_FOOTER.rstrip("\n"))

    body = "\n".join(lines) + "\n"
    return body


_RLIMIT_AS_RE = re.compile(
    r"<key>\s*RLIMIT_AS\s*</key>\s*<integer>\s*(\d+)\s*</integer>"
)
_ATTACHED_PIDS_BLOCK_RE = re.compile(
    r"<key>\s*AttachedPIDs\s*</key>\s*<array>(.*?)</array>",
    re.DOTALL,
)
_INTEGER_RE = re.compile(r"<integer>\s*(\d+)\s*</integer>")


def _parse_plist_memory_bytes(*, body: str) -> Optional[int]:
    """
        Extract the ``RLIMIT_AS`` integer from a plist body.

        :param body: Raw plist body.

        :returns: Integer bytes, or ``None`` when the key is
                  not present or unparseable.
    """
    if body is None:
        rtnval: Optional[int] = None
        return rtnval

    match = _RLIMIT_AS_RE.search(body)
    if match is None:
        rtnval = None
        return rtnval

    raw = match.group(1)
    try:
        rtnval = int(raw)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_plist_attached_pids(*, body: str) -> list[int]:
    """
        Extract the ``AttachedPIDs`` integer array from a plist
        body.

        :param body: Raw plist body.

        :returns: List of parsed PIDs.  Empty when the key is
                  missing or empty.
    """
    if body is None:
        rtnval: list[int] = []
        return rtnval

    block_match = _ATTACHED_PIDS_BLOCK_RE.search(body)
    if block_match is None:
        rtnval = []
        return rtnval

    block = block_match.group(1)
    rtnval = []
    for int_match in _INTEGER_RE.finditer(block):
        raw = int_match.group(1)
        try:
            pid_value = int(raw)
        except ValueError:
            continue
        rtnval.append(pid_value)
    return rtnval


def _parse_top_output(*, stdout: str) -> tuple:
    """
        Parse a single ``top -l 1 -stats pid,cpu,mem -pid
        <pid>`` snapshot.

        ``top`` output on macOS for a single PID has a final
        block of the form::

            PID    %CPU MEM
            12345  3.2  104857600

        We pick out the last whitespace-tokenised line that
        looks like the data row (numeric leading PID).

        Memory is reported in bytes when ``-stats mem`` is
        used; ``top``'s default suffixed form (e.g. ``5M``)
        is also accepted.

        :param stdout: Raw ``top`` stdout.

        :returns: ``(cpu_percent, memory_bytes)``; either may
                  be ``None`` when the column was not parsed.
    """
    if stdout is None or len(stdout) == 0:
        rtnval = (None, None)
        return rtnval

    cpu_percent: Optional[float] = None
    memory_bytes: Optional[int] = None

    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if len(line) == 0:
            continue
        tokens = line.split()
        if len(tokens) < 3:
            continue
        # First token must be a positive integer (PID).
        try:
            int(tokens[0])
        except ValueError:
            continue

        try:
            cpu_percent = float(tokens[1])
        except ValueError:
            cpu_percent = None

        memory_bytes = _interpret_top_memory_token(token=tokens[2])
        break

    rtnval = (cpu_percent, memory_bytes)
    return rtnval


_MEMORY_SUFFIXES = {
    "K": 1024,
    "M": 1024 * 1024,
    "G": 1024 * 1024 * 1024,
    "T": 1024 * 1024 * 1024 * 1024,
}


def _interpret_top_memory_token(*, token: str) -> Optional[int]:
    """
        Interpret a ``top``-style memory token.

        ``top -stats mem`` reports plain decimal byte counts.
        ``top``'s default ``MEM`` column reports a suffixed
        value such as ``"5M"`` or ``"3.2G"``.  This helper
        accepts both.

        :param token: Memory column value.

        :returns: Integer bytes, or ``None`` when the token
                  does not parse.
    """
    if token is None or len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    candidate = token.strip()

    # Try plain decimal first.
    try:
        rtnval = int(candidate)
        return rtnval
    except ValueError:
        pass

    suffix = candidate[-1].upper()
    numeric_part = candidate[:-1]
    if suffix not in _MEMORY_SUFFIXES:
        rtnval = None
        return rtnval

    try:
        magnitude = float(numeric_part)
    except ValueError:
        rtnval = None
        return rtnval

    multiplier = _MEMORY_SUFFIXES[suffix]
    rtnval = int(round(magnitude * multiplier))
    return rtnval
