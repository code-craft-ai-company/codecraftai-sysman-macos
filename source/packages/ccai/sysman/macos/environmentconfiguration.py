"""
    macOS environment-configuration feature.

    Implements the cross-OS environment-configuration contract for macOS
    by maintaining variables across two persistence surfaces, in lockstep
    with their runtime equivalents:

    * Runtime: ``launchctl setenv`` (system) and
      ``launchctl asuser <uid> launchctl setenv`` (user).
    * Persistence: a sentinel-bracketed block inside ``/etc/zshenv``
      (system) or ``~<user>/.zshenv`` (user).

    ``/etc/launchd.conf`` is intentionally NOT used: Apple deprecated
    that file in macOS 10.10 (Yosemite) and modern macOS does not honour
    it.  The shell ``zshenv`` block is the supported, equivalent path.

    Sentinel pair:
        ``# --- ccai system env start ---`` /
        ``# --- ccai system env end ---``.

    File I/O policy:
        Every read / write / delete / existence probe is performed
        through the injected :class:`ICommandRunner` (``cat`` /
        ``tee`` / ``rm`` / ``id``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        SYSTEM-scope writes honour ``config.elevated``.  USER-scope
        writes never elevate (matching the Ubuntu sibling's
        same-user-detection limitation; the design calls for
        suppression based on a real "current user" probe and that is
        not yet wired into this package).  Reads never elevate.
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

_DEFAULT_SYSTEM_ZSHENV = "/etc/zshenv"
_DEFAULT_USER_ZSHENV_BASENAME = ".zshenv"

_BLOCK_BEGIN = "# --- ccai system env start ---"
_BLOCK_END = "# --- ccai system env end ---"

_EXPORT_ENV_PATTERN = re.compile(
    r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)


MacosEnvActionStatus = ActionStatus


class MacosEnvScope(Enum):
    """
        Scope selector for environment-configuration verbs.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class MacosEnvironmentConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`MacosEnvironmentConfigurationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param system_zshenv: System-scope persistence file path.
                              Defaults to ``/etc/zshenv``.
        :param user_zshenv_basename: Basename of the user-scope
                                     persistence file written within
                                     each user's home directory.
                                     Defaults to ``.zshenv``.
    """
    system_zshenv: str = _DEFAULT_SYSTEM_ZSHENV
    user_zshenv_basename: str = _DEFAULT_USER_ZSHENV_BASENAME


@dataclass
class MacosEnvActionResult(FeatureActionResult):
    """
        Result of an environment-configuration write operation.

        :param name: The variable name targeted.
        :param value: The value written, or ``None`` for unset
                      operations.
        :param scope: The scope targeted.
        :param user: The username (USER scope only); ``None`` for
                     SYSTEM scope.
        :param path: The file path that was (or would have been)
                     modified.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    scope: Optional[MacosEnvScope] = None
    user: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosEnvValue:
    """
        A single environment variable value.

        :param name: The variable name.
        :param value: The variable value.
        :param scope: The scope the variable was read from.
        :param user: The username for USER scope; ``None`` otherwise.
        :param source: A short tag describing where the value was
                       parsed from (``"launchctl"`` or the absolute
                       path of the persistence file).
    """
    name: str
    value: str
    scope: MacosEnvScope
    user: Optional[str] = None
    source: Optional[str] = None


class MacosEnvQueryError(Exception):
    """
        Raised when an environment read fails for a reason other than
        the target variable simply being absent.
    """

    def __init__(self, *, message: str) -> None:
        """
            :param message: Human-readable detail.
        """
        self.message = message
        err_msg_lines = ["Environment configuration query failed."]
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosEnvironmentConfigurationManager(BaseFeatureManager):
    """
        macOS environment-configuration manager.  Maintains runtime
        ``launchctl`` state and the ``zshenv`` sentinel block in
        lockstep, dispatching every operation through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosEnvActionResult
    status_class = MacosEnvActionStatus
    feature_name = "environment-configuration"

    # ------------------------------------------------------------------
    # System-scope write verbs
    # ------------------------------------------------------------------

    def set_system_env(
        self,
        name: str,
        value: str,
    ) -> MacosEnvActionResult:
        """
            Set a system-scope environment variable in lockstep across
            ``launchctl setenv`` (runtime) and the sentinel block in
            ``/etc/zshenv`` (persistence).

            Skips when both sources already report the desired value.

            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`MacosEnvActionResult`.
        """
        path = self._typed_config().system_zshenv
        runtime_command = [_LAUNCHCTL, "setenv", name, value]

        runtime_value = self._read_launchctl_getenv(name=name, asuser_uid=None)
        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        desired_content = _set_block_export(
            content=existing_content, name=name, value=value,
        )

        runtime_matches = (
            runtime_value is not None and runtime_value == value
        )
        persistent_matches = (
            existing is not None and existing == desired_content
        )

        if runtime_matches and persistent_matches:
            skipped = self._build_result(
                status=MacosEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=MacosEnvScope.SYSTEM,
                user=None,
                path=path,
                command=runtime_command,
                message="value already set in runtime and zshenv",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=MacosEnvScope.SYSTEM,
                user=None,
                path=path,
                command=runtime_command,
            )
            return dry

        last_command = runtime_command
        last_path = path
        if not runtime_matches:
            runtime_run = self._runner.run(
                list(runtime_command),
                elevated=self._config.elevated,
            )
            if runtime_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=runtime_run.exit_code,
                    stdout=runtime_run.stdout,
                    stderr=runtime_run.stderr,
                    command=list(runtime_command),
                    name=name,
                    value=value,
                    scope=MacosEnvScope.SYSTEM,
                    user=None,
                    path=path,
                    message="launchctl_setenv_failed",
                )
                return failed

        if not persistent_matches:
            file_run = self._write_file(
                path=path,
                content=desired_content,
                elevated_override=None,
            )
            last_command = ["tee", path]
            last_path = path
            if file_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=file_run.exit_code,
                    stdout=file_run.stdout,
                    stderr=file_run.stderr,
                    command=list(last_command),
                    name=name,
                    value=value,
                    scope=MacosEnvScope.SYSTEM,
                    user=None,
                    path=path,
                    message="zshenv_write_failed",
                )
                return failed

        success = MacosEnvActionResult(
            status=MacosEnvActionStatus.SUCCESS,
            detail=name,
            command=list(last_command),
            name=name,
            value=value,
            scope=MacosEnvScope.SYSTEM,
            user=None,
            path=last_path,
        )
        return success

    def unset_system_env(self, name: str) -> MacosEnvActionResult:
        """
            Remove a system-scope environment variable from both the
            runtime (``launchctl unsetenv``) and the sentinel block in
            ``/etc/zshenv``.

            Skips when neither source carries the variable.  When the
            sentinel block becomes empty AND the rest of the file is
            also empty, the file is removed entirely.

            :param name: The variable name.

            :returns: The :class:`MacosEnvActionResult`.
        """
        path = self._typed_config().system_zshenv
        runtime_command = [_LAUNCHCTL, "unsetenv", name]

        runtime_value = self._read_launchctl_getenv(name=name, asuser_uid=None)
        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        new_content = _remove_block_export(
            content=existing_content, name=name,
        )

        runtime_present = runtime_value is not None
        persistent_changed = (
            existing is not None and new_content != existing_content
        )

        if not runtime_present and not persistent_changed:
            skipped = self._build_result(
                status=MacosEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=MacosEnvScope.SYSTEM,
                user=None,
                path=path,
                command=runtime_command,
                message="variable not present in runtime or zshenv",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=MacosEnvScope.SYSTEM,
                user=None,
                path=path,
                command=runtime_command,
            )
            return dry

        last_command = runtime_command
        last_path = path
        if runtime_present:
            runtime_run = self._runner.run(
                list(runtime_command),
                elevated=self._config.elevated,
            )
            if runtime_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=runtime_run.exit_code,
                    stdout=runtime_run.stdout,
                    stderr=runtime_run.stderr,
                    command=list(runtime_command),
                    name=name,
                    value=None,
                    scope=MacosEnvScope.SYSTEM,
                    user=None,
                    path=path,
                    message="launchctl_unsetenv_failed",
                )
                return failed

        if persistent_changed:
            if _file_is_empty(content=new_content):
                delete_run = self._delete_file(
                    path=path,
                    elevated_override=None,
                )
                last_command = ["rm", "-f", path]
                last_path = path
                if delete_run.exit_code != 0:
                    failed = MacosEnvActionResult(
                        status=MacosEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=delete_run.exit_code,
                        stdout=delete_run.stdout,
                        stderr=delete_run.stderr,
                        command=list(last_command),
                        name=name,
                        value=None,
                        scope=MacosEnvScope.SYSTEM,
                        user=None,
                        path=path,
                        message="zshenv_delete_failed",
                    )
                    return failed
            else:
                file_run = self._write_file(
                    path=path,
                    content=new_content,
                    elevated_override=None,
                )
                last_command = ["tee", path]
                last_path = path
                if file_run.exit_code != 0:
                    failed = MacosEnvActionResult(
                        status=MacosEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=file_run.exit_code,
                        stdout=file_run.stdout,
                        stderr=file_run.stderr,
                        command=list(last_command),
                        name=name,
                        value=None,
                        scope=MacosEnvScope.SYSTEM,
                        user=None,
                        path=path,
                        message="zshenv_write_failed",
                    )
                    return failed

        success = MacosEnvActionResult(
            status=MacosEnvActionStatus.SUCCESS,
            detail=name,
            command=list(last_command),
            name=name,
            value=None,
            scope=MacosEnvScope.SYSTEM,
            user=None,
            path=last_path,
        )
        return success

    # ------------------------------------------------------------------
    # System-scope read verbs
    # ------------------------------------------------------------------

    def get_system_env(self, name: str) -> Optional[MacosEnvValue]:
        """
            Read a system-scope environment variable.

            ``launchctl getenv`` is consulted first; the persistence
            file is consulted as a fallback so a variable that has
            been written to ``/etc/zshenv`` but not yet picked up at
            runtime still surfaces.

            :param name: The variable name.

            :returns: The :class:`MacosEnvValue`, or ``None`` when the
                      variable is not present in either source.
        """
        runtime_value = self._read_launchctl_getenv(
            name=name, asuser_uid=None,
        )
        if runtime_value is not None:
            rtnval: Optional[MacosEnvValue] = MacosEnvValue(
                name=name,
                value=runtime_value,
                scope=MacosEnvScope.SYSTEM,
                user=None,
                source="launchctl",
            )
            return rtnval

        path = self._typed_config().system_zshenv
        content = self._read_file(path=path)
        if content is not None:
            parsed = _parse_block_exports(text=content)
            if name in parsed:
                rtnval = MacosEnvValue(
                    name=name,
                    value=parsed[name],
                    scope=MacosEnvScope.SYSTEM,
                    user=None,
                    source=path,
                )
                return rtnval

        rtnval = None
        return rtnval

    def list_system_env(self) -> dict[str, str]:
        """
            Enumerate every system-scope variable visible in the
            ``/etc/zshenv`` sentinel block.

            ``launchctl`` exposes no whole-domain enumeration verb on
            modern macOS, so this list reflects the persistent surface
            only.

            :returns: A ``dict[str, str]`` of every parsed entry.
        """
        rtnval: dict[str, str] = {}

        path = self._typed_config().system_zshenv
        content = self._read_file(path=path)
        if content is not None:
            for key, value in _parse_block_exports(text=content).items():
                rtnval[key] = value

        return rtnval

    # ------------------------------------------------------------------
    # User-scope write verbs
    # ------------------------------------------------------------------

    def set_user_env(
        self,
        user: str,
        name: str,
        value: str,
    ) -> MacosEnvActionResult:
        """
            Set a user-scope environment variable in the user's profile.

            The runtime path is ``launchctl asuser <uid> launchctl
            setenv NAME VALUE``; the persistent path is the sentinel
            block inside ``~<user>/.zshenv``.

            Skips when both sources already report the desired value.

            Elevation note:
                User-scope writes always pass ``elevated=False`` to the
                runner.  The design spec calls for suppressing
                elevation when the target user equals the running user
                (and elevating otherwise), but a reliable "current
                user" probe is not yet wired into this package.  See
                ``specs/3-environment-configuration/tasks.md``.

            :param user: The username.
            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`MacosEnvActionResult`.
        """
        try:
            uid, home_dir = self._resolve_user(user=user)
        except MacosEnvQueryError as exc:
            failed = self._build_result(
                status=MacosEnvActionStatus.FAILED,
                name=name,
                value=value,
                scope=MacosEnvScope.USER,
                user=user,
                path=None,
                command=["id", "-u", user],
                message="user not found: {}".format(user),
                stderr=exc.message,
            )
            return failed

        path = self._user_zshenv_path(home_dir=home_dir)
        runtime_command = [
            _LAUNCHCTL, "asuser", str(uid),
            _LAUNCHCTL, "setenv", name, value,
        ]

        runtime_value = self._read_launchctl_getenv(
            name=name, asuser_uid=uid,
        )
        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        desired_content = _set_block_export(
            content=existing_content, name=name, value=value,
        )

        runtime_matches = (
            runtime_value is not None and runtime_value == value
        )
        persistent_matches = (
            existing is not None and existing == desired_content
        )

        if runtime_matches and persistent_matches:
            skipped = self._build_result(
                status=MacosEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=MacosEnvScope.USER,
                user=user,
                path=path,
                command=runtime_command,
                message="value already set in runtime and user zshenv",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=MacosEnvScope.USER,
                user=user,
                path=path,
                command=runtime_command,
            )
            return dry

        last_command = runtime_command
        last_path = path
        if not runtime_matches:
            runtime_run = self._runner.run(
                list(runtime_command),
                elevated=False,
            )
            if runtime_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=runtime_run.exit_code,
                    stdout=runtime_run.stdout,
                    stderr=runtime_run.stderr,
                    command=list(runtime_command),
                    name=name,
                    value=value,
                    scope=MacosEnvScope.USER,
                    user=user,
                    path=path,
                    message="launchctl_asuser_setenv_failed",
                )
                return failed

        if not persistent_matches:
            file_run = self._write_file(
                path=path,
                content=desired_content,
                elevated_override=False,
            )
            last_command = ["tee", path]
            last_path = path
            if file_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=file_run.exit_code,
                    stdout=file_run.stdout,
                    stderr=file_run.stderr,
                    command=list(last_command),
                    name=name,
                    value=value,
                    scope=MacosEnvScope.USER,
                    user=user,
                    path=path,
                    message="user_zshenv_write_failed",
                )
                return failed

        success = MacosEnvActionResult(
            status=MacosEnvActionStatus.SUCCESS,
            detail=name,
            command=list(last_command),
            name=name,
            value=value,
            scope=MacosEnvScope.USER,
            user=user,
            path=last_path,
        )
        return success

    def unset_user_env(
        self,
        user: str,
        name: str,
    ) -> MacosEnvActionResult:
        """
            Remove a user-scope environment variable from both the
            runtime and the user's ``.zshenv`` sentinel block.

            Skips when neither source carries the variable.

            :param user: The username.
            :param name: The variable name.

            :returns: The :class:`MacosEnvActionResult`.
        """
        try:
            uid, home_dir = self._resolve_user(user=user)
        except MacosEnvQueryError as exc:
            failed = self._build_result(
                status=MacosEnvActionStatus.FAILED,
                name=name,
                value=None,
                scope=MacosEnvScope.USER,
                user=user,
                path=None,
                command=["id", "-u", user],
                message="user not found: {}".format(user),
                stderr=exc.message,
            )
            return failed

        path = self._user_zshenv_path(home_dir=home_dir)
        runtime_command = [
            _LAUNCHCTL, "asuser", str(uid),
            _LAUNCHCTL, "unsetenv", name,
        ]

        runtime_value = self._read_launchctl_getenv(
            name=name, asuser_uid=uid,
        )
        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        new_content = _remove_block_export(
            content=existing_content, name=name,
        )

        runtime_present = runtime_value is not None
        persistent_changed = (
            existing is not None and new_content != existing_content
        )

        if not runtime_present and not persistent_changed:
            skipped = self._build_result(
                status=MacosEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=MacosEnvScope.USER,
                user=user,
                path=path,
                command=runtime_command,
                message="variable not present in runtime or user zshenv",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=MacosEnvScope.USER,
                user=user,
                path=path,
                command=runtime_command,
            )
            return dry

        last_command = runtime_command
        last_path = path
        if runtime_present:
            runtime_run = self._runner.run(
                list(runtime_command),
                elevated=False,
            )
            if runtime_run.exit_code != 0:
                failed = MacosEnvActionResult(
                    status=MacosEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=runtime_run.exit_code,
                    stdout=runtime_run.stdout,
                    stderr=runtime_run.stderr,
                    command=list(runtime_command),
                    name=name,
                    value=None,
                    scope=MacosEnvScope.USER,
                    user=user,
                    path=path,
                    message="launchctl_asuser_unsetenv_failed",
                )
                return failed

        if persistent_changed:
            if _file_is_empty(content=new_content):
                delete_run = self._delete_file(
                    path=path,
                    elevated_override=False,
                )
                last_command = ["rm", "-f", path]
                last_path = path
                if delete_run.exit_code != 0:
                    failed = MacosEnvActionResult(
                        status=MacosEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=delete_run.exit_code,
                        stdout=delete_run.stdout,
                        stderr=delete_run.stderr,
                        command=list(last_command),
                        name=name,
                        value=None,
                        scope=MacosEnvScope.USER,
                        user=user,
                        path=path,
                        message="user_zshenv_delete_failed",
                    )
                    return failed
            else:
                file_run = self._write_file(
                    path=path,
                    content=new_content,
                    elevated_override=False,
                )
                last_command = ["tee", path]
                last_path = path
                if file_run.exit_code != 0:
                    failed = MacosEnvActionResult(
                        status=MacosEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=file_run.exit_code,
                        stdout=file_run.stdout,
                        stderr=file_run.stderr,
                        command=list(last_command),
                        name=name,
                        value=None,
                        scope=MacosEnvScope.USER,
                        user=user,
                        path=path,
                        message="user_zshenv_write_failed",
                    )
                    return failed

        success = MacosEnvActionResult(
            status=MacosEnvActionStatus.SUCCESS,
            detail=name,
            command=list(last_command),
            name=name,
            value=None,
            scope=MacosEnvScope.USER,
            user=user,
            path=last_path,
        )
        return success

    # ------------------------------------------------------------------
    # Internal helpers — config / I/O / probes / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosEnvironmentConfigurationManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosEnvironmentConfigurationManagerConfig`.

            :returns: The active config typed as
                      :class:`MacosEnvironmentConfigurationManagerConfig`.
        """
        if isinstance(
            self._config,
            MacosEnvironmentConfigurationManagerConfig,
        ):
            rtnval = self._config
        else:
            rtnval = MacosEnvironmentConfigurationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_user(self, *, user: str) -> tuple:
        """
            Resolve a username's uid and home directory.

            Uses ``id -u <user>`` for the uid and ``dscl . -read
            /Users/<user> NFSHomeDirectory`` for the home directory
            because macOS' user database is layered atop OpenDirectory
            and ``getent`` is unreliable.

            :param user: The username.

            :returns: ``(uid, home)`` tuple where ``uid`` is an
                      ``int`` and ``home`` is an absolute path.
            :raises MacosEnvQueryError: When either probe fails.
        """
        uid_command = ["id", "-u", user]
        uid_run = self._runner.run(list(uid_command), elevated=False)
        if uid_run.exit_code != 0:
            raise MacosEnvQueryError(
                message="id -u {} returned non-zero".format(user),
            )
        uid_text = uid_run.stdout.strip()
        try:
            uid = int(uid_text)
        except ValueError:
            raise MacosEnvQueryError(
                message="id -u {} returned non-numeric: {!r}".format(
                    user, uid_text,
                ),
            )

        home_command = [
            "dscl", ".", "-read",
            "/Users/{}".format(user), "NFSHomeDirectory",
        ]
        home_run = self._runner.run(list(home_command), elevated=False)
        if home_run.exit_code != 0:
            raise MacosEnvQueryError(
                message="dscl . -read /Users/{} failed".format(user),
            )

        home_dir = _parse_dscl_home(stdout=home_run.stdout)
        if home_dir is None:
            raise MacosEnvQueryError(
                message="dscl returned no NFSHomeDirectory for {}".format(
                    user,
                ),
            )

        rtnval = (uid, home_dir)
        return rtnval

    def _user_zshenv_path(self, *, home_dir: str) -> str:
        """
            Build the absolute path to the user's ``.zshenv``.

            :param home_dir: The user's home directory.

            :returns: ``<home_dir>/<user_zshenv_basename>``.
        """
        rtnval = "{}/{}".format(
            home_dir, self._typed_config().user_zshenv_basename,
        )
        return rtnval

    def _read_launchctl_getenv(
        self,
        *,
        name: str,
        asuser_uid: Optional[int],
    ) -> Optional[str]:
        """
            Read the runtime value of an environment variable from
            ``launchctl``.

            ``launchctl getenv`` exits with code 0 in both the
            "present" and "absent" cases on modern macOS.  When the
            variable is absent the stdout is empty (just a newline).
            We treat empty stdout as ``None``.

            :param name: The variable name.
            :param asuser_uid: When not None, prepend
                               ``launchctl asuser <uid>`` so the
                               getenv probes the user-domain runtime.

            :returns: The runtime value as a string, or ``None`` when
                      not present.
        """
        if asuser_uid is None:
            command = [_LAUNCHCTL, "getenv", name]
        else:
            command = [
                _LAUNCHCTL, "asuser", str(asuser_uid),
                _LAUNCHCTL, "getenv", name,
            ]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        text = run_result.stdout.rstrip("\n")
        if len(text) == 0:
            rtnval = None
            return rtnval
        rtnval = text
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

    def _write_file(
        self,
        *,
        path: str,
        content: str,
        elevated_override: Optional[bool],
    ):
        """
            Write a file via ``tee`` with ``content`` on stdin.

            :param path: The absolute path to write.
            :param content: The full file content.
            :param elevated_override: When not None, override the
                                      config's ``elevated`` flag for
                                      this call (used by user-scope
                                      writes to suppress elevation).

            :returns: The runner's :class:`RunResult`.
        """
        command = ["tee", path]
        if elevated_override is None:
            run_elevated = self._config.elevated
        else:
            run_elevated = elevated_override
        run_result = self._runner.run(
            list(command),
            elevated=run_elevated,
            stdin=content,
        )
        return run_result

    def _delete_file(
        self,
        *,
        path: str,
        elevated_override: Optional[bool],
    ):
        """
            Delete a file via ``rm -f``.

            :param path: The absolute path to delete.
            :param elevated_override: When not None, override the
                                      config's ``elevated`` flag for
                                      this call.

            :returns: The runner's :class:`RunResult`.
        """
        command = ["rm", "-f", path]
        if elevated_override is None:
            run_elevated = self._config.elevated
        else:
            run_elevated = elevated_override
        run_result = self._runner.run(
            list(command),
            elevated=run_elevated,
        )
        return run_result

    def _build_result(
        self,
        *,
        status: MacosEnvActionStatus,
        name: Optional[str],
        value: Optional[str],
        scope: Optional[MacosEnvScope],
        user: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosEnvActionResult:
        """
            Construct a :class:`MacosEnvActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param name: The variable name to echo on the result.
            :param value: The would-be value.
            :param scope: The scope targeted.
            :param user: The username for USER scope.
            :param path: The file path the action targeted.
            :param command: The would-have-been argv.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`MacosEnvActionResult`.
        """
        rtnval = MacosEnvActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            scope=scope,
            user=user,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and renderers
# ----------------------------------------------------------------------


def _parse_dscl_home(*, stdout: str) -> Optional[str]:
    """
        Parse ``dscl . -read /Users/<user> NFSHomeDirectory`` output
        and return the home directory.

        ``dscl`` emits ``NFSHomeDirectory: /Users/<user>`` on a single
        line.  Multiline ``dsAttrTypeNative:`` shapes are also
        tolerated.

        :param stdout: Raw stdout text.

        :returns: The home directory, or ``None`` when not found.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("NFSHomeDirectory:"):
            _, _, value = line.partition(":")
            home = value.strip()
            if len(home) > 0:
                return home
    return None


def _parse_block_exports(*, text: str) -> dict[str, str]:
    """
        Parse ``export NAME="VALUE"`` lines that live inside the
        ccai sentinel block.

        Lines outside the block are ignored entirely so the manager
        does not surface user-authored environment overrides.

        :param text: Raw file content.

        :returns: A ``dict[str, str]`` of every parsed entry.
    """
    rtnval: dict[str, str] = {}
    block_start_idx, block_end_idx = _find_block(content=text)
    if block_start_idx is None or block_end_idx is None:
        return rtnval

    lines = text.splitlines()
    for idx in range(block_start_idx + 1, block_end_idx):
        stripped = lines[idx].strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            continue
        match = _EXPORT_ENV_PATTERN.match(stripped)
        if match is None:
            continue
        name = match.group(1)
        raw_value = match.group(2)
        rtnval[name] = _strip_quotes(value=raw_value)
    return rtnval


def _strip_quotes(*, value: str) -> str:
    """
        Strip a single pair of surrounding double quotes from
        ``value``.

        :param value: Raw value text.

        :returns: The unquoted (and unescaped) value.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        unescaped = inner.replace('\\"', '"').replace("\\\\", "\\")
        return unescaped
    return value


def _escape_value(*, value: str) -> str:
    """
        Escape a value for double-quoted shell rendering.

        :param value: The raw value.

        :returns: The escaped value (no surrounding quotes).
    """
    rtnval = value.replace("\\", "\\\\").replace('"', '\\"')
    return rtnval


def _set_block_export(
    *,
    content: str,
    name: str,
    value: str,
) -> str:
    """
        Return ``content`` updated so the codecraftai-managed block
        contains ``export NAME="value"`` for ``name``.

        When the block is missing, a fresh sentinel-bracketed block is
        appended at the end of the file.  Lines outside the block are
        preserved verbatim.

        :param content: Current file content.
        :param name: The variable name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = 'export {}="{}"'.format(name, escaped)

    block_start_idx, block_end_idx = _find_block(content=content)

    if block_start_idx is None or block_end_idx is None:
        if len(content) == 0:
            base = ""
        elif content.endswith("\n"):
            base = content
        else:
            base = content + "\n"
        block_lines = [
            _BLOCK_BEGIN,
            new_line,
            _BLOCK_END,
        ]
        body = base + "\n".join(block_lines) + "\n"
        return body

    lines = content.splitlines()
    updated: list[str] = list(lines)

    replaced = False
    for idx in range(block_start_idx + 1, block_end_idx):
        match = _EXPORT_ENV_PATTERN.match(updated[idx].strip())
        if match is not None and match.group(1) == name:
            updated[idx] = new_line
            replaced = True
            break

    if not replaced:
        updated.insert(block_end_idx, new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_block_export(*, content: str, name: str) -> str:
    """
        Return ``content`` with any matching ``export NAME=`` line
        removed from the codecraftai-managed block.

        When the block becomes empty, the sentinel comments are also
        removed so a future run produces a clean file.

        :param content: Current file content.
        :param name: The variable name to remove.

        :returns: The updated file content.
    """
    block_start_idx, block_end_idx = _find_block(content=content)
    if block_start_idx is None or block_end_idx is None:
        return content

    lines = content.splitlines()
    new_block: list[str] = []
    removed = False
    for idx in range(block_start_idx + 1, block_end_idx):
        match = _EXPORT_ENV_PATTERN.match(lines[idx].strip())
        if match is not None and match.group(1) == name:
            removed = True
            continue
        new_block.append(lines[idx])

    if not removed:
        return content

    head = lines[:block_start_idx]
    tail = lines[block_end_idx + 1:]

    if len(new_block) == 0:
        merged = head + tail
    else:
        merged = head + [_BLOCK_BEGIN] + new_block + [_BLOCK_END] + tail

    body = "\n".join(merged)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _find_block(*, content: str) -> tuple:
    """
        Locate the start/end line indices of the codecraftai-managed block.

        :param content: File content.

        :returns: ``(begin_index, end_index)`` line tuple, or
                  ``(None, None)`` when the block is missing.
    """
    lines = content.splitlines()
    begin_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if begin_idx is None and stripped == _BLOCK_BEGIN:
            begin_idx = idx
            continue
        if begin_idx is not None and stripped == _BLOCK_END:
            end_idx = idx
            break
    rtnval = (begin_idx, end_idx)
    return rtnval


def _file_is_empty(*, content: str) -> bool:
    """
        Return True when the file contains no non-blank, non-comment
        lines.

        :param content: File content.

        :returns: True when no actionable lines remain.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        return False
    return True
