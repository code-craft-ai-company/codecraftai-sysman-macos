"""
    macOS authentication-management feature.

    Implements the cross-OS authentication-management contract for
    macOS using direct edits to the per-service PAM files under
    ``/etc/pam.d/``.  Every command is dispatched through the
    injected :class:`ICommandRunner`.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``get_auth_config``         | ``cat /etc/pam.d/<svc>`` + parse  |
    +-----------------------------+-----------------------------------+
    | ``set_auth_config``         | ``cat`` + parse + render +        |
    |                             | ``tee /etc/pam.d/<svc>``          |
    +-----------------------------+-----------------------------------+
    | ``list_auth_methods``       | ``ls -1 /etc/pam.d`` + ``cat`` of |
    |                             | each canonical subsystem          |
    +-----------------------------+-----------------------------------+
    | ``enable_method``           | ``cat`` + un-comment +            |
    |                             | ``tee /etc/pam.d/<svc>``          |
    +-----------------------------+-----------------------------------+
    | ``disable_method``          | ``cat`` + comment-prefix +        |
    |                             | ``tee /etc/pam.d/<svc>``          |
    +-----------------------------+-----------------------------------+
    | ``validate_auth_config``    | ``cat`` + internal parser         |
    +-----------------------------+-----------------------------------+

    macOS authentication has two surfaces — the PAM stack under
    ``/etc/pam.d/`` (managed here) and the authorization database
    at ``/etc/authorization`` (a binary plist consumed by
    Authorization Services for GUI privilege requests).  The
    pilot focuses on PAM-stack management.  The authorization
    database surface is exposed as a forward-compat config field
    only — :class:`MacosAuthenticationManagerConfig` carries
    ``authz_path`` but no verb in this module reads from or writes
    to it.

    File I/O policy:
        Reads use ``cat`` with ``elevated=False``; writes use
        ``tee`` with ``elevated=True`` and the new content piped on
        ``stdin``.  This module does NOT consume an
        :class:`IFileOperator`.

    Idempotency model:
        Every write reads the current file, renders the desired
        content, and compares verbatim.  When equal the verb returns
        ``SKIPPED`` without invoking the runner for the write.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads
        (``get_auth_config``, ``list_auth_methods``,
        ``validate_auth_config``) never elevate.
"""

import os

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_CAT = "cat"
_TEE = "tee"
_LS = "ls"

_DEFAULT_PAM_DIR = "/etc/pam.d"
_DEFAULT_AUTHZ_PATH = "/etc/authorization"

_AUTH_KIND_LOGIN = "LOGIN"
_AUTH_KIND_SUDO = "SUDO"
_AUTH_KIND_SU = "SU"
_AUTH_KIND_SSH = "SSH"
_AUTH_KIND_CRON = "CRON"
_AUTH_KIND_OTHER = "OTHER"

_VALID_MODULE_TYPES = (
    "auth",
    "account",
    "password",
    "session",
    "@include",
    "-auth",
    "-account",
    "-password",
    "-session",
)

_DEFAULT_CANONICAL_SUBSYSTEMS = (
    "login",
    "sudo",
    "su",
    "sshd",
    "cron",
)


MacosAuthActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Subsystem typing
# ----------------------------------------------------------------------


@dataclass
class MacosAuthSubsystem:
    """
        Discriminated wrapper around a PAM service-file basename.

        :param kind: One of ``LOGIN`` / ``SUDO`` / ``SU`` / ``SSH`` /
                     ``CRON`` / ``OTHER``.
        :param name: The service-file basename.
    """
    kind: str
    name: str


MACOS_AUTH_LOGIN = MacosAuthSubsystem(kind=_AUTH_KIND_LOGIN, name="login")
MACOS_AUTH_SUDO = MacosAuthSubsystem(kind=_AUTH_KIND_SUDO, name="sudo")
MACOS_AUTH_SU = MacosAuthSubsystem(kind=_AUTH_KIND_SU, name="su")
MACOS_AUTH_SSH = MacosAuthSubsystem(kind=_AUTH_KIND_SSH, name="sshd")
MACOS_AUTH_CRON = MacosAuthSubsystem(kind=_AUTH_KIND_CRON, name="cron")


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _default_canonical_subsystems() -> list:
    """
        Build the default canonical-subsystem list.  Returns a fresh
        list each call so the dataclass default cannot be mutated
        across instances.

        :returns: A list of basename strings.
    """
    rtnval = list(_DEFAULT_CANONICAL_SUBSYSTEMS)
    return rtnval


