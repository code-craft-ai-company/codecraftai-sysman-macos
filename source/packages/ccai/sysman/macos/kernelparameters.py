"""
    macOS kernel-parameters feature.

    Implements the cross-OS kernel-parameters contract for macOS using
    ``sysctl(8)`` for runtime reads and writes plus the single flat
    ``/etc/sysctl.conf`` file for persistence.  Every verb is dispatched
    through an :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    Persistence shape:
        macOS does NOT have an ``/etc/sysctl.d/`` drop-in directory.  The
        only persistent surface is the single flat ``/etc/sysctl.conf``
        file, which launchd's ``com.apple.sysctl`` job reads at boot.
        Any persistent change is therefore performed as a
        read-modify-write of that one file.

    File I/O policy:
        ``/etc/sysctl.conf`` is read with ``cat`` and written with
        ``tee`` through the runner.  This module does NOT consume an
        :class:`IFileOperator`; the established macOS pattern routes
        file I/O through the runner so dry-run, elevation, and aspect
        composition compose uniformly.

    Reload semantics:
        ``sysctl -f /etc/sysctl.conf`` is the macOS equivalent of
        Linux's ``sysctl --system`` and is what the manager dispatches
        for :meth:`reload_parameters`.

    Reboot caveat:
        Some macOS sysctl keys (notably parts of the ``kern.``
        namespace under SIP) are read-only at runtime and are honoured
        only at boot.  The current pilot does not maintain an explicit
        SIP read-only set; the persistent write still rewrites
        ``/etc/sysctl.conf`` so the value applies after the next reboot
        and surfaces ``requires_reboot=False`` for cross-OS contract
        conformance.  See ``specs/3-kernel-parameters/tasks.md`` for
        the SIP-set follow-up.

    Elevation policy:
        ``set_parameter``, ``reload_parameters``, and
        ``remove_persisted_parameter`` honour ``config.elevated``.
        Every read (``get_parameter``, ``list_parameters``, idempotency
        probes) never elevates.
"""

import os

from dataclasses import dataclass
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_SYSCTL = "sysctl"

_HEADER_COMMENT = "# managed by codecraftai-sysman-macos"


MacosKernelParameterActionStatus = ActionStatus


@dataclass
class MacosKernelParameterManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosKernelParameterManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param sysctl_conf_path: Path to the flat persistence file.  The
                                 default ``/etc/sysctl.conf`` matches
                                 the upstream sysctl-discovered path on
                                 macOS.
    """
    sysctl_conf_path: str = "/etc/sysctl.conf"


@dataclass
class MacosKernelParameterActionResult(FeatureActionResult):
    """
        Result of a kernel-parameter management write operation.

        :param name: The parameter name targeted, or ``None`` for
                     :meth:`MacosKernelParameterManager.reload_parameters`.
        :param value: The value written (or would-be in ``DRY_RUN``);
                      ``None`` for ``reload_parameters`` and
                      ``remove_persisted_parameter``.
        :param persistent: Whether the write targeted persistent
                           storage in addition to the runtime surface.
                           ``None`` for verbs that do not split runtime
                           vs persistent.
        :param current_value: Pre-action runtime value, when known.
        :param applied_value: Post-action runtime value (or would-be on
                              ``DRY_RUN`` / ``SKIPPED``).
        :param requires_reboot: ``False`` on macOS sysctl writes by
                                default; kept on the result for cross-
                                OS contract conformance.
        :param source: The persistence file path that was (or would
                       have been) modified.  ``None`` for runtime-only
                       writes.
        :param diff: Reserved for a future dry-run unified-diff field;
                     left ``None`` in this implementation.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    persistent: Optional[bool] = None
    current_value: Optional[str] = None
    applied_value: Optional[str] = None
    requires_reboot: bool = False
    source: Optional[str] = None
    diff: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosParameterValue:
    """
        A single sysctl parameter and its current value.

        :param name: The dotted parameter name.
        :param value: The current string value (whitespace-stripped).
        :param is_default: Reserved tri-state describing whether the
                           value equals the kernel default.  ``None``
                           when not computed (the default for this
                           implementation).
    """
    name: str
    value: str
    is_default: Optional[bool] = None


