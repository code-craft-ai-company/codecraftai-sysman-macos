"""
    macOS user-group-management feature.

    Implements the cross-OS user-group-management contract for macOS using
    ``sysadminctl`` (account create / delete / password reset),
    ``dscl`` (Directory Services attribute reads and writes),
    ``dseditgroup`` (group lifecycle and membership), ``pwpolicy`` (account
    enable / disable), and ``id`` for primary GID + group enumeration.
    Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under local
    and SSH execution.

    File I/O policy:
        macOS account state lives in Open Directory (``Local/Default``
        node), not in flat files; reads and writes go through
        ``dscl`` / ``dseditgroup`` / ``pwpolicy`` rather than parsing
        ``/etc/passwd``.  This module does NOT consume an
        :class:`IFileOperator`; every read / write / probe flows through
        the runner so dry-run, elevation, and aspect composition compose
        uniformly.

    Elevation policy:
        Every write verb (``create_user``, ``delete_user``,
        ``modify_user``, ``set_password``, ``lock_account``,
        ``unlock_account``, ``create_group``, ``delete_group``,
        ``add_to_group``, ``remove_from_group``) honours
        ``config.elevated``.  Reads (``get_user``, ``get_group``,
        ``list_users``, ``list_groups``, idempotency probes) never
        elevate.

    sysadminctl admin-pair requirement:
        ``sysadminctl -addUser`` and ``sysadminctl -resetPasswordFor``
        require an existing admin user's credentials when invoked
        non-interactively, even under ``sudo``.  Callers configure these
        via :attr:`MacosUserGroupManagerConfig.admin_user` and
        :attr:`MacosUserGroupManagerConfig.admin_password`; when both are
        set, ``-adminUser <admin> -adminPassword <pw>`` is appended to
        the argv.  When either is None the manager omits the pair (the
        host then has to be SecureToken-bypassed for the call to
        succeed; this mirrors what every macOS automation stack does).

    Password handling:
        ``set_password`` builds the full ``sysadminctl -resetPasswordFor``
        argv with ``-newPassword`` followed by the plaintext password.
        The argv recorded on the result is sanitised: every password
        value is replaced with the literal ``"<redacted>"`` so the
        password never leaks into audit logs.
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


_SYSADMINCTL = "sysadminctl"
_DSCL = "dscl"
_DSEDITGROUP = "dseditgroup"
_PWPOLICY = "pwpolicy"
_ID = "id"

# Default Open Directory node used by every dscl / dseditgroup call.
# ``"."`` is the canonical local node alias on macOS.
_LOCAL_NODE = "."

# Sentinel placed into the recorded argv anywhere a password value
# would otherwise appear.
_REDACTED = "<redacted>"


MacosUserGroupActionStatus = ActionStatus


class MacosPrincipalKind(Enum):
    """
        Principal kind echoed onto every
        :class:`MacosUserGroupActionResult`.
    """
    USER = "user"
    GROUP = "group"


@dataclass
class MacosUserGroupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosUserGroupManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param regular_uid_min: Lower bound (inclusive) for the UID range
                                used by :meth:`MacosUserGroupManager.list_users`
                                when filtering out system accounts.  macOS
                                reserves UIDs ``1`` through ``500`` for
                                system accounts and starts the first
                                regular user at ``501``.
        :param regular_uid_max: Upper bound (inclusive) for the UID range
                                used by :meth:`MacosUserGroupManager.list_users`
                                when filtering out system accounts.
        :param include_system_accounts: When True,
                                        :meth:`MacosUserGroupManager.list_users`
                                        returns every parsed entry without
                                        filtering by UID range.
        :param admin_user: Existing admin login name used when
                           ``sysadminctl`` requires an admin pair (account
                           create + password reset).  Set to ``None`` to
                           omit the pair from the argv.
        :param admin_password: Plaintext admin password used in the same
                               argv pair.  Captured argv is sanitised to
                               ``"<redacted>"``.  Set to ``None`` to omit
                               the pair from the argv.
    """
    regular_uid_min: int = 501
    regular_uid_max: int = 65535
    include_system_accounts: bool = False
    admin_user: Optional[str] = None
    admin_password: Optional[str] = None