@dataclass
class MacosAuthenticationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosAuthenticationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param pam_dir: Absolute path to the PAM service-file
                        directory.  Default ``/etc/pam.d``.
        :param authz_path: Absolute path to the authorization
                           database plist.  Forward-compat only —
                           no verb reads from or writes to this
                           path in the pilot.  Default
                           ``/etc/authorization``.
        :param canonical_subsystems: List of PAM service-file
                                     basenames considered when
                                     ``list_auth_methods``,
                                     ``enable_method``, and
                                     ``disable_method`` walk the
                                     PAM directory.  Default is
                                     ``["login", "sudo", "su",
                                     "sshd", "cron"]``.
    """
    pam_dir: str = _DEFAULT_PAM_DIR
    authz_path: str = _DEFAULT_AUTHZ_PATH
    canonical_subsystems: list = field(
        default_factory=_default_canonical_subsystems,
    )


@dataclass
class MacosAuthEntry:
    """
        A single PAM stack entry.

        :param module_type: ``auth``, ``account``, ``password``,
                            ``session``, or ``@include``.
        :param control: PAM control text.
        :param module_path: The PAM module reference.
        :param args: Remaining tokens.
    """
    module_type: str
    control: str
    module_path: str
    args: list = field(default_factory=list)


@dataclass
class MacosAuthConfig:
    """
        A PAM stack config for one subsystem.

        :param subsystem: The PAM service-file basename.
        :param entries: PAM stack entries in declaration order.
    """
    subsystem: str
    entries: list = field(default_factory=list)


@dataclass
class MacosAuthMethodInfo:
    """
        A PAM module observed across the canonical subsystems.

        :param name: The module name (e.g. ``pam_unix.so``).
        :param enabled: True when the module appears un-commented
                        in at least one canonical subsystem.
        :param description: Comma-joined list of subsystems that
                            reference the module.
        :param module_path: Mirrors ``name`` for contract
                            conformance.
    """
    name: str
    enabled: bool
    description: Optional[str] = None
    module_path: Optional[str] = None


@dataclass
class MacosAuthValidationResult:
    """
        Outcome of
        :meth:`MacosAuthenticationManager.validate_auth_config`.

        :param valid: True when no structural errors were detected.
        :param errors: List of error strings.
        :param warnings: List of warning strings.
    """
    valid: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class MacosAuthActionResult(FeatureActionResult):
    """
        Result of an authentication-management write operation.

        :param subsystem: The subsystem targeted, when applicable.
        :param method: The method targeted, when applicable.
        :param message: Optional human-readable detail.
    """
    subsystem: Optional[str] = None
    method: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosAuthenticationQueryError(Exception):
    """
        Raised when an authentication-management read fails.
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

        err_msg_lines = ["Authentication query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosAuthenticationConfigError(Exception):
    """
        Raised when an :class:`MacosAuthConfig` is malformed for
        the targeted subsystem.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem the config targets.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication config rejected."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosAuthenticationSubsystemNotFoundError(Exception):
    """
        Raised when an unknown PAM subsystem (or a method not
        present in any canonical subsystem) is referenced.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem or method name.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication subsystem not found."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosAuthenticationManager(BaseFeatureManager):
    """
        macOS authentication-management manager.  Reads, writes,
        and validates per-service PAM stacks under ``/etc/pam.d/``
        through the injected :class:`ICommandRunner`.
    """

    result_class = MacosAuthActionResult
    status_class = MacosAuthActionStatus
    feature_name = "authentication-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_auth_config(self, subsystem) -> MacosAuthConfig:
        """
            Read a PAM subsystem's stack.

            :param subsystem: A :class:`MacosAuthSubsystem`,
                              one of the ``MACOS_AUTH_*`` module
                              constants, or a raw string with the
                              service-file basename.

            :returns: The :class:`MacosAuthConfig`.
            :raises MacosAuthenticationSubsystemNotFoundError:
                When the service file does not exist.
            :raises MacosAuthenticationQueryError:
                When the read fails for any other reason.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        command = [_CAT, path]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            stderr_lower = stderr_text.lower()
            if "no such file" in stderr_lower or "not found" in stderr_lower:
                raise MacosAuthenticationSubsystemNotFoundError(
                    subsystem=basename,
                    detail="PAM service file does not exist: {}".format(path),
                )
            raise MacosAuthenticationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        entries = _parse_pam_stack(content=run_result.stdout)
        rtnval = MacosAuthConfig(
            subsystem=basename,
            entries=entries,
        )
        return rtnval

    def list_auth_methods(self) -> list:
        """
            Walk the canonical PAM subsystems and aggregate every
            referenced module into a list of
            :class:`MacosAuthMethodInfo`.

            :returns: A ``list[MacosAuthMethodInfo]``.
        """
        cfg = self._typed_config()
        subsystems = list(cfg.canonical_subsystems)

        # name -> {"enabled": bool, "subsystems": list[str]}
        aggregate: dict = {}

        for basename in subsystems:
            path = self._pam_path(basename=basename)
            content = self._read_file(path=path)
            if content is None:
                continue

            for raw_line in content.splitlines():
                stripped = raw_line.strip()
                if len(stripped) == 0:
                    continue

                is_comment = stripped.startswith("#")
                if is_comment:
                    body = stripped.lstrip("#").strip()
                    if len(body) == 0:
                        continue
                else:
                    body = stripped

                tokens = _tokenize_pam_line(line=body)
                module_name = _module_name_from_tokens(tokens=tokens)
                if module_name is None:
                    continue

                if module_name not in aggregate:
                    aggregate[module_name] = {
                        "enabled": False, "subsystems": [],
                    }
                if not is_comment:
                    aggregate[module_name]["enabled"] = True
                if basename not in aggregate[module_name]["subsystems"]:
                    aggregate[module_name]["subsystems"].append(basename)

        rtnval: list = []
        for module_name in sorted(aggregate.keys()):
            entry = aggregate[module_name]
            description_text = ", ".join(entry["subsystems"])
            info = MacosAuthMethodInfo(
                name=module_name,
                enabled=entry["enabled"],
                description=description_text,
                module_path=module_name,
            )
            rtnval.append(info)
        return rtnval

    def validate_auth_config(self, subsystem) -> MacosAuthValidationResult:
        """
            Validate a PAM subsystem's stack syntactically.

            :param subsystem: As for :meth:`get_auth_config`.

            :returns: The :class:`MacosAuthValidationResult`.
            :raises MacosAuthenticationSubsystemNotFoundError:
                When the service file does not exist.
            :raises MacosAuthenticationQueryError:
                When the read fails for any other reason.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        command = [_CAT, path]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            stderr_lower = stderr_text.lower()
            if "no such file" in stderr_lower or "not found" in stderr_lower:
                raise MacosAuthenticationSubsystemNotFoundError(
                    subsystem=basename,
                    detail="PAM service file does not exist: {}".format(path),
                )
            raise MacosAuthenticationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        errors: list = []
        warnings: list = []
        entries = _parse_pam_stack(content=run_result.stdout)
        non_blank_input_lines = 0
        for raw_line in run_result.stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            if stripped.startswith("#"):
                continue
            non_blank_input_lines += 1

        if non_blank_input_lines == 0:
            warnings.append("PAM stack has no executable lines")
            rtnval = MacosAuthValidationResult(
                valid=True, errors=errors, warnings=warnings,
            )
            return rtnval

        for index, entry in enumerate(entries):
            line_num = index + 1
            if entry.module_type not in _VALID_MODULE_TYPES:
                errors.append(
                    "line {}: unknown module_type '{}'".format(
                        line_num, entry.module_type,
                    ),
                )
            if entry.module_type != "@include" and len(entry.control) == 0:
                errors.append(
                    "line {}: missing control token".format(line_num),
                )
            if len(entry.module_path) == 0:
                errors.append(
                    "line {}: missing module_path".format(line_num),
                )

        is_valid = len(errors) == 0
        rtnval = MacosAuthValidationResult(
            valid=is_valid, errors=errors, warnings=warnings,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_auth_config(
        self,
        subsystem,
        config: MacosAuthConfig,
    ) -> MacosAuthActionResult:
        """
            Replace a PAM subsystem's stack with the supplied
            config.

            Idempotent: returns ``SKIPPED`` when the rendered
            desired content equals the current file content.
            Honours dry-run.

            :param subsystem: As for :meth:`get_auth_config`.
            :param config: The desired :class:`MacosAuthConfig`.

            :returns: The :class:`MacosAuthActionResult`.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        rendered_content = _render_pam_stack(entries=config.entries)

        if existing_content == rendered_content:
            skipped = self._build_result(
                status=MacosAuthActionStatus.SKIPPED,
                subsystem=basename,
                method=None,
                command=write_command,
                message="PAM stack already at desired content",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuthActionStatus.DRY_RUN,
                subsystem=basename,
                method=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            subsystem=basename,
            method=None,
        )
        return rtnval

    def enable_method(self, name: str) -> MacosAuthActionResult:
        """
            Enable a PAM module across the canonical subsystems by
            stripping the leading ``#`` from any commented
            references to it.

            Idempotent: returns ``SKIPPED`` when the module is
            already enabled.  Raises
            :class:`MacosAuthenticationSubsystemNotFoundError`
            when the module is not present in any canonical
            subsystem.  Honours dry-run.

            :param name: The PAM module name (e.g.
                         ``pam_smartcard.so``).

            :returns: The :class:`MacosAuthActionResult`.
            :raises MacosAuthenticationSubsystemNotFoundError:
                When ``name`` is not present anywhere.
        """
        rtnval = self._toggle_method_state(name=name, enable=True)
        return rtnval

    def disable_method(self, name: str) -> MacosAuthActionResult:
        """
            Disable a PAM module across the canonical subsystems by
            prepending a ``#`` to any active references to it.

            Idempotent: returns ``SKIPPED`` when the module is
            already disabled or absent.  Honours dry-run.

            :param name: The PAM module name.

            :returns: The :class:`MacosAuthActionResult`.
        """
        rtnval = self._toggle_method_state(name=name, enable=False)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _toggle_method_state(
        self,
        *,
        name: str,
        enable: bool,
    ) -> MacosAuthActionResult:
        """
            Shared core of :meth:`enable_method` and
            :meth:`disable_method`.

            :param name: The PAM module name.
            :param enable: True for enable; False for disable.

            :returns: The :class:`MacosAuthActionResult`.
        """
        cfg = self._typed_config()
        subsystems = list(cfg.canonical_subsystems)

        if enable:
            label = "enable"
        else:
            label = "disable"

        # Track which files would be modified.
        modifications: list = []   # list of (path, new_content)
        seen_anywhere = False

        for basename in subsystems:
            path = self._pam_path(basename=basename)
            content = self._read_file(path=path)
            if content is None:
                continue

            new_content, found_in_file = _toggle_module_state_in_content(
                content=content,
                module_name=name,
                enable=enable,
            )

            if found_in_file:
                seen_anywhere = True

            if new_content != content:
                modifications.append((path, new_content))

        synthetic_command = ["pam-edit", "--{}".format(label), name]

        if enable and not seen_anywhere:
            raise MacosAuthenticationSubsystemNotFoundError(
                subsystem=name,
                detail=(
                    "module not present in any canonical "
                    "subsystem; cannot enable"
                ),
            )

        if len(modifications) == 0:
            if enable:
                msg_text = "method already enabled"
            else:
                msg_text = "method already disabled or absent"
            skipped = self._build_result(
                status=MacosAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=synthetic_command,
                message=msg_text,
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosAuthActionStatus.DRY_RUN,
                subsystem=None,
                method=name,
                command=synthetic_command,
            )
            return dry

        last_run_result = None
        last_command = None
        for path, new_content in modifications:
            write_command = [_TEE, path]
            last_command = write_command
            run_result = self._runner.run(
                list(write_command),
                elevated=self._config.elevated,
                stdin=new_content,
            )
            last_run_result = run_result
            if run_result.exit_code != 0:
                rtnval = self._wrap_runner_result(
                    run_result=run_result,
                    command=write_command,
                    subsystem=None,
                    method=name,
                )
                return rtnval

        if last_run_result is None or last_command is None:
            # Defensive: the modifications list was non-empty above.
            failed = self._build_result(
                status=MacosAuthActionStatus.FAILED,
                subsystem=None,
                method=name,
                command=synthetic_command,
                message="no modifications dispatched",
            )
            return failed

        rtnval = self._wrap_runner_result(
            run_result=last_run_result,
            command=last_command,
            subsystem=None,
            method=name,
        )
        return rtnval

    def _typed_config(self) -> MacosAuthenticationManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`MacosAuthenticationManagerConfig`.
        """
        if isinstance(self._config, MacosAuthenticationManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosAuthenticationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_subsystem(self, *, subsystem) -> str:
        """
            Resolve a subsystem reference into a basename.

            :param subsystem: An :class:`MacosAuthSubsystem`, a
                              string, or one of the
                              ``MACOS_AUTH_*`` constants.

            :returns: The basename string.
            :raises MacosAuthenticationSubsystemNotFoundError:
                When ``subsystem`` is None or empty.
        """
        if subsystem is None:
            raise MacosAuthenticationSubsystemNotFoundError(
                subsystem="<None>",
                detail="subsystem cannot be None",
            )

        if isinstance(subsystem, MacosAuthSubsystem):
            basename = subsystem.name
        elif isinstance(subsystem, str):
            basename = subsystem
        else:
            raise MacosAuthenticationSubsystemNotFoundError(
                subsystem=str(subsystem),
                detail="subsystem must be MacosAuthSubsystem or str",
            )

        if len(basename) == 0:
            raise MacosAuthenticationSubsystemNotFoundError(
                subsystem="<empty>",
                detail="subsystem basename is empty",
            )

        rtnval = basename
        return rtnval

    def _pam_path(self, *, basename: str) -> str:
        """
            Compose the absolute PAM service-file path for a
            basename.

            :param basename: The PAM service-file basename.

            :returns: The absolute path.
        """
        pam_dir = self._typed_config().pam_dir
        rtnval = "{}/{}".format(pam_dir, basename)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosAuthActionStatus,
        subsystem: Optional[str],
        method: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> MacosAuthActionResult:
        """
            Build a result without invoking the runner.

            :param status: The outcome status.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.

            :returns: The :class:`MacosAuthActionResult`.
        """
        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = MacosAuthActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            subsystem=subsystem,
            method=method,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        subsystem: Optional[str],
        method: Optional[str],
    ) -> MacosAuthActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosAuthActionResult`.

            :param run_result: The runner's result.
            :param command: The argv that was dispatched.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.

            :returns: The :class:`MacosAuthActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosAuthActionStatus.SUCCESS
        else:
            mapped_status = MacosAuthActionStatus.FAILED

        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = MacosAuthActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            subsystem=subsystem,
            method=method,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PAM stack parser / renderer
# ----------------------------------------------------------------------


def _parse_pam_stack(*, content: str) -> list:
    """
        Parse PAM stack content into a list of
        :class:`MacosAuthEntry`.

        :param content: The raw file content.

        :returns: A ``list[MacosAuthEntry]``.
    """
    rtnval: list = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue

        tokens = _tokenize_pam_line(line=stripped)
        if len(tokens) == 0:
            continue

        first_token = tokens[0]
        if first_token == "@include":
            if len(tokens) < 2:
                continue
            entry = MacosAuthEntry(
                module_type="@include",
                control="",
                module_path=tokens[1],
                args=[],
            )
            rtnval.append(entry)
            continue

        if len(tokens) < 3:
            continue

        module_type = tokens[0]
        control = tokens[1]
        module_path = tokens[2]
        if len(tokens) > 3:
            args_list = list(tokens[3:])
        else:
            args_list = []

        entry = MacosAuthEntry(
            module_type=module_type,
            control=control,
            module_path=module_path,
            args=args_list,
        )
        rtnval.append(entry)
    return rtnval


def _tokenize_pam_line(*, line: str) -> list:
    """
        Split a PAM stack line into tokens, treating any ``[...]``
        substring as one token.

        :param line: The raw line text.

        :returns: A list of tokens.
    """
    rtnval: list = []
    current = []
    in_bracket = False
    for ch in line:
        if in_bracket:
            current.append(ch)
            if ch == "]":
                rtnval.append("".join(current))
                current = []
                in_bracket = False
            continue

        if ch == "[":
            if len(current) > 0:
                rtnval.append("".join(current))
                current = []
            current.append(ch)
            in_bracket = True
            continue

        if ch.isspace():
            if len(current) > 0:
                rtnval.append("".join(current))
                current = []
            continue

        current.append(ch)

    if len(current) > 0:
        rtnval.append("".join(current))
    return rtnval


def _render_pam_stack(*, entries: list) -> str:
    """
        Render a list of :class:`MacosAuthEntry` into PAM stack
        syntax.

        :param entries: The entries to render.

        :returns: The rendered file content (single trailing
                  newline).
    """
    lines: list = []
    for entry in entries:
        if entry.module_type == "@include":
            lines.append("@include {}".format(entry.module_path))
            continue

        parts = [entry.module_type, entry.control, entry.module_path]
        for arg in entry.args:
            parts.append(arg)
        rendered_line = " ".join(parts)
        lines.append(rendered_line)

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _module_name_from_tokens(*, tokens: list) -> Optional[str]:
    """
        Extract the PAM module name from a tokenised line.

        :param tokens: The tokens.

        :returns: The module name (third token in normal lines,
                  second in ``@include`` lines), or ``None`` when
                  the line does not look like a PAM stack entry.
    """
    if len(tokens) == 0:
        rtnval: Optional[str] = None
        return rtnval

    first = tokens[0]
    if first == "@include":
        if len(tokens) >= 2:
            rtnval = tokens[1]
        else:
            rtnval = None
        return rtnval

    if first not in _VALID_MODULE_TYPES:
        rtnval = None
        return rtnval

    if len(tokens) < 3:
        rtnval = None
        return rtnval

    rtnval = tokens[2]
    return rtnval


def _toggle_module_state_in_content(
    *,
    content: str,
    module_name: str,
    enable: bool,
) -> tuple:
    """
        Return new content with every reference to ``module_name``
        toggled to the supplied state and a flag indicating whether
        the module appeared (regardless of state).

        Active line:        ``auth required pam_unix.so``
        Disabled line:      ``# auth required pam_unix.so``

        :param content: The current file content.
        :param module_name: The module name to match against the
                            third token of each line.
        :param enable: True to strip leading ``#``; False to add
                       leading ``# ``.

        :returns: ``(new_content, found)``.
    """
    new_lines: list = []
    found_flag = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            new_lines.append(raw_line)
            continue

        is_comment = stripped.startswith("#")
        if is_comment:
            body = stripped.lstrip("#").strip()
            if len(body) == 0:
                new_lines.append(raw_line)
                continue
        else:
            body = stripped

        tokens = _tokenize_pam_line(line=body)
        line_module = _module_name_from_tokens(tokens=tokens)
        if line_module != module_name:
            new_lines.append(raw_line)
            continue

        found_flag = True
        if enable:
            if is_comment:
                new_lines.append(body)
            else:
                new_lines.append(raw_line)
        else:
            if is_comment:
                new_lines.append(raw_line)
            else:
                new_lines.append("# {}".format(raw_line))

    body_out = "\n".join(new_lines)
    if content.endswith("\n") and not body_out.endswith("\n"):
        body_out = body_out + "\n"
    rtnval = (body_out, found_flag)
    return rtnval