class MacosKernelParameterQueryError(Exception):
    """
        Raised when a parameter read fails for a reason other than the
        parameter simply being unknown.
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
        self.command: list[str] = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Kernel parameter query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(
            " ".join(self.command),
        ))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosKernelParameterManager(BaseFeatureManager):
    """
        macOS sysctl kernel-parameter manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; persistence file
        I/O is performed via ``cat`` / ``tee`` rather than a separate
        file operator.
    """

    result_class = MacosKernelParameterActionResult
    status_class = MacosKernelParameterActionStatus
    feature_name = "kernel-parameters"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_parameter(self, name: str) -> Optional[MacosParameterValue]:
        """
            Read the current runtime value of a sysctl parameter.

            Runs ``sysctl -n <name>`` without elevation.  Returns
            ``None`` when the parameter is not present; raises
            :class:`MacosKernelParameterQueryError` for unexpected
            failures.

            :param name: The dotted parameter name.

            :returns: The :class:`MacosParameterValue`, or ``None`` when
                      the parameter does not exist.
            :raises MacosKernelParameterQueryError: When the runner
                                                    reports an
                                                    unexpected failure.
        """
        command = [_SYSCTL, "-n", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            value = run_result.stdout.strip()
            rtnval: Optional[MacosParameterValue] = MacosParameterValue(
                name=name,
                value=value,
                is_default=None,
            )
            return rtnval

        stderr = run_result.stderr or ""
        if _is_unknown_oid(stderr=stderr):
            rtnval = None
            return rtnval

        raise MacosKernelParameterQueryError(
            command=command,
            exit_code=run_result.exit_code,
            stderr=stderr,
        )

    def list_parameters(self) -> dict[str, str]:
        """
            Enumerate every sysctl parameter and its current value.

            Runs ``sysctl -a --ignore`` without elevation and parses
            both the ``name = value`` and ``name: value`` separator
            styles emitted on macOS.

            :returns: A ``dict[str, str]`` of every parsed parameter.
            :raises MacosKernelParameterQueryError: When the runner
                                                    reports a non-zero
                                                    exit code.
        """
        command = [_SYSCTL, "-a", "--ignore"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosKernelParameterQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = _parse_sysctl_a(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_parameter(
        self,
        name: str,
        value: str,
        persistent: bool = True,
    ) -> MacosKernelParameterActionResult:
        """
            Set a sysctl parameter at runtime and (optionally) persist
            it.

            When ``persistent=True``, the manager rewrites
            ``/etc/sysctl.conf`` to add or update the ``name=value``
            line AND applies the value at runtime via ``sysctl -w``.
            When ``persistent=False``, only the runtime apply is
            performed.

            Idempotency:
                ``persistent=False`` skips when the runtime value
                already matches.  ``persistent=True`` skips only when
                BOTH the runtime value matches AND the persisted line
                already matches.

            macOS reboot caveat:
                Some keys (notably parts of ``kern.`` under SIP) are
                read-only at runtime; their value takes effect only at
                boot.  In this pilot we always attempt the runtime
                ``sysctl -w`` and surface ``requires_reboot=False``;
                a future SIP-aware code path is tracked in
                ``specs/3-kernel-parameters/tasks.md``.

            :param name: The dotted parameter name.
            :param value: The desired string value.
            :param persistent: When True (default), update
                               ``/etc/sysctl.conf`` in addition to the
                               runtime surface.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        runtime_command = [_SYSCTL, "-w", "{}={}".format(name, value)]

        if persistent:
            rtnval = self._set_parameter_persistent(
                name=name,
                value=value,
                runtime_command=runtime_command,
            )
            return rtnval

        rtnval = self._set_parameter_runtime_only(
            name=name,
            value=value,
            runtime_command=runtime_command,
        )
        return rtnval

    def reload_parameters(self) -> MacosKernelParameterActionResult:
        """
            Reload the persistent ``/etc/sysctl.conf`` file via
            ``sysctl -f /etc/sysctl.conf``.

            Never skips.  Honours dry-run.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        path = self._typed_config().sysctl_conf_path
        command = [_SYSCTL, "-f", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosKernelParameterActionStatus.DRY_RUN,
                name=None,
                value=None,
                persistent=None,
                command=command,
                source=path,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = MacosKernelParameterActionStatus.FAILED

        rtnval = MacosKernelParameterActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=None,
            value=None,
            persistent=None,
            source=path,
        )
        return rtnval

    def remove_persisted_parameter(
        self,
        name: str,
    ) -> MacosKernelParameterActionResult:
        """
            Remove a parameter from ``/etc/sysctl.conf``.

            Reads the current contents of the file, drops the matching
            line, and writes the new content via ``tee``.  Skips when
            the file does not exist or the parameter is not present.

            :param name: The dotted parameter name.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        path = self._typed_config().sysctl_conf_path
        write_command = ["tee", path]

        existing = self._read_file(path=path)
        if existing is None or len(existing) == 0:
            skipped = self._build_result(
                status=MacosKernelParameterActionStatus.SKIPPED,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
                message="sysctl.conf is absent or empty",
            )
            return skipped

        new_content = _remove_sysctl_line(content=existing, name=name)
        if new_content == existing:
            skipped = self._build_result(
                status=MacosKernelParameterActionStatus.SKIPPED,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
                message="parameter not present in sysctl.conf",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = MacosKernelParameterActionStatus.FAILED

        rtnval = MacosKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=name,
            value=None,
            persistent=True,
            source=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — set_parameter
    # ------------------------------------------------------------------

    def _set_parameter_runtime_only(
        self,
        *,
        name: str,
        value: str,
        runtime_command: list[str],
    ) -> MacosKernelParameterActionResult:
        """
            Apply ``sysctl -w`` without touching any persistent file.

            :param name: The parameter name.
            :param value: The desired value.
            :param runtime_command: Pre-built ``["sysctl", "-w", ...]``
                                    argv.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=MacosKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=value,
                persistent=False,
                command=runtime_command,
                applied_value=value,
            )
            return dry

        current_info = self.get_parameter(name=name)
        if current_info is not None:
            current_value: Optional[str] = current_info.value
        else:
            current_value = None

        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=MacosKernelParameterActionStatus.SKIPPED,
                name=name,
                value=value,
                persistent=False,
                command=runtime_command,
                current_value=current_value,
                applied_value=value,
                message="runtime value already matches",
            )
            return skipped

        run_result = self._runner.run(
            list(runtime_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = MacosKernelParameterActionStatus.FAILED

        rtnval = MacosKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(runtime_command),
            name=name,
            value=value,
            persistent=False,
            current_value=current_value,
            applied_value=value,
        )
        return rtnval

    def _set_parameter_persistent(
        self,
        *,
        name: str,
        value: str,
        runtime_command: list[str],
    ) -> MacosKernelParameterActionResult:
        """
            Persist a parameter into ``/etc/sysctl.conf`` and apply at
            runtime.

            :param name: The parameter name.
            :param value: The desired value.
            :param runtime_command: Pre-built ``["sysctl", "-w", ...]``
                                    argv.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        path = self._typed_config().sysctl_conf_path
        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=value,
                persistent=True,
                command=write_command,
                source=path,
                applied_value=value,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is None:
            existing_content = ""
        else:
            existing_content = existing
        desired_content = _set_sysctl_line(
            content=existing_content,
            name=name,
            value=value,
        )

        current_info = self.get_parameter(name=name)
        if current_info is not None:
            current_value: Optional[str] = current_info.value
        else:
            current_value = None

        file_matches = (existing_content == desired_content)
        runtime_matches = (
            current_value is not None and current_value == value
        )

        if file_matches and runtime_matches:
            skipped = self._build_result(
                status=MacosKernelParameterActionStatus.SKIPPED,
                name=name,
                value=value,
                persistent=True,
                command=write_command,
                source=path,
                current_value=current_value,
                applied_value=value,
                message="runtime and sysctl.conf already match",
            )
            return skipped

        if not file_matches:
            file_run = self._runner.run(
                list(write_command),
                elevated=self._config.elevated,
                stdin=desired_content,
            )
            if file_run.exit_code != 0:
                failed = MacosKernelParameterActionResult(
                    status=MacosKernelParameterActionStatus.FAILED,
                    detail=name,
                    exit_code=file_run.exit_code,
                    stdout=file_run.stdout,
                    stderr=file_run.stderr,
                    command=list(write_command),
                    name=name,
                    value=value,
                    persistent=True,
                    source=path,
                    current_value=current_value,
                    applied_value=None,
                    message="persist_failed",
                )
                return failed

        runtime_run = self._runner.run(
            list(runtime_command),
            elevated=self._config.elevated,
        )
        if runtime_run.exit_code != 0:
            failed = MacosKernelParameterActionResult(
                status=MacosKernelParameterActionStatus.FAILED,
                detail=name,
                exit_code=runtime_run.exit_code,
                stdout=runtime_run.stdout,
                stderr=runtime_run.stderr,
                command=list(runtime_command),
                name=name,
                value=value,
                persistent=True,
                source=path,
                current_value=current_value,
                applied_value=None,
                message="runtime_apply_failed",
            )
            return failed

        rtnval = MacosKernelParameterActionResult(
            status=MacosKernelParameterActionStatus.SUCCESS,
            detail=name,
            exit_code=runtime_run.exit_code,
            stdout=runtime_run.stdout,
            stderr=runtime_run.stderr,
            command=list(runtime_command),
            name=name,
            value=value,
            persistent=True,
            source=path,
            current_value=current_value,
            applied_value=value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — config / file I/O / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosKernelParameterManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosKernelParameterManagerConfig`.

            The base class stores the config as
            :class:`FeatureConfig`; this helper centralises the
            ``isinstance`` cast so callers can read ``sysctl_conf_path``
            without losing static-type clarity.

            :returns: The active config typed as
                      :class:`MacosKernelParameterManagerConfig`.
        """
        if isinstance(self._config, MacosKernelParameterManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosKernelParameterManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code (i.e. the
                      path is missing).
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosKernelParameterActionStatus,
        name: Optional[str],
        value: Optional[str],
        persistent: Optional[bool],
        command: list[str],
        current_value: Optional[str] = None,
        applied_value: Optional[str] = None,
        source: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosKernelParameterActionResult:
        """
            Construct a :class:`MacosKernelParameterActionResult`
            without invoking the runner.

            :param status: The outcome status.
            :param name: The parameter name to echo on the result.
            :param value: The would-be applied value.
            :param persistent: Whether the action targeted persistence.
            :param command: The would-have-been argv, copied for
                            capture.
            :param current_value: Pre-action runtime value, when known.
            :param applied_value: Post-action runtime value.
            :param source: The persistence file path, when applicable.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`MacosKernelParameterActionResult`.
        """
        rtnval = MacosKernelParameterActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            persistent=persistent,
            current_value=current_value,
            applied_value=applied_value,
            source=source,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _is_unknown_oid(*, stderr: str) -> bool:
    """
        Detect the "unknown oid" / "unknown key" stderr emitted by
        ``sysctl -n`` for an unknown parameter on macOS.

        :param stderr: Captured stderr text.

        :returns: True when the stderr indicates an unknown parameter.
    """
    lower = stderr.lower()
    if "unknown oid" in lower:
        return True
    if "unknown key" in lower:
        return True
    if "second level name" in lower:
        return True
    if "no such file or directory" in lower:
        return True
    return False


def _parse_sysctl_a(*, stdout: str) -> dict[str, str]:
    """
        Parse the output of ``sysctl -a`` into a name-to-value dict.

        macOS' ``sysctl -a`` emits both ``name: value`` and
        ``name = value`` separators depending on the OID.  Lines
        without a recognisable separator are silently skipped.

        :param stdout: Captured stdout text.

        :returns: A ``dict[str, str]`` of every parsed parameter.
    """
    rtnval: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if len(stripped) == 0:
            continue
        # Prefer ``=`` because some keys contain ``:`` in their value;
        # only fall back to ``:`` when no ``=`` is present.
        if "=" in stripped:
            key_part, _, value_part = stripped.partition("=")
        elif ":" in stripped:
            key_part, _, value_part = stripped.partition(":")
        else:
            continue
        key = key_part.strip()
        value = value_part.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _set_sysctl_line(*, content: str, name: str, value: str) -> str:
    """
        Return ``/etc/sysctl.conf`` content updated to contain
        ``<name>=<value>``.

        Existing lines for the same parameter are replaced in place.
        Comment and blank lines are preserved verbatim.  An empty
        starting ``content`` yields a header-prefixed file.  The
        rendered form uses no whitespace around ``=`` per macOS
        ``sysctl.conf(5)``.

        :param content: Current file content.
        :param name: The parameter name.
        :param value: The desired value.

        :returns: The updated file content terminated with a newline.
    """
    new_line = "{}={}".format(name, value)

    if len(content) == 0:
        body = "{}\n{}\n".format(_HEADER_COMMENT, new_line)
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or len(stripped) == 0:
            updated.append(line)
            continue
        if "=" not in stripped:
            updated.append(line)
            continue
        key_part, _, _ = stripped.partition("=")
        existing_name = key_part.strip()
        if existing_name == name:
            updated.append(new_line)
            replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_sysctl_line(*, content: str, name: str) -> str:
    """
        Return ``/etc/sysctl.conf`` content with any line matching
        ``name`` removed.

        Comment and blank lines are preserved.

        :param content: Current file content.
        :param name: The parameter name to remove.

        :returns: The updated file content.
    """
    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or len(stripped) == 0:
            kept.append(line)
            continue
        if "=" not in stripped:
            kept.append(line)
            continue
        key_part, _, _ = stripped.partition("=")
        existing_name = key_part.strip()
        if existing_name == name:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body
