"""
    macOS kdump-management feature.

    macOS has no kdump equivalent.  The native kernel-crash-capture
    model is fundamentally different from Linux's pre-reserved-memory
    kdump:

        * Kernel panic logs land at
          ``/Library/Logs/DiagnosticReports/*.panic`` and ``*.ips``
          automatically -- there is no opt-in service to enable.
        * No memory is pre-reserved for a capture kernel; the panic
          handler writes a stack-trace text file directly from the
          panicking kernel context.
        * There is no runtime-configurable dump target; dumps always
          land in ``/Library/Logs/DiagnosticReports/`` (system-wide)
          and ``~/Library/Logs/DiagnosticReports/`` (per-user).
        * For deeper system-state captures, operators run
          ``sysdiagnose`` ad hoc.

    Implication for the cross-OS contract:

        * ``enable_kdump``, ``disable_kdump``, and
          ``configure_kdump_target`` MUST return ``FAILED`` with a
          descriptive ``message`` rather than pretend to succeed.  The
          kdump-management contract explicitly permits non-silent
          rejection of out-of-scope verbs on platforms that have no
          equivalent.
        * ``get_kdump_status`` returns a synthetic always-on status
          pointing at ``/Library/Logs/DiagnosticReports``.
        * ``list_recent_dumps`` enumerates ``*.panic`` and ``*.ips``
          files in the system-wide DiagnosticReports directory.
        * ``clear_old_dumps`` runs `find -mtime +N -exec rm`.
        * ``get_dump_info`` `cat`s the first ~30 lines of the panic
          report and surfaces the kernel string and panic message.

    File I/O policy:
        Every file read / write is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``ls`` / ``find`` /
        ``stat``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        ``clear_old_dumps`` honours ``config.elevated`` (purging
        ``/Library/Logs/DiagnosticReports`` typically requires admin).
        Reads (``get_kdump_status``, ``list_recent_dumps``,
        ``get_dump_info``) and the unsupported-write stubs never
        elevate.
"""

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


_CAT = "cat"
_LS = "ls"
_FIND = "find"
_STAT = "stat"

_DEFAULT_REPORT_DIR = "/Library/Logs/DiagnosticReports"
_PANIC_EXT = ".panic"
_IPS_EXT = ".ips"

_UNSUPPORTED_MESSAGE = (
    "macOS has no kdump equivalent; kernel-panic capture is always-on. "
    "Use sysdiagnose for system-state captures."
)


MacosKdumpActionStatus = ActionStatus


class MacosKdumpTargetKind(Enum):
    """
        macOS kdump target kinds.

        Only ``LOCAL_PATH`` is meaningful on macOS; the other contract-
        defined values are intentionally absent because macOS panic
        captures cannot be redirected to remote SSH / NFS endpoints
        without OS-level customisation that is out of scope here.
    """
    LOCAL_PATH = "local_path"