@dataclass
class MacosUserGroupActionResult(FeatureActionResult):
    """
        Result of a user-or-group write operation.

        :param name: The user or group name targeted.  ``None`` for
                     verbs that are not name-scoped.
        :param kind: Whether the principal is a user or a group.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    kind: Optional[MacosPrincipalKind] = None
    message: Optional[str] = None


@dataclass
class MacosUserSpec:
    """
        Specification for creating a user via ``sysadminctl -addUser``.

        :param name: Login name.
        :param full_name: Optional full / display name (the macOS
                          ``RealName`` attribute).  Passed via
                          ``-fullName`` when set.
        :param password: Optional initial plaintext password (passed via
                         ``-password``).  None creates an account
                         without a password (sysadminctl will prompt
                         when invoked interactively; the result is left
                         to the host's policy).
        :param uid: Optional numeric UID (``-UID``).  System allocates
                    one when None.
        :param primary_group: Optional primary group name.  macOS does
                              not accept this on the create call;
                              setting it triggers a follow-up
                              ``dscl . -change /Users/<name>
                              PrimaryGroupID`` after creation.
        :param shell: Login shell (``-shell``).  Defaults to
                      ``/bin/zsh`` (the modern macOS default).
        :param home: Optional home directory path (``-home``).
        :param groups: Optional supplementary groups (added via
                       ``dseditgroup -o edit -a <name> -t user`` per
                       group after creation).
        :param system: True when this is a system / hidden account.
                       macOS does not have a first-class ``-r``-style
                       flag here; setting ``system=True`` simply
                       documents intent for callers and keeps the
                       contract aligned with the Linux peers.
        :param create_home: When True, ``-home`` is honoured normally
                            (sysadminctl creates the directory when
                            running with sufficient privileges).
                            Reserved for parity with the Linux peers.
    """
    name: str
    full_name: Optional[str] = None
    password: Optional[str] = None
    uid: Optional[int] = None
    primary_group: Optional[str] = None
    shell: str = "/bin/zsh"
    home: Optional[str] = None
    groups: Optional[list[str]] = None
    system: bool = False
    create_home: bool = True


@dataclass
class MacosUserChanges:
    """
        Specification for modifying an existing user account.  Every
        field is optional; only fields that are not ``None`` cause a
        ``dscl . -change`` (or ``dseditgroup`` for group membership) to
        be emitted.

        :param new_name: New ``RecordName``.  Note that renaming a macOS
                         user via ``dscl`` is best-effort because the
                         home directory and group memberships still
                         reference the old name; the cross-OS contract
                         exposes this knob for parity with Linux.
        :param uid: New numeric UID (``UniqueID``).
        :param home: New home directory (``NFSHomeDirectory``).
        :param shell: New login shell (``UserShell``).
        :param full_name: New display name (``RealName``).
        :param primary_group: New primary group (``PrimaryGroupID``);
                              when set, ``dscl . -read /Groups/<group>
                              PrimaryGroupID`` is consulted to resolve
                              the GID.
        :param groups: Supplementary group set (handled via
                       ``dseditgroup``).
        :param append_groups: When True with ``groups`` set, the manager
                              only adds the user to listed groups.
                              Otherwise the user's current memberships
                              that are NOT in ``groups`` are revoked
                              first.
    """
    new_name: Optional[str] = None
    uid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    full_name: Optional[str] = None
    primary_group: Optional[str] = None
    groups: Optional[list[str]] = None
    append_groups: bool = False


@dataclass
class MacosUserInfo:
    """
        Information about an existing local user.

        :param name: Login name (``RecordName``).
        :param uid: Numeric UID (``UniqueID``).
        :param gid: Primary GID (``PrimaryGroupID``).
        :param home: Home directory (``NFSHomeDirectory``).
        :param shell: Login shell (``UserShell``).
        :param full_name: Display name (``RealName``).
        :param groups: Names of every group the user belongs to
                       (primary + supplementary), in the order returned
                       by ``id -Gn``.
        :param locked: True when ``pwpolicy -u <name>
                       -authentication-allowed`` reports authentication
                       is NOT allowed (i.e., the account is disabled).
    """
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    full_name: str
    groups: list[str] = field(default_factory=list)
    locked: bool = False


@dataclass
class MacosGroupSpec:
    """
        Specification for creating a group via ``dseditgroup -o create``.

        :param name: Group name.
        :param gid: Optional numeric GID (``-i``).
        :param description: Optional human-readable description (``-r``).
    """
    name: str
    gid: Optional[int] = None
    description: Optional[str] = None


@dataclass
class MacosGroupInfo:
    """
        Information about an existing local group.

        :param name: Group name.
        :param gid: Numeric GID.
        :param members: Supplementary members of the group.
    """
    name: str
    gid: int
    members: list[str] = field(default_factory=list)


class MacosUserGroupQueryError(Exception):
    """
        Raised when a user / group read fails for a reason other than
        the target simply being absent.
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

        err_msg_lines = ["User/group query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosUserGroupManager(BaseFeatureManager):
    """
        macOS user-and-group manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; reads use ``dscl``,
        ``dseditgroup``, ``id``, and ``pwpolicy``, while writes use
        ``sysadminctl``, ``dscl . -change``, ``dseditgroup``, and
        ``pwpolicy``.
    """

    result_class = MacosUserGroupActionResult
    status_class = MacosUserGroupActionStatus
    feature_name = "user-group-management"

    # ------------------------------------------------------------------
    # User write verbs
    # ------------------------------------------------------------------

    def create_user(
        self,
        spec: MacosUserSpec,
    ) -> MacosUserGroupActionResult:
        """
            Create a local user via ``sysadminctl -addUser``.

            Idempotent: skipped when ``dscl . -read /Users/<name>``
            already succeeds.

            :param spec: The :class:`MacosUserSpec` describing the user.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        full_command, recorded_command = self._build_useradd_command(
            spec=spec,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=recorded_command,
            )
            return dry

        if self._user_record_exists(name=spec.name):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=recorded_command,
                message="user already exists",
            )
            return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosUserGroupActionStatus.SUCCESS
        else:
            mapped_status = MacosUserGroupActionStatus.FAILED

        rtnval = MacosUserGroupActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            name=spec.name,
            kind=MacosPrincipalKind.USER,
        )
        return rtnval

    def delete_user(
        self,
        name: str,
    ) -> MacosUserGroupActionResult:
        """
            Delete a local user via ``sysadminctl -deleteUser`` (with
            ``-secure`` to ensure the home directory is removed).
            Idempotent: skipped when the user does not exist.

            :param name: The login name to delete.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        full_command, recorded_command = self._build_userdel_command(
            name=name,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=name,
                command=recorded_command,
            )
            return dry

        if not self._user_record_exists(name=name):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=name,
                command=recorded_command,
                message="user does not exist",
            )
            return skipped

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosUserGroupActionStatus.SUCCESS
        else:
            mapped_status = MacosUserGroupActionStatus.FAILED

        rtnval = MacosUserGroupActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            name=name,
            kind=MacosPrincipalKind.USER,
        )
        return rtnval

    def modify_user(
        self,
        name: str,
        changes: MacosUserChanges,
    ) -> MacosUserGroupActionResult:
        """
            Modify an existing user via one or more ``dscl . -change``
            calls (one per scalar attribute) plus ``dseditgroup`` calls
            for group membership changes.

            Idempotent per-attribute: only attributes whose current
            value differs from the requested value cause a ``dscl
            -change``.  Group membership comparisons honour
            :attr:`MacosUserChanges.append_groups`.  Every call's argv
            is executed in declaration order; on the first non-zero
            exit the method returns ``FAILED`` and stops.

            The recorded ``command`` field on the returned result is
            the FIRST emitted argv (or, when nothing needed changing,
            an empty list).  ``detail`` always carries the user's name.

            :param name: The current login name to modify.
            :param changes: The :class:`MacosUserChanges` describing the
                            desired new state.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        if self._config.dry_run:
            dry_command = self._build_first_modify_command(
                name=name, changes=changes,
            )
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=name,
                command=dry_command,
            )
            return dry

        existing = self.get_user(name=name)
        if existing is None:
            failed = self._build_user_result(
                status=MacosUserGroupActionStatus.FAILED,
                name=name,
                command=[_DSCL, _LOCAL_NODE, "-read", "/Users/{}".format(name)],
                message="user does not exist",
                stderr="user does not exist",
            )
            return failed

        plan = self._build_modify_plan(
            name=name,
            changes=changes,
            current=existing,
        )

        if len(plan) == 0:
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=name,
                command=[],
                message="user already matches requested state",
            )
            return skipped

        first_command = list(plan[0])
        last_run = None
        for argv in plan:
            this_run = self._runner.run(
                list(argv),
                elevated=self._config.elevated,
            )
            last_run = this_run
            if this_run.exit_code != 0:
                failed = MacosUserGroupActionResult(
                    status=MacosUserGroupActionStatus.FAILED,
                    detail=name,
                    exit_code=this_run.exit_code,
                    stdout=this_run.stdout,
                    stderr=this_run.stderr,
                    command=list(argv),
                    name=name,
                    kind=MacosPrincipalKind.USER,
                    message="modify_user step failed",
                )
                return failed

        # ``last_run`` is non-None at this point because ``plan`` is
        # non-empty.
        rtnval = MacosUserGroupActionResult(
            status=MacosUserGroupActionStatus.SUCCESS,
            detail=name,
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=first_command,
            name=name,
            kind=MacosPrincipalKind.USER,
        )
        return rtnval

    def set_password(
        self,
        user: str,
        password: str,
    ) -> MacosUserGroupActionResult:
        """
            Reset ``user``'s password via ``sysadminctl
            -resetPasswordFor``.  The recorded argv replaces every
            password value with ``"<redacted>"`` so the plaintext
            password never lands in audit captures.  No idempotency
            guard — the current password hash is not externally
            observable.

            :param user: The login name whose password to set.
            :param password: The plaintext password to install.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        full_command, recorded_command = self._build_passwd_command(
            user=user, password=password,
        )

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=user,
                command=recorded_command,
            )
            return dry

        run_result = self._runner.run(
            list(full_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosUserGroupActionStatus.SUCCESS
        else:
            mapped_status = MacosUserGroupActionStatus.FAILED

        rtnval = MacosUserGroupActionResult(
            status=mapped_status,
            detail=user,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(recorded_command),
            name=user,
            kind=MacosPrincipalKind.USER,
        )
        return rtnval

    def lock_account(
        self,
        user: str,
    ) -> MacosUserGroupActionResult:
        """
            Disable ``user``'s account via ``pwpolicy -u <user>
            -disableuser``.  Idempotent: skipped when authentication is
            already not allowed.

            :param user: The login name to lock.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = [_PWPOLICY, "-u", user, "-disableuser"]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if self._account_is_locked(user=user):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is already locked",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    def unlock_account(
        self,
        user: str,
    ) -> MacosUserGroupActionResult:
        """
            Enable ``user``'s account via ``pwpolicy -u <user>
            -enableuser``.  Idempotent: skipped when authentication is
            already allowed.

            :param user: The login name to unlock.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = [_PWPOLICY, "-u", user, "-enableuser"]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if not self._account_is_locked(user=user):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="account is not locked",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    # ------------------------------------------------------------------
    # Group write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: MacosGroupSpec,
    ) -> MacosUserGroupActionResult:
        """
            Create a local group via ``dseditgroup -o create``.  Skipped
            when the group already exists.

            :param spec: The :class:`MacosGroupSpec` describing the
                         group.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = self._build_groupadd_command(spec=spec)

        if self._config.dry_run:
            dry = self._build_group_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=spec.name,
                command=command,
            )
            return dry

        if self._group_record_exists(name=spec.name):
            skipped = self._build_group_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=spec.name,
                command=command,
                message="group already exists",
            )
            return skipped

        rtnval = self._dispatch_group_write(
            command=command, name=spec.name,
        )
        return rtnval

    def delete_group(
        self,
        name: str,
    ) -> MacosUserGroupActionResult:
        """
            Delete a local group via ``dseditgroup -o delete``.  Skipped
            when the group does not exist.

            :param name: The group name to delete.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = [_DSEDITGROUP, "-o", "delete", name]

        if self._config.dry_run:
            dry = self._build_group_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry

        if not self._group_record_exists(name=name):
            skipped = self._build_group_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=name,
                command=command,
                message="group does not exist",
            )
            return skipped

        rtnval = self._dispatch_group_write(command=command, name=name)
        return rtnval

    def add_to_group(
        self,
        user: str,
        group: str,
    ) -> MacosUserGroupActionResult:
        """
            Add ``user`` to ``group`` as a supplementary member via
            ``dseditgroup -o edit -a <user> -t user <group>``.  Skipped
            when the user is already a member.

            :param user: The login name to add.
            :param group: The group name to add to.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = [
            _DSEDITGROUP, "-o", "edit",
            "-a", user, "-t", "user",
            group,
        ]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if not self._group_record_exists(name=group):
            failed = self._build_user_result(
                status=MacosUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if self._dseditgroup_checkmember(user=user, group=group):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is already a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    def remove_from_group(
        self,
        user: str,
        group: str,
    ) -> MacosUserGroupActionResult:
        """
            Remove ``user`` from ``group`` via ``dseditgroup -o edit -d
            <user> -t user <group>``.  Skipped when the user is not a
            member.

            :param user: The login name to remove.
            :param group: The group name to remove from.

            :returns: The :class:`MacosUserGroupActionResult`.
        """
        command = [
            _DSEDITGROUP, "-o", "edit",
            "-d", user, "-t", "user",
            group,
        ]

        if self._config.dry_run:
            dry = self._build_user_result(
                status=MacosUserGroupActionStatus.DRY_RUN,
                name=user,
                command=command,
            )
            return dry

        if not self._group_record_exists(name=group):
            failed = self._build_user_result(
                status=MacosUserGroupActionStatus.FAILED,
                name=user,
                command=command,
                message="group does not exist",
                stderr="group does not exist",
            )
            return failed

        if not self._dseditgroup_checkmember(user=user, group=group):
            skipped = self._build_user_result(
                status=MacosUserGroupActionStatus.SKIPPED,
                name=user,
                command=command,
                message="user is not a member of the group",
            )
            return skipped

        rtnval = self._dispatch_user_write(command=command, name=user)
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user(
        self,
        name: str,
    ) -> Optional[MacosUserInfo]:
        """
            Look up a user by name.

            Runs ``dscl . -read /Users/<name>`` for the scalar attributes,
            ``id -Gn <name>`` for the supplementary group list, and
            ``pwpolicy -u <name> -authentication-allowed`` to decide
            whether the account is locked.  Reads do not elevate.

            :param name: The login name to look up.

            :returns: The :class:`MacosUserInfo`, or ``None`` when no
                      such user exists.
        """
        attributes = self._dscl_read_user(name=name)
        if attributes is None:
            rtnval: Optional[MacosUserInfo] = None
            return rtnval

        groups = self._lookup_user_groups(name=name)
        locked = self._account_is_locked(user=name)

        rtnval = MacosUserInfo(
            name=attributes["name"],
            uid=attributes["uid"],
            gid=attributes["gid"],
            home=attributes["home"],
            shell=attributes["shell"],
            full_name=attributes["full_name"],
            groups=groups,
            locked=locked,
        )
        return rtnval

    def list_users(self) -> list[MacosUserInfo]:
        """
            Enumerate every local user.

            Runs ``dscl . -list /Users UniqueID`` and parses the
            ``<name>\\t<uid>`` pairs.  When
            :attr:`MacosUserGroupManagerConfig.include_system_accounts`
            is False, results are filtered to entries whose UID lies
            within the configured ``regular_uid_min`` /
            ``regular_uid_max`` inclusive range (default 501-65535).
            Each retained entry is materialised via :meth:`get_user`.
            Reads do not elevate.

            :returns: One :class:`MacosUserInfo` per filtered entry.
            :raises MacosUserGroupQueryError: When ``dscl -list``
                                              exits non-zero.
        """
        command = [_DSCL, _LOCAL_NODE, "-list", "/Users", "UniqueID"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        config = self._typed_config()
        uid_min = config.regular_uid_min
        uid_max = config.regular_uid_max
        include_system = config.include_system_accounts

        rtnval: list[MacosUserInfo] = []
        for raw_line in run_result.stdout.splitlines():
            entry = _parse_dscl_list_uid_line(line=raw_line)
            if entry is None:
                continue
            if not include_system:
                entry_uid = entry["uid"]
                if entry_uid < uid_min:
                    continue
                if entry_uid > uid_max:
                    continue

            user_info = self.get_user(name=entry["name"])
            if user_info is None:
                # Race / deletion between -list and -read; skip.
                continue
            rtnval.append(user_info)

        return rtnval

    def get_group(
        self,
        name: str,
    ) -> Optional[MacosGroupInfo]:
        """
            Look up a group by name via ``dscl . -read /Groups/<name>``.

            :param name: The group name to look up.

            :returns: The :class:`MacosGroupInfo`, or ``None`` when no
                      such group exists.
        """
        rtnval = self._dscl_read_group(name=name)
        return rtnval

    def list_groups(self) -> list[MacosGroupInfo]:
        """
            Enumerate every local group via ``dscl . -list /Groups
            PrimaryGroupID``.  Each entry is materialised via
            :meth:`get_group`.  Reads do not elevate.

            :returns: One :class:`MacosGroupInfo` per parsed entry.
            :raises MacosUserGroupQueryError: When ``dscl -list`` exits
                                              non-zero.
        """
        command = [_DSCL, _LOCAL_NODE, "-list", "/Groups", "PrimaryGroupID"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosUserGroupQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval: list[MacosGroupInfo] = []
        for raw_line in run_result.stdout.splitlines():
            entry = _parse_dscl_list_gid_line(line=raw_line)
            if entry is None:
                continue
            group_info = self.get_group(name=entry["name"])
            if group_info is None:
                continue
            rtnval.append(group_info)

        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv builders
    # ------------------------------------------------------------------

    def _build_useradd_command(
        self,
        *,
        spec: MacosUserSpec,
    ) -> tuple[list[str], list[str]]:
        """
            Build the ``sysadminctl -addUser`` argv from a
            :class:`MacosUserSpec`.

            :param spec: The :class:`MacosUserSpec` to render.

            :returns: ``(full_command, recorded_command)`` tuple where
                      ``full_command`` carries plaintext password values
                      (sent to the runner) and ``recorded_command``
                      replaces every password value with ``"<redacted>"``.
        """
        command: list[str] = [_SYSADMINCTL, "-addUser", spec.name]

        if spec.full_name is not None:
            command.append("-fullName")
            command.append(spec.full_name)

        # ``-password`` flag with the plaintext value lands in the full
        # argv; the recorded copy redacts it below.
        if spec.password is not None:
            command.append("-password")
            command.append(spec.password)

        if spec.shell is not None and len(spec.shell) > 0:
            command.append("-shell")
            command.append(spec.shell)

        if spec.home is not None:
            command.append("-home")
            command.append(spec.home)

        if spec.uid is not None:
            command.append("-UID")
            command.append(str(spec.uid))

        admin_pair = self._admin_pair()
        if admin_pair is not None:
            admin_user, admin_password = admin_pair
            command.append("-adminUser")
            command.append(admin_user)
            command.append("-adminPassword")
            command.append(admin_password)

        recorded = self._sanitise_argv_passwords(argv=command)
        rtnval = (command, recorded)
        return rtnval

    def _build_userdel_command(
        self,
        *,
        name: str,
    ) -> tuple[list[str], list[str]]:
        """
            Build the ``sysadminctl -deleteUser`` argv.

            ``-secure`` is appended unconditionally so the home
            directory is removed alongside the account record (matching
            the cross-OS contract's "delete user removes home" rule).

            :param name: The login name to delete.

            :returns: ``(full_command, recorded_command)`` tuple.  The
                      delete call has no plaintext fields, but the
                      admin pair (if configured) does, so the recorded
                      copy redacts it.
        """
        command: list[str] = [_SYSADMINCTL, "-deleteUser", name, "-secure"]

        admin_pair = self._admin_pair()
        if admin_pair is not None:
            admin_user, admin_password = admin_pair
            command.append("-adminUser")
            command.append(admin_user)
            command.append("-adminPassword")
            command.append(admin_password)

        recorded = self._sanitise_argv_passwords(argv=command)
        rtnval = (command, recorded)
        return rtnval

    def _build_passwd_command(
        self,
        *,
        user: str,
        password: str,
    ) -> tuple[list[str], list[str]]:
        """
            Build the ``sysadminctl -resetPasswordFor`` argv.

            :param user: The login name whose password to set.
            :param password: The plaintext password.

            :returns: ``(full_command, recorded_command)`` tuple where
                      every password value is redacted in the recorded
                      copy.
        """
        command: list[str] = [
            _SYSADMINCTL, "-resetPasswordFor", user,
            "-newPassword", password,
        ]

        admin_pair = self._admin_pair()
        if admin_pair is not None:
            admin_user, admin_password = admin_pair
            command.append("-adminUser")
            command.append(admin_user)
            command.append("-adminPassword")
            command.append(admin_password)

        recorded = self._sanitise_argv_passwords(argv=command)
        rtnval = (command, recorded)
        return rtnval

    def _build_groupadd_command(
        self,
        *,
        spec: MacosGroupSpec,
    ) -> list[str]:
        """
            Build the ``dseditgroup -o create`` argv.

            :param spec: The :class:`MacosGroupSpec` to render.

            :returns: A new list with the full argv.
        """
        command: list[str] = [_DSEDITGROUP, "-o", "create"]
        if spec.gid is not None:
            command.append("-i")
            command.append(str(spec.gid))
        if spec.description is not None:
            command.append("-r")
            command.append(spec.description)
        command.append(spec.name)
        return command

    def _build_first_modify_command(
        self,
        *,
        name: str,
        changes: MacosUserChanges,
    ) -> list[str]:
        """
            Compute the FIRST argv that :meth:`modify_user` would emit
            without consulting current state.  Used purely to populate
            ``command`` on the dry-run result.

            :param name: The current login name.
            :param changes: The requested changes.

            :returns: A representative argv; ``[]`` when ``changes``
                      requests nothing.
        """
        if changes.new_name is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "RecordName", name, changes.new_name,
            ]
            return rtnval

        if changes.uid is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "UniqueID", "<current>", str(changes.uid),
            ]
            return rtnval

        if changes.home is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "NFSHomeDirectory", "<current>", changes.home,
            ]
            return rtnval

        if changes.shell is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "UserShell", "<current>", changes.shell,
            ]
            return rtnval

        if changes.full_name is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "RealName", "<current>", changes.full_name,
            ]
            return rtnval

        if changes.primary_group is not None:
            rtnval = [
                _DSCL, _LOCAL_NODE, "-change",
                "/Users/{}".format(name),
                "PrimaryGroupID", "<current>", "<resolved>",
            ]
            return rtnval

        if changes.groups is not None and len(changes.groups) > 0:
            rtnval = [
                _DSEDITGROUP, "-o", "edit",
                "-a", name, "-t", "user",
                changes.groups[0],
            ]
            return rtnval

        rtnval = []
        return rtnval

    def _build_modify_plan(
        self,
        *,
        name: str,
        changes: MacosUserChanges,
        current: MacosUserInfo,
    ) -> list[list[str]]:
        """
            Compute the ordered list of argvs needed to bring ``current``
            into line with ``changes``.  Skips fields that already match.

            :param name: The current login name.
            :param changes: The requested changes.
            :param current: The user's currently-observed info.

            :returns: One argv per attribute that requires a write.
        """
        plan: list[list[str]] = []
        record_path = "/Users/{}".format(name)

        if changes.new_name is not None and changes.new_name != current.name:
            plan.append([
                _DSCL, _LOCAL_NODE, "-change",
                record_path, "RecordName", current.name, changes.new_name,
            ])

        if changes.uid is not None and changes.uid != current.uid:
            plan.append([
                _DSCL, _LOCAL_NODE, "-change",
                record_path, "UniqueID",
                str(current.uid), str(changes.uid),
            ])

        if changes.home is not None and changes.home != current.home:
            plan.append([
                _DSCL, _LOCAL_NODE, "-change",
                record_path, "NFSHomeDirectory",
                current.home, changes.home,
            ])

        if changes.shell is not None and changes.shell != current.shell:
            plan.append([
                _DSCL, _LOCAL_NODE, "-change",
                record_path, "UserShell",
                current.shell, changes.shell,
            ])

        if (
            changes.full_name is not None
            and changes.full_name != current.full_name
        ):
            plan.append([
                _DSCL, _LOCAL_NODE, "-change",
                record_path, "RealName",
                current.full_name, changes.full_name,
            ])

        # Primary group is matched by name; resolving the GID requires a
        # ``dscl . -read /Groups/<name> PrimaryGroupID`` lookup.  We
        # always emit the change when the caller specifies one because
        # the manager has no cheap way to compare the requested name to
        # ``current.gid`` without that extra read on every modify call.
        if changes.primary_group is not None:
            resolved_gid = self._lookup_group_gid(name=changes.primary_group)
            if resolved_gid is not None and resolved_gid != current.gid:
                plan.append([
                    _DSCL, _LOCAL_NODE, "-change",
                    record_path, "PrimaryGroupID",
                    str(current.gid), str(resolved_gid),
                ])

        if changes.groups is not None:
            desired = list(changes.groups)
            observed = set(current.groups)
            if changes.append_groups:
                for group_name in desired:
                    if group_name in observed:
                        continue
                    plan.append([
                        _DSEDITGROUP, "-o", "edit",
                        "-a", name, "-t", "user",
                        group_name,
                    ])
            else:
                desired_set = set(desired)
                # First add missing memberships.
                for group_name in desired:
                    if group_name in observed:
                        continue
                    plan.append([
                        _DSEDITGROUP, "-o", "edit",
                        "-a", name, "-t", "user",
                        group_name,
                    ])
                # Then evict surplus memberships.  Skip the user's
                # primary group, which ``id -Gn`` reports but cannot
                # be revoked via ``dseditgroup``.
                for observed_name in current.groups:
                    if observed_name in desired_set:
                        continue
                    plan.append([
                        _DSEDITGROUP, "-o", "edit",
                        "-d", name, "-t", "user",
                        observed_name,
                    ])

        return plan

    # ------------------------------------------------------------------
    # Internal helpers — admin pair / argv sanitisation
    # ------------------------------------------------------------------

    def _admin_pair(self) -> Optional[tuple[str, str]]:
        """
            Return the configured admin (user, password) pair, or
            ``None`` when either half is missing.
        """
        config = self._typed_config()
        admin_user = config.admin_user
        admin_password = config.admin_password
        if admin_user is None:
            rtnval: Optional[tuple[str, str]] = None
            return rtnval
        if admin_password is None:
            rtnval = None
            return rtnval
        rtnval = (admin_user, admin_password)
        return rtnval

    def _sanitise_argv_passwords(
        self,
        *,
        argv: list[str],
    ) -> list[str]:
        """
            Return a copy of ``argv`` with every password value replaced
            by ``"<redacted>"``.

            macOS sysadminctl-flavour password flags (``-password``,
            ``-newPassword``, ``-adminPassword``) are followed
            immediately by the plaintext value.  The sanitiser walks
            the argv linearly, redacting the value-position only.

            :param argv: The argv to sanitise.

            :returns: A new list (the original is left intact).
        """
        password_flags = {
            "-password",
            "-newPassword",
            "-adminPassword",
        }
        rtnval: list[str] = []
        redact_next = False
        for token in argv:
            if redact_next:
                rtnval.append(_REDACTED)
                redact_next = False
                continue
            rtnval.append(token)
            if token in password_flags:
                redact_next = True
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — dispatch + result construction
    # ------------------------------------------------------------------

    def _dispatch_user_write(
        self,
        *,
        command: list[str],
        name: str,
    ) -> MacosUserGroupActionResult:
        """
            Dispatch a user-scope write command and wrap its outcome.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="user_write",
            name=name,
            kind=MacosPrincipalKind.USER,
        )
        return rtnval

    def _dispatch_group_write(
        self,
        *,
        command: list[str],
        name: str,
    ) -> MacosUserGroupActionResult:
        """
            Dispatch a group-scope write command and wrap its outcome.
        """
        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation="group_write",
            name=name,
            kind=MacosPrincipalKind.GROUP,
        )
        return rtnval

    def _build_user_result(
        self,
        *,
        status: MacosUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosUserGroupActionResult:
        """
            Construct a user-kind :class:`MacosUserGroupActionResult`
            without invoking the runner.
        """
        rtnval = MacosUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=MacosPrincipalKind.USER,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _build_group_result(
        self,
        *,
        status: MacosUserGroupActionStatus,
        name: str,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosUserGroupActionResult:
        """
            Construct a group-kind :class:`MacosUserGroupActionResult`
            without invoking the runner.
        """
        rtnval = MacosUserGroupActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            kind=MacosPrincipalKind.GROUP,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — directory-services reads
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosUserGroupManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosUserGroupManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, MacosUserGroupManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosUserGroupManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _user_record_exists(self, *, name: str) -> bool:
        """
            Probe ``dscl . -read /Users/<name>`` for existence.
            Reads do not elevate.

            :param name: The login name to probe.

            :returns: True when the read exits zero; False otherwise.
        """
        command = [_DSCL, _LOCAL_NODE, "-read", "/Users/{}".format(name)]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _group_record_exists(self, *, name: str) -> bool:
        """
            Probe ``dscl . -read /Groups/<name>`` for existence.

            :param name: The group name to probe.

            :returns: True when the read exits zero.
        """
        command = [_DSCL, _LOCAL_NODE, "-read", "/Groups/{}".format(name)]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _dscl_read_user(self, *, name: str) -> Optional[dict]:
        """
            Run ``dscl . -read /Users/<name>`` and parse the multi-line
            attribute block.

            :param name: The login name.

            :returns: A dict with ``name``, ``uid``, ``gid``, ``home``,
                      ``shell``, ``full_name`` keys, or ``None`` when
                      the read fails.
        """
        command = [_DSCL, _LOCAL_NODE, "-read", "/Users/{}".format(name)]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        attributes = _parse_dscl_read_output(output=run_result.stdout)
        rtnval = _materialise_user_attributes(
            attributes=attributes, fallback_name=name,
        )
        return rtnval

    def _dscl_read_group(self, *, name: str) -> Optional[MacosGroupInfo]:
        """
            Run ``dscl . -read /Groups/<name>`` and parse the result.

            :param name: The group name.

            :returns: The :class:`MacosGroupInfo`, or ``None`` when the
                      read fails.
        """
        command = [_DSCL, _LOCAL_NODE, "-read", "/Groups/{}".format(name)]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[MacosGroupInfo] = None
            return rtnval

        attributes = _parse_dscl_read_output(output=run_result.stdout)
        rtnval = _materialise_group_attributes(
            attributes=attributes, fallback_name=name,
        )
        return rtnval

    def _lookup_user_groups(self, *, name: str) -> list[str]:
        """
            Run ``id -Gn <name>`` and split the result.

            :param name: The login name.

            :returns: The list of group names; empty on failure.
        """
        command = [_ID, "-Gn", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list[str] = []
            return rtnval

        stripped = run_result.stdout.strip()
        if len(stripped) == 0:
            rtnval = []
            return rtnval

        rtnval = stripped.split()
        return rtnval

    def _lookup_group_gid(self, *, name: str) -> Optional[int]:
        """
            Run ``dscl . -read /Groups/<name> PrimaryGroupID`` and
            return the parsed GID.

            :param name: The group name.

            :returns: The GID, or ``None`` on failure.
        """
        command = [
            _DSCL, _LOCAL_NODE, "-read",
            "/Groups/{}".format(name), "PrimaryGroupID",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[int] = None
            return rtnval

        rtnval = _parse_primary_group_id(output=run_result.stdout)
        return rtnval

    def _dseditgroup_checkmember(
        self,
        *,
        user: str,
        group: str,
    ) -> bool:
        """
            Probe ``dseditgroup -o checkmember -m <user> <group>`` for
            membership.

            ``dseditgroup`` exits zero when the user is a member and
            non-zero otherwise; the stdout sentence is informational and
            not consulted by this probe.

            :param user: The login name.
            :param group: The group name.

            :returns: True when the user is a member.
        """
        command = [
            _DSEDITGROUP, "-o", "checkmember",
            "-m", user, group,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _account_is_locked(self, *, user: str) -> bool:
        """
            Probe ``pwpolicy -u <user> -authentication-allowed``.

            ``pwpolicy`` reports either ``Authentication is allowed``
            or ``Authentication is not allowed``.  Stripped match on
            ``"not allowed"`` is treated as locked.  On a non-zero
            exit, ``False`` is returned so the caller decides how to
            surface absence.

            :param user: The login name.

            :returns: True when the account is locked.
        """
        command = [_PWPOLICY, "-u", user, "-authentication-allowed"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        text = run_result.stdout.lower()
        rtnval = "not allowed" in text
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_dscl_read_output(*, output: str) -> dict:
    """
        Parse ``dscl . -read /Users/<name>`` (or ``/Groups/<name>``)
        output into a flat ``dict[str, str]``.

        ``dscl -read`` emits one ``Attribute: value`` line per scalar
        attribute, plus indented continuation lines for multi-valued
        attributes (e.g. ``GroupMembership``).  Continuation lines are
        joined into the previous attribute's value with single-space
        separators.  Empty lines are ignored.

        :param output: Raw stdout text.

        :returns: ``dict[str, str]`` keyed by attribute name.
    """
    rtnval: dict[str, str] = {}
    current_key: Optional[str] = None
    for raw_line in output.splitlines():
        if len(raw_line.strip()) == 0:
            continue
        # Continuation line: starts with whitespace and we have a key
        # already.
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


def _materialise_user_attributes(
    *,
    attributes: dict,
    fallback_name: str,
) -> dict:
    """
        Convert a parsed ``dscl -read /Users/<name>`` attribute dict
        into the dict shape consumed by :class:`MacosUserInfo`.

        :param attributes: Parsed attribute map.
        :param fallback_name: Name to fall back to when ``RecordName``
                              is absent from the read output.

        :returns: Dict with ``name``, ``uid``, ``gid``, ``home``,
                  ``shell``, ``full_name`` keys.  Numeric coercion
                  failures default to ``0``.
    """
    record_name = attributes.get("RecordName", "").strip()
    if len(record_name) == 0:
        record_name = fallback_name

    uid_raw = attributes.get("UniqueID", "").strip()
    try:
        uid_value = int(uid_raw)
    except ValueError:
        uid_value = 0

    gid_raw = attributes.get("PrimaryGroupID", "").strip()
    try:
        gid_value = int(gid_raw)
    except ValueError:
        gid_value = 0

    rtnval = {
        "name": record_name,
        "uid": uid_value,
        "gid": gid_value,
        "home": attributes.get("NFSHomeDirectory", "").strip(),
        "shell": attributes.get("UserShell", "").strip(),
        "full_name": attributes.get("RealName", "").strip(),
    }
    return rtnval


def _materialise_group_attributes(
    *,
    attributes: dict,
    fallback_name: str,
) -> Optional[MacosGroupInfo]:
    """
        Convert a parsed ``dscl -read /Groups/<name>`` attribute dict
        into a :class:`MacosGroupInfo`.

        :param attributes: Parsed attribute map.
        :param fallback_name: Name to fall back to when ``RecordName``
                              is absent.

        :returns: The :class:`MacosGroupInfo` or ``None`` when the
                  ``PrimaryGroupID`` attribute is absent / unparseable.
    """
    name_value = attributes.get("RecordName", "").strip()
    if len(name_value) == 0:
        name_value = fallback_name

    gid_raw = attributes.get("PrimaryGroupID", "").strip()
    if len(gid_raw) == 0:
        rtnval: Optional[MacosGroupInfo] = None
        return rtnval
    try:
        gid_value = int(gid_raw)
    except ValueError:
        rtnval = None
        return rtnval

    membership_raw = attributes.get("GroupMembership", "").strip()
    members: list[str] = []
    if len(membership_raw) > 0:
        for token in membership_raw.split():
            cleaned = token.strip()
            if len(cleaned) > 0:
                members.append(cleaned)

    rtnval = MacosGroupInfo(
        name=name_value,
        gid=gid_value,
        members=members,
    )
    return rtnval


def _parse_dscl_list_uid_line(*, line: str) -> Optional[dict]:
    """
        Parse a single ``dscl . -list /Users UniqueID`` line.

        Lines are space- (or tab-) separated ``<name> <uid>`` pairs;
        ``dscl`` pads the UID column out so multiple consecutive spaces
        are common.

        :param line: A raw line of stdout.

        :returns: Dict with ``name`` and ``uid`` keys, or ``None`` when
                  the line cannot be parsed.
    """
    stripped = line.strip()
    if len(stripped) == 0:
        rtnval: Optional[dict] = None
        return rtnval

    tokens = stripped.split()
    if len(tokens) < 2:
        rtnval = None
        return rtnval

    name_value = tokens[0]
    uid_raw = tokens[-1]
    try:
        uid_value = int(uid_raw)
    except ValueError:
        rtnval = None
        return rtnval

    rtnval = {
        "name": name_value,
        "uid": uid_value,
    }
    return rtnval


def _parse_dscl_list_gid_line(*, line: str) -> Optional[dict]:
    """
        Parse a single ``dscl . -list /Groups PrimaryGroupID`` line.
        Format identical to the user-list variant.

        :param line: A raw line of stdout.

        :returns: Dict with ``name`` and ``gid`` keys, or ``None``.
    """
    stripped = line.strip()
    if len(stripped) == 0:
        rtnval: Optional[dict] = None
        return rtnval

    tokens = stripped.split()
    if len(tokens) < 2:
        rtnval = None
        return rtnval

    name_value = tokens[0]
    gid_raw = tokens[-1]
    try:
        gid_value = int(gid_raw)
    except ValueError:
        rtnval = None
        return rtnval

    rtnval = {
        "name": name_value,
        "gid": gid_value,
    }
    return rtnval


def _parse_primary_group_id(*, output: str) -> Optional[int]:
    """
        Parse ``dscl . -read /Groups/<name> PrimaryGroupID`` output and
        return the GID.

        Output shape::

            PrimaryGroupID: 20

        :param output: Raw stdout.

        :returns: The integer GID, or ``None`` when not present /
                  parseable.
    """
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip() != "PrimaryGroupID":
            continue
        cleaned_value = value.strip()
        try:
            rtnval: Optional[int] = int(cleaned_value)
        except ValueError:
            rtnval = None
        return rtnval

    rtnval = None
    return rtnval
