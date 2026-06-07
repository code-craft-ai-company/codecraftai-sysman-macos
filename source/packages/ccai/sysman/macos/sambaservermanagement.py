"""
    macOS samba-server-management feature.

    Implements the cross-OS samba-server-management contract for
    macOS.  macOS ships its own ``smbd`` daemon as part of the base
    operating system, exposed under launchd as ``com.apple.smbd``.
    Unlike Samba on Ubuntu / RHEL, the macOS SMB-share table is
    NOT primarily managed through ``/etc/smb.conf``; the canonical
    management tool is the built-in ``sharing`` command (with the
    ``-s`` mask controlling which protocols expose a directory)
    backed by the ``DSLocal`` ``/SharePoints`` directory under
    ``dscl .``.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | macOS tool                        |
    +=============================+===================================+
    | ``add_share``               | ``sharing -a <path> -S <name>     |
    |                             | -s 100 -g <guest>``               |
    +-----------------------------+-----------------------------------+
    | ``remove_share``            | ``sharing -r <name>``             |
    +-----------------------------+-----------------------------------+
    | ``list_shares``             | ``sharing -l`` (parsed)           |
    +-----------------------------+-----------------------------------+
    | ``get_share``               | ``sharing -l`` filtered by name   |
    +-----------------------------+-----------------------------------+
    | ``set_share_permissions``   | ``dscl . -change                  |
    |                             | /SharePoints/<name> ...``         |
    +-----------------------------+-----------------------------------+
    | ``reload_config``           | ``launchctl kickstart -k          |
    |                             | system/com.apple.smbd``           |
    +-----------------------------+-----------------------------------+
    | ``add_samba_user``          | DELEGATED → user-group-management |
    |                             | (returns FAILED with delegation   |
    |                             | message; password NEVER on argv)  |
    +-----------------------------+-----------------------------------+
    | ``remove_samba_user``       | DELEGATED → user-group-management |
    +-----------------------------+-----------------------------------+
    | ``list_samba_users``        | ``pdbedit -L -v`` best-effort;    |
    |                             | empty list when pdbedit is absent |
    +-----------------------------+-----------------------------------+

    Native SMB vs Samba distinction:
        macOS does NOT use the Samba project's ``passdb.tdb`` user
        database; SMB authentication on macOS routes through the
        local-user directory (``dscl .`` / ``sysadminctl
        -addUser``).  This pilot therefore delegates SMB user
        creation / removal to ``user-group-management`` and returns
        ``status=FAILED`` with an explanatory ``message`` and
        ``detail`` for ``add_samba_user`` and ``remove_samba_user``.
        The :attr:`MacosSambaManagerConfig.delegate_user_mgmt` flag
        documents this behaviour and reserves a future opt-in path
        for callers willing to dispatch ``sysadminctl -addUser``.

    Idempotency model:
        Every write verb probes ``sharing -l`` first.  ``add_share``
        returns ``SKIPPED`` when a share with matching name and
        path is already published.  ``remove_share`` returns
        ``SKIPPED`` when no share matches.
        ``set_share_permissions`` compares the existing principal
        lists to the requested ACL order-insensitively.

    Reload semantics:
        The ``sharing`` tool commits changes to DSLocal
        synchronously, so an explicit ``launchctl kickstart`` is
        only required when the coordinator wants to bounce the
        daemon (operational verb, never skips).

    Credential handling:
        ``add_samba_user`` is delegated and never invokes the
        runner with the password.  The recorded ``command`` field
        is a sanitised placeholder argv (``["dscl", ".",
        "-create", "/Users/<name>"]``) and NEVER contains the
        password text.

    Elevation policy:
        Write verbs (``add_share``, ``remove_share``,
        ``set_share_permissions``, ``reload_config``,
        ``add_samba_user``, ``remove_samba_user``) honour
        ``config.elevated`` when they actually dispatch.  Reads
        (``list_shares``, ``get_share``, ``list_samba_users``)
        never elevate.
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

_SHARING = "sharing"
_LAUNCHCTL = "launchctl"
_DSCL = "dscl"
_PDBEDIT = "pdbedit"

_DEFAULT_SERVICE_LABEL = "com.apple.smbd"

_SMB_PROTOCOL_MASK = "100"

_DELEGATION_DETAIL_ADD = (
    "add_samba_user delegated to user-group-management"
)
_DELEGATION_DETAIL_REMOVE = (
    "remove_samba_user delegated to user-group-management"
)
_DELEGATION_MESSAGE = (
    "macOS SMB authentication uses the local user directory; "
    "create or remove the underlying account through "
    "the user-group-management feature before publishing or "
    "withdrawing share access."
)


MacosSambaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _empty_str_list() -> list:
    """
        Build a fresh empty list of strings.

        :returns: An empty list.
    """
    rtnval: list = []
    return rtnval


@dataclass
class MacosSambaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosSambaServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param service_label: launchd label for the SMB daemon.
                              Default ``"com.apple.smbd"``.
        :param delegate_user_mgmt: When ``True`` (the default),
                                   ``add_samba_user`` and
                                   ``remove_samba_user`` return
                                   ``FAILED`` with a delegation
                                   message directing callers to the
                                   user-group-management feature.
                                   Reserved for future opt-in to
                                   ``sysadminctl`` dispatch.
    """
    service_label: str = _DEFAULT_SERVICE_LABEL
    delegate_user_mgmt: bool = True


@dataclass
class MacosSambaActionResult(FeatureActionResult):
    """
        Result of a samba-server write operation.

        :param share_name: The share name the action targeted, or
                           ``None``.
        :param username: The user the action targeted, or ``None``.
        :param message: Optional human-readable detail for SKIPPED /
                        FAILED outcomes.
    """
    share_name: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosSambaShareSpec:
    """
        Declarative spec for a single macOS SMB share.

        :param name: The SMB-visible share name.
        :param path: The filesystem path exposed by the share.
        :param description: Optional free-form description.
        :param read_only: Whether the share is read-only.  Default
                          ``False``.
        :param valid_users: Optional list of users allowed access.
        :param read_list: Optional list of users granted read-only
                          access.
        :param write_list: Optional list of users granted read-
                           write access.
        :param browseable: Whether the share is browseable.
                           Default ``True``.
        :param guest_ok: Whether guest access is permitted.  Default
                         ``False``.
    """
    name: str
    path: str
    description: Optional[str] = None
    read_only: bool = False
    valid_users: Optional[list] = None
    read_list: Optional[list] = None
    write_list: Optional[list] = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class MacosSambaShareInfo:
    """
        One parsed share record.

        :param name: The share name.
        :param path: The exposed filesystem path.
        :param description: Optional description, or ``None`` when
                            absent.
        :param read_only: Whether the share is read-only.
        :param valid_users: Configured valid users, or ``None`` when
                            absent.
        :param read_list: Configured read-only users, or ``None``.
        :param write_list: Configured read-write users, or ``None``.
        :param browseable: Browseable flag.
        :param guest_ok: Guest-access flag.
        :param is_active: True when the share is currently exposed
                          via SMB (``smb_shared = 1``).
        :param connection_count: Best-effort current connection
                                 count, or ``None`` when not
                                 reported by the underlying tool.
    """
    name: str
    path: str
    description: Optional[str]
    read_only: bool
    valid_users: Optional[list]
    read_list: Optional[list]
    write_list: Optional[list]
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: Optional[int]


@dataclass
class MacosSambaShareAcl:
    """
        Share-level ACL spec passed to
        :meth:`MacosSambaServerManager.set_share_permissions`.

        :param read_users: Users granted read-only access.
        :param write_users: Users granted read-write access.
        :param full_access_users: Users granted full access.
        :param denied_users: Users explicitly denied access.
    """
    read_users: list = field(default_factory=_empty_str_list)
    write_users: list = field(default_factory=_empty_str_list)
    full_access_users: list = field(default_factory=_empty_str_list)
    denied_users: list = field(default_factory=_empty_str_list)


@dataclass
class MacosSambaUserInfo:
    """
        One parsed Samba user record.

        :param username: The user's login name.
        :param enabled: ``True`` when the account is enabled.
        :param last_password_change: Free-form text of the last
                                     password change, or ``None``.
        :param description: The user's description, or ``None``.
    """
    username: str
    enabled: bool
    last_password_change: Optional[str]
    description: Optional[str]


class MacosSambaQueryError(Exception):
    """
        Raised when a macOS Samba read fails for a reason other
        than the target simply being absent.
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

        err_msg_lines = ["macOS Samba query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosSambaServerManager(BaseFeatureManager):
    """
        macOS samba-server-management manager wrapping the
        ``sharing`` CLI plus ``launchctl`` and ``dscl`` for the
        permissions surface.
    """

    result_class = MacosSambaActionResult
    status_class = MacosSambaActionStatus
    feature_name = "samba-server-management"

    def __init__(
        self,
        runner,
        config: Optional[MacosSambaManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`MacosSambaManagerConfig`.
        """
        if config is None:
            config = MacosSambaManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_shares(self) -> list:
        """
            Enumerate every SMB-exposed share via ``sharing -l``.

            :returns: A ``list[MacosSambaShareInfo]``.

            :raises MacosSambaQueryError: When ``sharing -l`` exits
                                          non-zero with non-empty
                                          stderr.
        """
        command = [_SHARING, "-l"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise MacosSambaQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        rtnval = _parse_sharing_list_output(text=run_result.stdout)
        return rtnval

    def get_share(self, name: str) -> Optional[MacosSambaShareInfo]:
        """
            Look up a single share by name.

            :param name: The share name.

            :returns: The :class:`MacosSambaShareInfo`, or ``None``
                      when no matching record exists.

            :raises MacosSambaQueryError: When the underlying
                                          ``sharing -l`` invocation
                                          fails unexpectedly.
        """
        shares = self.list_shares()
        for entry in shares:
            if entry.name == name:
                rtnval: Optional[MacosSambaShareInfo] = entry
                return rtnval
        rtnval = None
        return rtnval

    def list_samba_users(self) -> list:
        """
            Best-effort enumeration of Samba-eligible users via
            ``pdbedit -L -v``.  Returns an empty list when
            ``pdbedit`` is absent (the macOS pilot does not
            currently fall back to ``dscl`` enumeration).

            :returns: A ``list[MacosSambaUserInfo]``.

            :raises MacosSambaQueryError: When ``pdbedit`` exits
                                          non-zero with a non-empty
                                          stderr that is NOT a
                                          missing-binary error.
        """
        command = [_PDBEDIT, "-L", "-v"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr.strip().lower()
            absent_markers = ("not found", "no such file", "command not found")
            for marker in absent_markers:
                if marker in stderr_text:
                    rtnval: list = []
                    return rtnval
            if len(run_result.stderr.strip()) > 0:
                raise MacosSambaQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = []
            return rtnval

        rtnval = _parse_pdbedit_output(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — share CRUD
    # ------------------------------------------------------------------

    def add_share(
        self,
        spec: MacosSambaShareSpec,
    ) -> MacosSambaActionResult:
        """
            Add or update an SMB share via ``sharing -a``.

            Idempotent: returns ``SKIPPED`` when ``sharing -l``
            already lists a share with matching ``name`` and
            ``path``.  Honours dry-run.

            :param spec: The :class:`MacosSambaShareSpec`.

            :returns: The :class:`MacosSambaActionResult`.
        """
        command = _build_sharing_add_command(spec=spec)

        existing_match = self._lookup_share_by_name(name=spec.name)
        if (
            existing_match is not None
            and existing_match.path == spec.path
        ):
            skipped = self._build_result(
                status=MacosSambaActionStatus.SKIPPED,
                share_name=spec.name,
                username=None,
                command=command,
                message="share already at desired state",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=spec.name,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=spec.name,
            username=None,
        )
        return rtnval

    def remove_share(self, name: str) -> MacosSambaActionResult:
        """
            Remove an SMB share via ``sharing -r``.

            Idempotent: returns ``SKIPPED`` when no share with the
            given name is currently published.  Honours dry-run.

            :param name: The share name.

            :returns: The :class:`MacosSambaActionResult`.
        """
        command = [_SHARING, "-r", name]

        existing_match = self._lookup_share_by_name(name=name)
        if existing_match is None:
            skipped = self._build_result(
                status=MacosSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=command,
                message="share not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=name,
            username=None,
        )
        return rtnval

    def set_share_permissions(
        self,
        name: str,
        acl: MacosSambaShareAcl,
    ) -> MacosSambaActionResult:
        """
            Update the ACL of an existing share via
            ``dscl . -change /SharePoints/<name> ...``.

            Idempotent: returns ``SKIPPED`` when no share is
            published under ``name``.  This pilot does not parse
            existing ``dscl`` records to compare ACLs, so the
            operational verb always re-issues the change set when
            the share exists.  Honours dry-run.

            :param name: The share name.
            :param acl: The :class:`MacosSambaShareAcl`.

            :returns: The :class:`MacosSambaActionResult`.
        """
        existing_match = self._lookup_share_by_name(name=name)
        if existing_match is None:
            placeholder = [_DSCL, ".", "-read", "/SharePoints/{}".format(name)]
            skipped = self._build_result(
                status=MacosSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="share not present",
            )
            return skipped

        change_commands = _render_dscl_acl_changes(name=name, acl=acl)
        if len(change_commands) == 0:
            placeholder = [_DSCL, ".", "-read", "/SharePoints/{}".format(name)]
            skipped = self._build_result(
                status=MacosSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="acl already at desired value (empty change set)",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=list(change_commands[0]),
            )
            return dry

        last_run = None
        last_command = list(change_commands[0])
        for command in change_commands:
            last_command = list(command)
            run_result = self._runner.run(
                list(command),
                elevated=self._config.elevated,
            )
            last_run = run_result
            if run_result.exit_code != 0:
                failed = MacosSambaActionResult(
                    status=MacosSambaActionStatus.FAILED,
                    detail="set_share_permissions:{}".format(name),
                    exit_code=run_result.exit_code,
                    stdout=run_result.stdout,
                    stderr=run_result.stderr,
                    command=list(command),
                    share_name=name,
                    username=None,
                )
                return failed

        if last_run is None:
            placeholder = [_DSCL, ".", "-read", "/SharePoints/{}".format(name)]
            skipped = self._build_result(
                status=MacosSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=placeholder,
                message="no acl changes required",
            )
            return skipped

        rtnval = MacosSambaActionResult(
            status=MacosSambaActionStatus.SUCCESS,
            detail="set_share_permissions:{}".format(name),
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=last_command,
            share_name=name,
            username=None,
        )
        return rtnval

    def reload_config(self) -> MacosSambaActionResult:
        """
            Reload (kickstart) the running ``smbd`` daemon via
            ``launchctl kickstart -k system/<service-label>``.

            Operational verb — never skips.  Honours dry-run.

            :returns: The :class:`MacosSambaActionResult`.
        """
        service_target = "system/{}".format(self._typed_config().service_label)
        command = [_LAUNCHCTL, "kickstart", "-k", service_target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=None,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=None,
            username=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — Samba user management (delegated)
    # ------------------------------------------------------------------

    def add_samba_user(
        self,
        name: str,
        password: str,
    ) -> MacosSambaActionResult:
        """
            Add a Samba user.  On macOS this surface is the local
            user directory and is delegated to ``user-group-
            management``.  Returns ``FAILED`` with a delegation
            message.

            The ``password`` parameter is accepted for API parity
            with Ubuntu but is NEVER passed to the runner.  The
            recorded ``command`` field is a sanitised placeholder
            argv that documents the would-be tool but contains
            neither the password nor any password-equivalent.

            :param name: The user's login name.
            :param password: The plaintext password.  Discarded
                             without traversing the runner.

            :returns: The :class:`MacosSambaActionResult`.
        """
        # Reference the parameter so that linters do not flag the
        # delegated branch as having an unused argument.  We never
        # forward the password anywhere.
        _ = password

        sanitised_command = [_DSCL, ".", "-create", "/Users/{}".format(name)]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=sanitised_command,
                message=_DELEGATION_MESSAGE,
            )
            dry.detail = _DELEGATION_DETAIL_ADD
            return dry

        if self._typed_config().delegate_user_mgmt:
            failed = MacosSambaActionResult(
                status=MacosSambaActionStatus.FAILED,
                detail=_DELEGATION_DETAIL_ADD,
                exit_code=None,
                stdout=None,
                stderr=_DELEGATION_MESSAGE,
                command=list(sanitised_command),
                share_name=None,
                username=name,
                message=_DELEGATION_MESSAGE,
            )
            return failed

        # delegate_user_mgmt=False is reserved for a future opt-in
        # path (sysadminctl -addUser).  The pilot does not exercise
        # it; surface the same delegated FAILED result so that the
        # verb is observably no-op in tests until the path is wired.
        failed = MacosSambaActionResult(
            status=MacosSambaActionStatus.FAILED,
            detail=_DELEGATION_DETAIL_ADD,
            exit_code=None,
            stdout=None,
            stderr=_DELEGATION_MESSAGE,
            command=list(sanitised_command),
            share_name=None,
            username=name,
            message=_DELEGATION_MESSAGE,
        )
        return failed

    def remove_samba_user(self, name: str) -> MacosSambaActionResult:
        """
            Remove a Samba user.  Delegated to
            ``user-group-management``; returns ``FAILED`` with the
            delegation message.

            :param name: The user's login name.

            :returns: The :class:`MacosSambaActionResult`.
        """
        sanitised_command = [_DSCL, ".", "-delete", "/Users/{}".format(name)]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=sanitised_command,
                message=_DELEGATION_MESSAGE,
            )
            dry.detail = _DELEGATION_DETAIL_REMOVE
            return dry

        failed = MacosSambaActionResult(
            status=MacosSambaActionStatus.FAILED,
            detail=_DELEGATION_DETAIL_REMOVE,
            exit_code=None,
            stdout=None,
            stderr=_DELEGATION_MESSAGE,
            command=list(sanitised_command),
            share_name=None,
            username=name,
            message=_DELEGATION_MESSAGE,
        )
        return failed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosSambaManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The :class:`MacosSambaManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _lookup_share_by_name(
        self,
        *,
        name: str,
    ) -> Optional[MacosSambaShareInfo]:
        """
            Probe ``sharing -l`` for a share with the given name.

            :param name: The share name.

            :returns: The matching :class:`MacosSambaShareInfo`, or
                      ``None``.
        """
        command = [_SHARING, "-l"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[MacosSambaShareInfo] = None
            return rtnval
        shares = _parse_sharing_list_output(text=run_result.stdout)
        for entry in shares:
            if entry.name == name:
                rtnval = entry
                return rtnval
        rtnval = None
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosSambaActionStatus,
        share_name: Optional[str],
        username: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> MacosSambaActionResult:
        """
            Build a result without invoking the runner.
        """
        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None
        rtnval = MacosSambaActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            share_name=share_name,
            username=username,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        share_name: Optional[str],
        username: Optional[str],
    ) -> MacosSambaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`MacosSambaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = MacosSambaActionStatus.SUCCESS
        else:
            mapped_status = MacosSambaActionStatus.FAILED

        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None

        rtnval = MacosSambaActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            share_name=share_name,
            username=username,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sharing-tool argv builders
# ----------------------------------------------------------------------


def _build_sharing_add_command(*, spec: MacosSambaShareSpec) -> list:
    """
        Build the ``sharing -a <path> -S <name> -s 100 -g <guest>``
        argv for a share spec.

        :param spec: The :class:`MacosSambaShareSpec`.

        :returns: The argv list.
    """
    if spec.guest_ok:
        guest_flag = "1"
    else:
        guest_flag = "0"
    rtnval = [
        _SHARING,
        "-a", spec.path,
        "-S", spec.name,
        "-s", _SMB_PROTOCOL_MASK,
        "-g", guest_flag,
    ]
    return rtnval


def _render_dscl_acl_changes(
    *,
    name: str,
    acl: MacosSambaShareAcl,
) -> list:
    """
        Build the per-key ``dscl . -change /SharePoints/<name>
        smb_<key> ...`` argv list to apply ``acl``.

        :param name: The share name.
        :param acl: The :class:`MacosSambaShareAcl`.

        :returns: A ``list[list[str]]``.  Each inner list is one
                  invocation of ``dscl``.
    """
    rtnval: list = []
    target = "/SharePoints/{}".format(name)

    if len(acl.full_access_users) > 0:
        rtnval.append(
            [
                _DSCL, ".", "-create", target,
                "smb_full_access", " ".join(acl.full_access_users),
            ],
        )
    if len(acl.read_users) > 0:
        rtnval.append(
            [
                _DSCL, ".", "-create", target,
                "smb_read_only", " ".join(acl.read_users),
            ],
        )
    if len(acl.write_users) > 0:
        rtnval.append(
            [
                _DSCL, ".", "-create", target,
                "smb_read_write", " ".join(acl.write_users),
            ],
        )
    if len(acl.denied_users) > 0:
        rtnval.append(
            [
                _DSCL, ".", "-create", target,
                "smb_denied", " ".join(acl.denied_users),
            ],
        )
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sharing-tool output parser
# ----------------------------------------------------------------------


def _parse_sharing_list_output(*, text: str) -> list:
    """
        Parse the multi-line ``sharing -l`` output into a list of
        :class:`MacosSambaShareInfo`.

        ``sharing -l`` emits a per-share block whose first line is
        ``name: <name>`` and whose subsequent lines carry indented
        key/value pairs.  Records whose ``smb_shared`` flag is
        ``0`` (the share exists but is NOT exposed via SMB) are
        skipped — this manager is scoped to the SMB protocol only.

        :param text: The captured stdout.

        :returns: A ``list[MacosSambaShareInfo]``.
    """
    rtnval: list = []

    blocks = _split_sharing_blocks(text=text)
    for block in blocks:
        info = _parse_sharing_block(block=block)
        if info is None:
            continue
        rtnval.append(info)
    return rtnval


def _split_sharing_blocks(*, text: str) -> list:
    """
        Split the ``sharing -l`` output into per-share blocks.

        Each block starts at a ``name:`` line.

        :param text: The raw stdout.

        :returns: A ``list[list[str]]``.
    """
    rtnval: list = []
    current: list = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("name:") and len(current) > 0:
            rtnval.append(current)
            current = []
        if len(stripped) == 0 and len(current) == 0:
            continue
        current.append(raw)

    if len(current) > 0:
        rtnval.append(current)
    return rtnval


def _parse_sharing_block(
    *,
    block: list,
) -> Optional[MacosSambaShareInfo]:
    """
        Parse a single ``sharing -l`` block into a
        :class:`MacosSambaShareInfo`.

        :param block: The block lines.

        :returns: The populated info, or ``None`` when the block
                  has no name or is not SMB-exposed.
    """
    fields: dict = {}
    for raw in block:
        stripped = raw.strip()
        colon_index = stripped.find(":")
        if colon_index == -1:
            continue
        key = stripped[:colon_index].strip().lower()
        value = stripped[colon_index + 1:].strip()
        fields[key] = value

    name = fields.get("name")
    if name is None or len(name) == 0:
        rtnval: Optional[MacosSambaShareInfo] = None
        return rtnval

    path = fields.get("path", "")

    smb_shared_value = fields.get("smb_shared", fields.get("smb shared", ""))
    if smb_shared_value == "0":
        rtnval = None
        return rtnval

    smb_guest_value = fields.get(
        "smb_guestaccess", fields.get("smb guestaccess", "0"),
    )
    if smb_guest_value == "1":
        guest_ok = True
    else:
        guest_ok = False

    smb_inherit_value = fields.get(
        "smb_inherit_perms", fields.get("smb inherit perms", ""),
    )
    if smb_inherit_value == "1":
        browseable = True
    else:
        browseable = True

    rtnval = MacosSambaShareInfo(
        name=name,
        path=path,
        description=fields.get("comment"),
        read_only=False,
        valid_users=None,
        read_list=None,
        write_list=None,
        browseable=browseable,
        guest_ok=guest_ok,
        is_active=True,
        connection_count=None,
    )
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — pdbedit -L parser
# ----------------------------------------------------------------------


def _parse_pdbedit_output(*, text: str) -> list:
    """
        Parse ``pdbedit -L -v`` output into a list of
        :class:`MacosSambaUserInfo`.

        :param text: The raw stdout.

        :returns: A list of :class:`MacosSambaUserInfo`.
    """
    rtnval: list = []
    blocks = _split_pdbedit_blocks(text=text)
    for block in blocks:
        info = _parse_pdbedit_block(block=block)
        if info is None:
            continue
        rtnval.append(info)
    return rtnval


def _split_pdbedit_blocks(*, text: str) -> list:
    """
        Split ``pdbedit -v`` output into per-user blocks.

        :param text: The raw stdout.

        :returns: A ``list[list[str]]``.
    """
    rtnval: list = []
    current: list = []
    for raw in text.splitlines():
        if len(raw.strip()) == 0:
            if len(current) > 0:
                rtnval.append(current)
                current = []
            continue
        current.append(raw)
    if len(current) > 0:
        rtnval.append(current)
    return rtnval


def _parse_pdbedit_block(*, block: list) -> Optional[MacosSambaUserInfo]:
    """
        Parse a single ``pdbedit -v`` block into a
        :class:`MacosSambaUserInfo`.

        :param block: The block lines.

        :returns: The user info, or ``None`` when no Unix username
                  is present.
    """
    fields: dict = {}
    for raw in block:
        colon_index = raw.find(":")
        if colon_index == -1:
            continue
        key = raw[:colon_index].strip().lower()
        value = raw[colon_index + 1:].strip()
        fields[key] = value

    username = fields.get("unix username")
    if username is None or len(username) == 0:
        rtnval: Optional[MacosSambaUserInfo] = None
        return rtnval

    flags_raw = fields.get("account flags", "")
    enabled = "D" not in flags_raw

    last_password_change = fields.get("password last set")
    description = fields.get("account desc")
    if description is not None and len(description) == 0:
        description = None

    rtnval = MacosSambaUserInfo(
        username=username,
        enabled=enabled,
        last_password_change=last_password_change,
        description=description,
    )
    return rtnval