@dataclass
class MacosKdumpManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosKdumpManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param report_dir: Directory holding macOS kernel-panic reports.
                           Default: ``/Library/Logs/DiagnosticReports``.
                           Used as the search root for
                           ``list_recent_dumps``, ``clear_old_dumps``,
                           and ``get_dump_info``.
    """
    report_dir: str = _DEFAULT_REPORT_DIR


@dataclass
class MacosKdumpActionResult(FeatureActionResult):
    """
        Result of a kdump-management write operation.

        :param target: The dump target affected, when applicable
                       (typically the report directory path for
                       configure/clear verbs).  ``None`` for the
                       unsupported-write stubs.
        :param requires_reboot: Always ``False`` on macOS -- panic
                                capture is kernel-resident and never
                                reboot-gated.  Carried for cross-OS
                                contract conformance.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes (especially the macOS-
                        unsupported rationale).
    """
    target: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class MacosKdumpTargetSpec:
    """
        Desired macOS kdump target.

        Only ``LOCAL_PATH`` is honoured; other kinds (when added in
        future) would be rejected with ``FAILED``.

        :param target_kind: The :class:`MacosKdumpTargetKind` selector.
        :param path: Local filesystem path for ``LOCAL_PATH``.
        :param ssh_user: Reserved for cross-OS contract conformance;
                         macOS rejects SSH targets.
        :param ssh_host: Reserved for cross-OS contract conformance;
                         macOS rejects SSH targets.
        :param nfs_export: Reserved for cross-OS contract conformance;
                           macOS rejects NFS targets.
        :param compression: Reserved; macOS does not configure
                            compression for panic logs.
    """
    target_kind: MacosKdumpTargetKind
    path: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_host: Optional[str] = None
    nfs_export: Optional[str] = None
    compression: Optional[str] = None


@dataclass
class MacosKdumpStatus:
    """
        Snapshot of the current macOS panic-capture state.

        macOS panic logging is always-on.  ``enabled`` and ``running``
        are therefore both reported as ``True`` for any healthy host;
        ``message`` carries the explanatory advisory.

        :param enabled: Always ``True`` on macOS.
        :param running: Always ``True`` on macOS.
        :param target_kind: Always
                            :data:`MacosKdumpTargetKind.LOCAL_PATH`.
        :param target_path: The fixed
                            ``/Library/Logs/DiagnosticReports`` path.
        :param kernel: Reserved; macOS does not surface a separate
                       capture-kernel image.  Always ``None``.
        :param reserved_memory_mb: Reserved; always ``None`` on macOS.
        :param message: Always populated with the advisory describing
                        the macOS native model.
    """
    enabled: bool
    running: bool
    target_kind: Optional[MacosKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None
    reserved_memory_mb: Optional[int] = None
    message: Optional[str] = None


@dataclass
class MacosKdumpInfo:
    """
        Metadata for a single macOS panic / diagnostic-report
        artifact.

        :param id: Filename of the report (e.g.
                   ``Kernel-2026-04-24-093131.panic``).
        :param path: Absolute path to the report file.
        :param created: Modification timestamp as reported by ``stat``,
                        free-form text.
        :param size_bytes: On-disk size in bytes, when known.
        :param kernel: First ``Darwin Kernel Version`` line parsed from
                       the report, when available.
        :param panic_message: First ``panic(`` line parsed from the
                              report, when available.
    """
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None
    panic_message: Optional[str] = None


class MacosKdumpQueryError(Exception):
    """
        Raised when a kdump-management read fails for a reason other
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

        err_msg_lines = ["macOS kdump query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_panic_metadata(*, text: str) -> dict:
    """
        Best-effort parse of the first ~30 lines of a macOS panic /
        diagnostic-report text file.

        :param text: Raw report text.

        :returns: A dict that may carry ``kernel`` and / or
                  ``panic_message`` keys.
    """
    rtnval: dict = {}
    line_count = 0
    for raw_line in text.splitlines():
        line_count = line_count + 1
        if line_count > 30:
            break
        line = raw_line.strip()

        if rtnval.get("kernel") is None and "Darwin Kernel Version" in line:
            rtnval["kernel"] = line
            continue

        if rtnval.get("panic_message") is None and "panic(" in line:
            rtnval["panic_message"] = line
            continue

    return rtnval


# ----------------------------------------------------------------------
# MacosKdumpManager
# ----------------------------------------------------------------------


class MacosKdumpManager(BaseFeatureManager):
    """
        macOS kdump manager -- a contract-conforming skeleton.

        Kernel-panic capture on macOS is always-on and not user-
        configurable, so the write verbs (``enable_kdump``,
        ``disable_kdump``, ``configure_kdump_target``) return ``FAILED``
        with a descriptive ``message`` rather than pretend to act.
        ``clear_old_dumps`` is the only true mutating verb on this
        manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  ``clear_old_dumps`` honours
        ``config.elevated``; everything else passes
        ``elevated=False``.
    """

    result_class = MacosKdumpActionResult
    status_class = MacosKdumpActionStatus
    feature_name = "kdump-management"

    # ------------------------------------------------------------------
    # Write verbs (most return FAILED on macOS)
    # ------------------------------------------------------------------

    def enable_kdump(self) -> MacosKdumpActionResult:
        """
            Not supported on macOS.  Returns ``FAILED`` with the
            ``UNSUPPORTED_MESSAGE`` advisory.

            :returns: The :class:`MacosKdumpActionResult`.
        """
        rtnval = self._unsupported_result(action="enable")
        return rtnval

    def disable_kdump(self) -> MacosKdumpActionResult:
        """
            Not supported on macOS.  Returns ``FAILED`` with the
            ``UNSUPPORTED_MESSAGE`` advisory.

            :returns: The :class:`MacosKdumpActionResult`.
        """
        rtnval = self._unsupported_result(action="disable")
        return rtnval

    def configure_kdump_target(
        self,
        spec: MacosKdumpTargetSpec,
    ) -> MacosKdumpActionResult:
        """
            Not supported on macOS.  Returns ``FAILED`` with the
            ``UNSUPPORTED_MESSAGE`` advisory.

            :param spec: The :class:`MacosKdumpTargetSpec` (ignored).

            :returns: The :class:`MacosKdumpActionResult`.
        """
        # Defensive use of `spec` so callers see a target-aware result.
        if spec.path is not None:
            target_path: Optional[str] = spec.path
        else:
            target_path = None
        rtnval = self._unsupported_result(
            action="configure_target",
            target=target_path,
        )
        return rtnval

    def clear_old_dumps(
        self,
        age_days: int,
    ) -> MacosKdumpActionResult:
        """
            Remove panic / diagnostic-report files older than
            ``age_days``.

            Runs ``find <report_dir> -maxdepth 1 -type f
            \\( -name '*.panic' -o -name '*.ips' \\) -mtime +<age>
            -exec rm -f {} \\;``.

            :param age_days: Minimum age in days.  Must be ``>= 0``.

            :returns: The :class:`MacosKdumpActionResult`.
        """
        action = "clear_old_dumps"
        if age_days < 0:
            failed = self._build_result(
                status=MacosKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=self._config.report_dir,
                message="age_days must be >= 0",
                stderr="invalid_age_days",
            )
            return failed

        command = [
            _FIND,
            self._config.report_dir,
            "-maxdepth", "1",
            "-type", "f",
            "(",
            "-name", "*{}".format(_PANIC_EXT),
            "-o",
            "-name", "*{}".format(_IPS_EXT),
            ")",
            "-mtime", "+{}".format(age_days),
            "-exec", "rm", "-f", "{}", ";",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="clear_old_dumps",
            target=self._config.report_dir,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_kdump_status(self) -> MacosKdumpStatus:
        """
            Synthetic status for macOS.

            macOS panic logging is always-on; this method does not
            execute any command and returns a fixed
            :class:`MacosKdumpStatus` describing the native model.

            :returns: The :class:`MacosKdumpStatus`.
        """
        rtnval = MacosKdumpStatus(
            enabled=True,
            running=True,
            target_kind=MacosKdumpTargetKind.LOCAL_PATH,
            target_path=self._config.report_dir,
            kernel=None,
            reserved_memory_mb=None,
            message="macOS native kernel-panic logging",
        )
        return rtnval

    def list_recent_dumps(self) -> list[MacosKdumpInfo]:
        """
            Enumerate ``*.panic`` / ``*.ips`` files under the configured
            report directory.

            Runs ``ls -1t <report_dir>`` once, then filters in pure
            Python by extension.  Reads never elevate.

            :returns: A list of :class:`MacosKdumpInfo`, freshest
                      first.
            :raises MacosKdumpQueryError: When ``ls`` fails for a
                                          reason other than a missing
                                          directory.
        """
        command = [_LS, "-1t", self._config.report_dir]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_lower = (run_result.stderr or "").lower()
            if "no such file" in stderr_lower:
                rtnval: list[MacosKdumpInfo] = []
                return rtnval
            raise MacosKdumpQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = []
        for raw_line in run_result.stdout.splitlines():
            entry = raw_line.strip()
            if len(entry) == 0:
                continue
            if entry.startswith("."):
                continue
            if not (entry.endswith(_PANIC_EXT) or entry.endswith(_IPS_EXT)):
                continue

            full_path = "{}/{}".format(self._config.report_dir, entry)
            info = MacosKdumpInfo(
                id=entry,
                path=full_path,
                created="",
                size_bytes=None,
                kernel=None,
                panic_message=None,
            )
            rtnval.append(info)

        return rtnval

    def get_dump_info(self, dump_id: str) -> MacosKdumpInfo:
        """
            Return parsed metadata for a single panic / diagnostic-
            report file.

            ``cat``s the first ~30 lines of
            ``<report_dir>/<dump_id>`` and surfaces the
            ``Darwin Kernel Version`` and ``panic(`` lines, plus the
            mtime / size from ``stat``.

            :param dump_id: The report filename.

            :returns: The :class:`MacosKdumpInfo`.
            :raises MacosKdumpQueryError: When the file is not
                                          readable.
        """
        full_path = "{}/{}".format(self._config.report_dir, dump_id)
        cat_command = [_CAT, full_path]
        run_result = self._runner.run(list(cat_command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosKdumpQueryError(
                command=cat_command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        parsed = _parse_panic_metadata(text=run_result.stdout)

        stat_command = [_STAT, "-f", "%Sm;%z", full_path]
        stat_run = self._runner.run(list(stat_command), elevated=False)
        if stat_run.exit_code == 0:
            raw = stat_run.stdout.strip()
            created_part, _, size_part = raw.partition(";")
            created = created_part.strip()
            size_text = size_part.strip()
            if len(size_text) > 0 and size_text.isdigit():
                size_bytes: Optional[int] = int(size_text)
            else:
                size_bytes = None
        else:
            created = ""
            size_bytes = None

        rtnval = MacosKdumpInfo(
            id=dump_id,
            path=full_path,
            created=created,
            size_bytes=size_bytes,
            kernel=parsed.get("kernel"),
            panic_message=parsed.get("panic_message"),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _unsupported_result(
        self,
        *,
        action: str,
        target: Optional[str] = None,
    ) -> MacosKdumpActionResult:
        """
            Build a ``FAILED`` result carrying the macOS-unsupported
            advisory.

            :param action: The verb identifier ("enable" / "disable" /
                           "configure_target").
            :param target: Optional target string echoed onto the
                           result.

            :returns: The :class:`MacosKdumpActionResult`.
        """
        rtnval = MacosKdumpActionResult(
            status=MacosKdumpActionStatus.FAILED,
            detail=action,
            command=None,
            target=target,
            requires_reboot=False,
            message=_UNSUPPORTED_MESSAGE,
            stderr="not_supported_on_macos",
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosKdumpActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        target: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> MacosKdumpActionResult:
        """
            Construct a :class:`MacosKdumpActionResult` without
            dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param target: The dump target affected.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`MacosKdumpActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = MacosKdumpActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            target=target,
            requires_reboot=False,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval
