"""
    macOS identity-management feature.

    Implements the cross-OS identity-management contract for macOS
    using ``dsconfigad`` for Active Directory bind / unbind / status,
    ``dscl`` for directory-services reads (and Open Directory bind
    inspection), and ``dscacheutil`` (plus a kick to ``mDNSResponder``)
    for cache invalidation.  Every verb is dispatched through the
    injected :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-------------------------------------+
    | Verb                        | macOS tool                          |
    +=============================+=====================================+
    | ``join_realm``              | ``dsconfigad -add <domain> -username|
    |                             | <admin> -password <pw> [-computer   |
    |                             | <name>] [-ou <ou>] [-force]``       |
    |                             | (password redacted in recorded argv)|
    +-----------------------------+-------------------------------------+
    | ``leave_realm``             | ``dsconfigad -remove -username      |
    |                             | <admin> -password <pw> -force``     |
    +-----------------------------+-------------------------------------+
    | ``get_realm_status``        | ``dsconfigad -show`` (parse)        |
    +-----------------------------+-------------------------------------+
    | ``list_joined_realms``      | ``dsconfigad -show`` — single-realm |
    |                             | reality on macOS; returns a list of |
    |                             | one when bound, empty otherwise     |
    +-----------------------------+-------------------------------------+
    | ``sync_users``              | ``dscacheutil -flushcache`` then    |
    |                             | ``killall -HUP mDNSResponder``      |
    +-----------------------------+-------------------------------------+
    | ``resolve_user``            | ``dscl /Search read /Users/<user>`` |
    +-----------------------------+-------------------------------------+
    | ``resolve_group``           | ``dscl /Search read /Groups/<grp>`` |
    +-----------------------------+-------------------------------------+

    Single-realm reality:
        macOS bind state is fundamentally single-realm.  The host can
        be bound to exactly one Active Directory at a time via
        ``dsconfigad`` plus any number of Open Directory nodes via
        ``dscl /Search``.  ``list_joined_realms`` therefore returns a
        list of zero or one :class:`MacosRealmInfo` records — never
        more — so callers that drive a multi-OS rollout can still
        iterate uniformly.

    File I/O policy:
        macOS bind state lives inside Directory Services
        (``/Library/Preferences/OpenDirectory/...``); reads and writes
        go through ``dsconfigad`` / ``dscl`` rather than parsing
        plists.  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``join_realm``, ``leave_realm``,
        ``sync_users``) honours ``config.elevated``.  Reads
        (``get_realm_status``, ``list_joined_realms``,
        ``resolve_user``, ``resolve_group``) never elevate.

    Password handling:
        ``dsconfigad`` accepts the password on argv via ``-password
        <pw>``.  The argv recorded on the result is sanitised: every
        password value is replaced with the literal ``"<redacted>"``
        so the password never leaks into audit captures.  This
        mirrors the redaction pattern established by
        :class:`MacosUserGroupManager`.
"""

import os

from dataclasses import dataclass, field
from enum import Enum
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

_DSCONFIGAD = "dsconfigad"
_DSCL = "dscl"
_DSCACHEUTIL = "dscacheutil"
_KILLALL = "killall"

# ``dscl /Search`` is the canonical search-path node; reading from it
# returns Open Directory + AD records merged.
_SEARCH_NODE = "/Search"

# Sentinel placed into the recorded argv anywhere a password value
# would otherwise appear.
_REDACTED = "<redacted>"


MacosIdentityActionStatus = ActionStatus


class MacosRealmKind(Enum):
    """
        Identity back-end kinds visible on macOS.

        :cvar ACTIVE_DIRECTORY: Microsoft Active Directory (bound via
                                ``dsconfigad``).
        :cvar OPEN_DIRECTORY: macOS Open Directory (bound via
                              ``dscl /Search`` augmentation).
        :cvar UNKNOWN: Bound to a directory whose kind cannot be
                       inferred from ``dsconfigad`` / ``dscl``
                       output.
    """
    ACTIVE_DIRECTORY = "active_directory"
    OPEN_DIRECTORY = "open_directory"
    UNKNOWN = "unknown"


@dataclass
class MacosIdentityManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosIdentityManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param dsconfigad_executable: Path / name of the
                                      ``dsconfigad`` binary.  Defaults
                                      to ``"dsconfigad"`` (PATH
                                      resolution).
        :param dscl_executable: Path / name of the ``dscl`` binary.
                                Defaults to ``"dscl"``.
        :param dscacheutil_executable: Path / name of the
                                       ``dscacheutil`` binary.  Defaults
                                       to ``"dscacheutil"``.
        :param killall_executable: Path / name of the ``killall``
                                   binary used to nudge
                                   ``mDNSResponder``.  Defaults to
                                   ``"killall"``.
        :param default_ou: Optional OU path used by :meth:`join_realm`
                           when the supplied
                           :class:`MacosRealmJoinSpec` does not carry
                           its own ``ou_path``.
        :param force: When True, ``join_realm`` and ``leave_realm``
                      append ``-force`` to the ``dsconfigad`` argv so
                      the bind / unbind proceeds non-interactively
                      even when the directory reports stale state.
    """
    dsconfigad_executable: str = _DSCONFIGAD
    dscl_executable: str = _DSCL
    dscacheutil_executable: str = _DSCACHEUTIL
    killall_executable: str = _KILLALL
    default_ou: Optional[str] = None
    force: bool = True


@dataclass
class MacosIdentityActionResult(FeatureActionResult):
    """
        Result of an identity-management write operation.

        :param realm: The realm acted upon, when relevant.
        :param username: The admin username acted upon, when relevant.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosRealmJoinSpec:
    """
        Specification for joining a realm via ``dsconfigad -add``.

        :param realm: The Active Directory domain (e.g.
                      ``"ad.example.com"``).
        :param admin_user: Administrator login used for the bind
                           (lands on argv via ``-username``).
        :param admin_password: Administrator password.  Lands on argv
                               via ``-password`` when the runner is
                               invoked, then redacted from the
                               recorded argv on the result.
        :param computer_name: Optional override for the host's
                              computer-account name (``-computer``).
        :param ou_path: Optional Organizational Unit (``-ou``).
        :param domain_controller: Optional DC override
                                  (``-preferred``).
    """
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class MacosRealmInfo:
    """
        Information about a realm the host is currently bound to.

        :param realm: Realm / domain name.
        :param realm_kind: Detected back-end kind.
        :param is_default: True for AD binds; True for the first
                           Open Directory node when no AD bind is
                           present.  macOS does not expose a
                           "default realm" concept independent of
                           bind order.
        :param domain_controller: ``dsconfigad`` ``Preferred Domain
                                  controller`` line, or ``None``.
        :param last_sync: Reserved.  ``dsconfigad`` does not surface
                          a last-sync timestamp; this field is always
                          ``None``.
    """
    realm: str
    realm_kind: MacosRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class MacosRealmStatus:
    """
        Aggregate realm-bind status of the host.

        :param joined: True when ``dsconfigad -show`` reports an
                       active bind.
        :param current_realm: Realm name when bound; ``None``
                              otherwise.
        :param realm_kind: Back-end kind when bound.
        :param computer_name: Host's computer account name as reported
                              by ``dsconfigad`` (``Computer Account``
                              line), or ``None``.
    """
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[MacosRealmKind] = None
    computer_name: Optional[str] = None


@dataclass
class MacosUserResolveResult:
    """
        Result of resolving a user against ``dscl /Search``.

        :param username: The supplied username.
        :param uid: ``UniqueID`` value, when resolvable.
        :param gid: ``PrimaryGroupID`` value, when resolvable.
        :param home: ``NFSHomeDirectory`` value, when resolvable.
        :param shell: ``UserShell`` value, when resolvable.
        :param realm: Realm the user comes from when the
                      ``OriginalNodeName`` line points at an AD
                      plug-in; ``None`` for local accounts.
        :param groups: Reserved.  ``dscl read /Users/<u>`` does not
                       enumerate supplementary groups; this list is
                       always empty.  Callers needing supplementary
                       groups should call :meth:`resolve_group` for
                       each known group name.
        :param found: True when ``dscl read`` returned a record.
    """
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class MacosGroupResolveResult:
    """
        Result of resolving a group against ``dscl /Search``.

        :param groupname: The supplied groupname.
        :param gid: ``PrimaryGroupID`` value, when resolvable.
        :param members: Names listed under the ``GroupMembership``
                        attribute.
        :param realm: Realm the group comes from, or ``None`` for
                      local.
        :param found: True when ``dscl read`` returned a record.
    """
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False


class MacosIdentityQueryError(Exception):
    """
        Raised when an identity-management read fails for a reason
        other than the target simply being absent.
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

        err_msg_lines = ["Identity-management query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# MacosIdentityManager
# ----------------------------------------------------------------------


class MacosIdentityManager(BaseFeatureManager):
    """
        macOS identity / realm manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.  Reads use
        ``dsconfigad -show`` and ``dscl /Search read``; writes use
        ``dsconfigad -add`` / ``-remove`` and
        ``dscacheutil -flushcache``.
    """

    result_class = MacosIdentityActionResult
    status_class = MacosIdentityActionStatus
    feature_name = "identity-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def join_realm(
        self,
        spec: MacosRealmJoinSpec,
    ) -> MacosIdentityActionResult:
        """
            Bind the host to ``spec.realm`` via ``dsconfigad -add``.

            The admin password lands on argv on the runner side but
            is replaced by ``"<redacted>"`` in the recorded argv on
            the result, so the password never leaks into audit
            captures.  Idempotent: when ``dsconfigad -show`` already
            reports the host bound to the requested realm, returns
            ``SKIPPED`` without invoking ``dsconfigad -add``.

            :param spec: The :class:`MacosRealmJoinSpec` describing
                         the bind.

            :returns: The :class:`MacosIdentityActionResult`.
        """
        full_command, recorded_command = self._build_join_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=MacosIdentityActionStatus.DRY_RUN,
                realm=spec.realm,
                username=spec.admin_user,
                command=recorded_command,
            )
            return dry

        if self._is_realm_joined(realm=spec.realm):
            skipped = self._build_realm_result(
                status=MacosIdentityActionStatus.SKIPPED,
                realm=spec.realm,
                username=spec.admin_user,
                command=recorded_command,
                message="host already bound to realm",
            )
            return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosIdentityActionStatus.SUCCESS
        else:
            mapped_status = MacosIdentityActionStatus.FAILED

        rtnval = MacosIdentityActionResult(
            status=mapped_status,
            detail=spec.realm,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            realm=spec.realm,
            username=spec.admin_user,
        )
        return rtnval

    def leave_realm(
        self,
        admin_user: str,
        admin_password: str,
        realm: Optional[str] = None,
    ) -> MacosIdentityActionResult:
        """
            Unbind the host from a realm via ``dsconfigad -remove``.

            macOS requires admin credentials for the unbind step in
            order to remove the computer account from the directory.
            ``leave_realm`` therefore accepts the credential pair
            explicitly rather than reusing a stored one — there is no
            stable way for the manager to retain the bind credential
            across the join / leave boundary.

            Idempotent: when ``dsconfigad -show`` reports no active
            bind, returns ``SKIPPED`` without invoking
            ``dsconfigad -remove``.

            :param admin_user: Administrator login (lands on argv via
                               ``-username``).
            :param admin_password: Administrator password (redacted
                                   from the recorded argv).
            :param realm: Optional realm guard.  When supplied and the
                          current bind does not match, returns
                          ``SKIPPED``.

            :returns: The :class:`MacosIdentityActionResult`.
        """
        full_command, recorded_command = self._build_leave_argv(
            admin_user=admin_user,
            admin_password=admin_password,
        )

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=MacosIdentityActionStatus.DRY_RUN,
                realm=realm,
                username=admin_user,
                command=recorded_command,
            )
            return dry

        current_status = self.get_realm_status()
        if not current_status.joined:
            skipped = self._build_realm_result(
                status=MacosIdentityActionStatus.SKIPPED,
                realm=realm,
                username=admin_user,
                command=recorded_command,
                message="host is not bound to any realm",
            )
            return skipped
        if realm is not None and current_status.current_realm is not None:
            if current_status.current_realm.lower() != realm.lower():
                skipped = self._build_realm_result(
                    status=MacosIdentityActionStatus.SKIPPED,
                    realm=realm,
                    username=admin_user,
                    command=recorded_command,
                    message="host is bound to a different realm",
                )
                return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosIdentityActionStatus.SUCCESS
        else:
            mapped_status = MacosIdentityActionStatus.FAILED

        rtnval = MacosIdentityActionResult(
            status=mapped_status,
            detail=realm,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            realm=realm,
            username=admin_user,
        )
        return rtnval

    def sync_users(
        self,
    ) -> MacosIdentityActionResult:
        """
            Flush the Directory Services cache via
            ``dscacheutil -flushcache`` followed by
            ``killall -HUP mDNSResponder`` so the next NSS lookup
            refreshes from the directory.

            Both invocations are best-effort: a non-zero
            ``dscacheutil`` exit causes ``mDNSResponder`` to be left
            alone, and the result is mapped to ``FAILED``.  Not
            idempotent — every call re-flushes.

            :returns: The :class:`MacosIdentityActionResult`.
        """
        config = self._typed_config()
        flush_command = [config.dscacheutil_executable, "-flushcache"]
        kick_command = [config.killall_executable, "-HUP", "mDNSResponder"]

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=MacosIdentityActionStatus.DRY_RUN,
                realm=None,
                username=None,
                command=flush_command,
            )
            return dry

        flush_run = self._runner.run(
            list(flush_command),
            elevated=self._config.elevated,
        )
        if flush_run.exit_code != 0:
            failed = MacosIdentityActionResult(
                status=MacosIdentityActionStatus.FAILED,
                detail=None,
                exit_code=flush_run.exit_code,
                stdout=flush_run.stdout,
                stderr=flush_run.stderr,
                command=list(flush_command),
                realm=None,
                username=None,
                message="dscacheutil -flushcache failed",
            )
            return failed

        kick_run = self._runner.run(
            list(kick_command),
            elevated=self._config.elevated,
        )
        if kick_run.exit_code == 0:
            mapped_status = MacosIdentityActionStatus.SUCCESS
        else:
            mapped_status = MacosIdentityActionStatus.FAILED

        rtnval = MacosIdentityActionResult(
            status=mapped_status,
            detail=None,
            exit_code=kick_run.exit_code,
            stdout=kick_run.stdout,
            stderr=kick_run.stderr,
            command=list(flush_command),
            realm=None,
            username=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_realm_status(self) -> MacosRealmStatus:
        """
            Return the host's current realm-bind status.

            Runs ``dsconfigad -show`` and parses the key/value output.
            When the host is not bound, ``dsconfigad`` exits zero with
            output reporting ``Active Directory Forest = -`` (or
            equivalent) — the parser maps that to
            ``MacosRealmStatus(joined=False)``.  Reads do not elevate.

            :returns: The :class:`MacosRealmStatus`.
            :raises MacosIdentityQueryError: When ``dsconfigad -show``
                                             exits non-zero.
        """
        record = self._dsconfigad_show()
        if record is None:
            rtnval = MacosRealmStatus(joined=False)
            return rtnval

        rtnval = _materialise_realm_status(record=record)
        return rtnval

    def list_joined_realms(self) -> list[MacosRealmInfo]:
        """
            Enumerate the realms the host is currently bound to.

            macOS supports a single AD bind plus arbitrary Open
            Directory nodes; this implementation reports the AD bind
            (when present) as a single :class:`MacosRealmInfo`
            entry.  Open Directory binds are not enumerated by this
            method — callers needing OD node listing should call
            ``dscl /Search -read /``.

            Reads do not elevate.

            :returns: Zero or one :class:`MacosRealmInfo` records.
            :raises MacosIdentityQueryError: When ``dsconfigad -show``
                                             exits non-zero.
        """
        status = self.get_realm_status()
        if not status.joined:
            rtnval: list[MacosRealmInfo] = []
            return rtnval
        if status.current_realm is None:
            rtnval = []
            return rtnval

        info = MacosRealmInfo(
            realm=status.current_realm,
            realm_kind=status.realm_kind or MacosRealmKind.UNKNOWN,
            is_default=True,
            domain_controller=None,
            last_sync=None,
        )
        rtnval = [info]
        return rtnval

    def resolve_user(
        self,
        username: str,
    ) -> MacosUserResolveResult:
        """
            Resolve a username through ``dscl /Search read
            /Users/<username>``.  Reads do not elevate.

            :param username: The username to resolve.

            :returns: The :class:`MacosUserResolveResult`.  When the
                      user does not exist, ``found=False``.
        """
        config = self._typed_config()
        command = [
            config.dscl_executable,
            _SEARCH_NODE,
            "-read",
            "/Users/{}".format(username),
        ]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = MacosUserResolveResult(
                username=username,
                found=False,
            )
            return rtnval

        attributes = _parse_dscl_read_output(output=run_result.stdout)
        rtnval = _materialise_user_resolve(
            username=username,
            attributes=attributes,
        )
        return rtnval

    def resolve_group(
        self,
        groupname: str,
    ) -> MacosGroupResolveResult:
        """
            Resolve a groupname through ``dscl /Search read
            /Groups/<groupname>``.  Reads do not elevate.

            :param groupname: The groupname to resolve.

            :returns: The :class:`MacosGroupResolveResult`.  When the
                      group does not exist, ``found=False``.
        """
        config = self._typed_config()
        command = [
            config.dscl_executable,
            _SEARCH_NODE,
            "-read",
            "/Groups/{}".format(groupname),
        ]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = MacosGroupResolveResult(
                groupname=groupname,
                found=False,
            )
            return rtnval

        attributes = _parse_dscl_read_output(output=run_result.stdout)
        rtnval = _materialise_group_resolve(
            groupname=groupname,
            attributes=attributes,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv builders
    # ------------------------------------------------------------------

    def _build_join_argv(
        self,
        *,
        spec: MacosRealmJoinSpec,
    ) -> tuple[list[str], list[str]]:
        """
            Build the ``dsconfigad -add`` argv for a join.

            :param spec: The :class:`MacosRealmJoinSpec`.

            :returns: ``(full_command, recorded_command)`` where the
                      recorded variant has every password value
                      replaced with ``"<redacted>"``.
        """
        config = self._typed_config()
        command: list[str] = [config.dsconfigad_executable]
        command.append("-add")
        command.append(spec.realm)
        command.append("-username")
        command.append(spec.admin_user)
        command.append("-password")
        command.append(spec.admin_password)

        if spec.computer_name is not None:
            command.append("-computer")
            command.append(spec.computer_name)

        ou_value = spec.ou_path
        if ou_value is None:
            ou_value = config.default_ou
        if ou_value is not None:
            command.append("-ou")
            command.append(ou_value)

        if spec.domain_controller is not None:
            command.append("-preferred")
            command.append(spec.domain_controller)

        if config.force:
            command.append("-force")

        recorded = _sanitise_argv_passwords(argv=command)
        rtnval = (command, recorded)
        return rtnval

    def _build_leave_argv(
        self,
        *,
        admin_user: str,
        admin_password: str,
    ) -> tuple[list[str], list[str]]:
        """
            Build the ``dsconfigad -remove`` argv for an unbind.

            :param admin_user: Administrator login.
            :param admin_password: Administrator password.

            :returns: ``(full_command, recorded_command)`` tuple.
        """
        config = self._typed_config()
        command: list[str] = [config.dsconfigad_executable]
        command.append("-remove")
        command.append("-username")
        command.append(admin_user)
        command.append("-password")
        command.append(admin_password)

        if config.force:
            command.append("-force")

        recorded = _sanitise_argv_passwords(argv=command)
        rtnval = (command, recorded)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — dispatch + result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosIdentityManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosIdentityManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, MacosIdentityManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosIdentityManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_realm_result(
        self,
        *,
        status: MacosIdentityActionStatus,
        realm: Optional[str],
        username: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosIdentityActionResult:
        """
            Construct a :class:`MacosIdentityActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param realm: The realm acted upon, when relevant.
            :param username: The admin username, when relevant.
            :param command: The argv to record (already sanitised).
            :param message: Optional human-readable detail.
            :param stderr: Optional captured stderr.
        """
        rtnval = MacosIdentityActionResult(
            status=status,
            detail=realm,
            command=list(command),
            realm=realm,
            username=username,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read probes
    # ------------------------------------------------------------------

    def _is_realm_joined(
        self,
        *,
        realm: str,
    ) -> bool:
        """
            Probe ``dsconfigad -show`` and check whether the parsed
            realm matches ``realm`` (case-insensitive).

            :param realm: The realm of interest.

            :returns: True when bound to ``realm``.  Query failures
                      collapse to False so the caller's bind attempt
                      proceeds normally.
        """
        try:
            status = self.get_realm_status()
        except MacosIdentityQueryError:
            rtnval = False
            return rtnval

        if not status.joined:
            rtnval = False
            return rtnval
        if status.current_realm is None:
            rtnval = False
            return rtnval

        rtnval = status.current_realm.lower() == realm.lower()
        return rtnval

    def _dsconfigad_show(self) -> Optional[dict]:
        """
            Run ``dsconfigad -show`` and parse the result into a flat
            attribute dictionary.

            :returns: The parsed dict, or ``None`` when the host is
                      not bound (parser returns an empty / sentinel
                      shape).
            :raises MacosIdentityQueryError: When ``dsconfigad -show``
                                             exits non-zero.
        """
        config = self._typed_config()
        command = [config.dsconfigad_executable, "-show"]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosIdentityQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_dsconfigad_show(output=run_result.stdout)
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — argv sanitisation
# ----------------------------------------------------------------------


def _sanitise_argv_passwords(*, argv: list[str]) -> list[str]:
    """
        Return a copy of ``argv`` with the value following any
        ``-password`` flag replaced by ``"<redacted>"``.

        :param argv: The argv to sanitise.

        :returns: A new list (the original is left intact).
    """
    password_flags = {
        "-password",
        "-newpassword",
        "-localpassword",
    }
    rtnval: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            rtnval.append(_REDACTED)
            redact_next = False
            continue
        rtnval.append(token)
        # Match case-insensitively to be safe with mixed-case flags.
        if token.lower() in password_flags:
            redact_next = True
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — parsers
# ----------------------------------------------------------------------


def _parse_dsconfigad_show(*, output: str) -> Optional[dict]:
    """
        Parse the key/value output of ``dsconfigad -show`` into a flat
        attribute dictionary.

        ``dsconfigad -show`` emits one ``Key = Value`` line per
        attribute.  The "not bound" state surfaces as
        ``Active Directory Forest = -`` and similar dash placeholders.

        :param output: Raw stdout text.

        :returns: ``dict[str, str]`` of parsed attributes, or ``None``
                  when the output is empty.
    """
    rtnval: dict[str, str] = {}
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if len(cleaned_key) == 0:
            continue
        rtnval[cleaned_key] = cleaned_value

    if len(rtnval) == 0:
        rtnval_optional: Optional[dict] = None
        return rtnval_optional

    return rtnval


def _materialise_realm_status(*, record: dict) -> MacosRealmStatus:
    """
        Convert a parsed ``dsconfigad -show`` attribute dict into a
        :class:`MacosRealmStatus`.

        :param record: Parsed attribute map.

        :returns: The :class:`MacosRealmStatus`.
    """
    forest_value = record.get("Active Directory Forest", "").strip()
    domain_value = record.get("Active Directory Domain", "").strip()
    computer_value = record.get("Computer Account", "").strip()

    if (
        len(forest_value) == 0 or forest_value == "-"
    ) and (
        len(domain_value) == 0 or domain_value == "-"
    ):
        rtnval = MacosRealmStatus(joined=False)
        return rtnval

    if len(domain_value) > 0 and domain_value != "-":
        realm_value = domain_value
    else:
        realm_value = forest_value

    if len(computer_value) == 0 or computer_value == "-":
        computer_name_value: Optional[str] = None
    else:
        computer_name_value = computer_value

    rtnval = MacosRealmStatus(
        joined=True,
        current_realm=realm_value,
        realm_kind=MacosRealmKind.ACTIVE_DIRECTORY,
        computer_name=computer_name_value,
    )
    return rtnval


def _parse_dscl_read_output(*, output: str) -> dict:
    """
        Parse ``dscl /Search -read /Users/<u>`` (or
        ``/Groups/<g>``) output into a flat ``dict[str, str]``.

        ``dscl -read`` emits ``Attribute: value`` lines.  Continuation
        lines are indented with whitespace and join to the previous
        attribute's value with single-space separators.

        :param output: Raw stdout text.

        :returns: ``dict[str, str]`` keyed by attribute name.
    """
    rtnval: dict[str, str] = {}
    current_key: Optional[str] = None
    for raw_line in output.splitlines():
        if len(raw_line.strip()) == 0:
            continue
        if raw_line[0] == " " or raw_line[0] == "\t":
            if current_key is None:
                continue
            extra = raw_line.strip()
            if len(extra) == 0:
                continue
            existing = rtnval.get(current_key, "")
            if len(existing) == 0:
                rtnval[current_key] = extra
            else:
                rtnval[current_key] = existing + " " + extra
            continue
        if ":" not in raw_line:
            continue
        key, _, value = raw_line.partition(":")
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if len(cleaned_key) == 0:
            continue
        rtnval[cleaned_key] = cleaned_value
        current_key = cleaned_key
    return rtnval


def _materialise_user_resolve(
    *,
    username: str,
    attributes: dict,
) -> MacosUserResolveResult:
    """
        Convert a parsed ``dscl read /Users/<u>`` attribute dict into
        a :class:`MacosUserResolveResult`.

        :param username: The supplied username.
        :param attributes: Parsed attribute map.

        :returns: The :class:`MacosUserResolveResult`.  When the
                  payload lacks essential fields, ``found`` is still
                  True (the record was returned) but the absent
                  fields collapse to ``None``.
    """
    if len(attributes) == 0:
        rtnval = MacosUserResolveResult(username=username, found=False)
        return rtnval

    uid_value = _coerce_optional_int(
        raw_value=attributes.get("UniqueID", ""),
    )
    gid_value = _coerce_optional_int(
        raw_value=attributes.get("PrimaryGroupID", ""),
    )

    home_raw = attributes.get("NFSHomeDirectory", "").strip()
    if len(home_raw) == 0:
        home_value: Optional[str] = None
    else:
        home_value = home_raw

    shell_raw = attributes.get("UserShell", "").strip()
    if len(shell_raw) == 0:
        shell_value: Optional[str] = None
    else:
        shell_value = shell_raw

    realm_value = _detect_realm_from_node(
        node=attributes.get("OriginalNodeName", ""),
    )

    rtnval = MacosUserResolveResult(
        username=username,
        uid=uid_value,
        gid=gid_value,
        home=home_value,
        shell=shell_value,
        realm=realm_value,
        groups=[],
        found=True,
    )
    return rtnval


def _materialise_group_resolve(
    *,
    groupname: str,
    attributes: dict,
) -> MacosGroupResolveResult:
    """
        Convert a parsed ``dscl read /Groups/<g>`` attribute dict
        into a :class:`MacosGroupResolveResult`.

        :param groupname: The supplied groupname.
        :param attributes: Parsed attribute map.

        :returns: The :class:`MacosGroupResolveResult`.
    """
    if len(attributes) == 0:
        rtnval = MacosGroupResolveResult(
            groupname=groupname,
            found=False,
        )
        return rtnval

    gid_value = _coerce_optional_int(
        raw_value=attributes.get("PrimaryGroupID", ""),
    )

    membership_raw = attributes.get("GroupMembership", "").strip()
    members: list[str] = []
    if len(membership_raw) > 0:
        for token in membership_raw.split():
            cleaned = token.strip()
            if len(cleaned) > 0:
                members.append(cleaned)

    realm_value = _detect_realm_from_node(
        node=attributes.get("OriginalNodeName", ""),
    )

    rtnval = MacosGroupResolveResult(
        groupname=groupname,
        gid=gid_value,
        members=members,
        realm=realm_value,
        found=True,
    )
    return rtnval


def _coerce_optional_int(*, raw_value: str) -> Optional[int]:
    """
        Coerce a raw attribute string to an int, returning ``None``
        on any failure.

        :param raw_value: The raw attribute string.

        :returns: The integer, or ``None``.
    """
    cleaned = raw_value.strip()
    if len(cleaned) == 0:
        rtnval: Optional[int] = None
        return rtnval
    try:
        rtnval = int(cleaned)
    except ValueError:
        rtnval = None
    return rtnval


def _detect_realm_from_node(*, node: str) -> Optional[str]:
    """
        Pull a realm name from an ``OriginalNodeName`` attribute.

        ``dscl`` reports the source node for AD records as something
        like ``/Active Directory/EXAMPLE/All Domains``.  Local
        records report ``/Local/Default``.

        :param node: The raw ``OriginalNodeName`` string.

        :returns: The realm name when the node points at an AD plug-
                  in; ``None`` for local accounts.
    """
    cleaned = node.strip()
    if len(cleaned) == 0:
        rtnval: Optional[str] = None
        return rtnval

    lowered = cleaned.lower()
    if "/active directory/" not in lowered:
        rtnval = None
        return rtnval

    parts = cleaned.split("/")
    # Expected form: "/Active Directory/<DOMAIN>/All Domains" — the
    # third slash-separated part is the AD domain.
    if len(parts) < 3:
        rtnval = None
        return rtnval

    candidate = parts[2].strip()
    if len(candidate) == 0:
        rtnval = None
        return rtnval

    rtnval = candidate
    return rtnval
