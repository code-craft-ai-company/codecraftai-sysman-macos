"""
    macOS filesystem-quotas feature.

    Implements the cross-OS filesystem-quotas contract for macOS
    using the BSD ``quota`` family (``quotacheck``, ``quotaon``,
    ``quotaoff``, ``setquota``, ``quota``, ``repquota``) plus a
    ``mount`` parser for filesystem-type detection.  Every
    command is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    Major limitation — APFS has no quota subsystem:
        Modern macOS (10.13+ High Sierra and later) uses APFS
        as the default filesystem.  APFS does NOT have a per-
        user / per-group quota subsystem; Apple removed classic
        quotas when introducing APFS and replaced them with
        per-volume container management (``diskutil apfs``
        reserve / quota properties), which is a fundamentally
        different surface that does NOT satisfy the cross-OS
        contract's per-principal semantics.

        Rather than dispatching commands that fail with kernel
        errors, this manager:

        * On writes against APFS: returns ``FAILED`` with
          ``detail="apfs_no_quota_support"`` and the would-have-
          been argv recorded on ``command``.
        * On reads against APFS: returns the empty / disabled
          variant of the read type (``None`` for single-
          principal queries, ``[]`` for ``list_quotas``,
          ``MacosQuotaStatus(enabled=False, ...)`` for
          ``get_quota_status``).

    Quota-aware filesystems:
        The ``MacosFilesystemQuotasManagerConfig.quota_aware_filesystems``
        list enumerates which filesystem types the manager will
        target.  Defaults to ``["hfs", "ufs"]``.  Operators
        running on a custom kernel module may extend this list.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_user_quota``, ``get_group_quota``, ``list_quotas``,
        ``get_quota_status``, the ``mount`` probe) NEVER elevate
        regardless of ``config.elevated``.

    Mount validation:
        Every write verb calls ``_detect_fstype(mountpoint)``
        before any quota command.  A path that ``mount`` does not
        list raises
        :class:`MacosQuotaFilesystemNotMountedError`.
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

_QUOTACHECK = "quotacheck"
_QUOTAON = "quotaon"
_QUOTAOFF = "quotaoff"
_SETQUOTA = "setquota"
_QUOTA = "quota"
_REPQUOTA = "repquota"
_MOUNT = "mount"

_BLOCK_BYTES = 1024

_APFS_DETAIL = "apfs_no_quota_support"


MacosQuotaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class MacosQuotaPrincipalKind(Enum):
    """
        Identifies whether a quota record applies to a user or
        group principal.

        +-----------+------------------------+
        | Value     | Meaning                |
        +===========+========================+
        | ``USER``  | Per-user quotas (-u)   |
        +-----------+------------------------+
        | ``GROUP`` | Per-group quotas (-g)  |
        +-----------+------------------------+
    """
    USER = "user"
    GROUP = "group"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


def _default_quota_aware_filesystems() -> list[str]:
    """
        Default list of quota-aware filesystem types for the
        manager configuration.  Returns a fresh list per call so
        the dataclass mutable-default rule is honoured.
    """
    rtnval = ["hfs", "ufs"]
    return rtnval


@dataclass
class MacosFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`MacosFilesystemQuotasManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param quota_aware_filesystems: List of filesystem types
            the manager treats as quota-aware.  Defaults to
            ``["hfs", "ufs"]``; ``apfs`` is intentionally absent.
            Operators running on a custom kernel module may
            extend this list as needed.
    """
    quota_aware_filesystems: list[str] = field(
        default_factory=_default_quota_aware_filesystems,
    )


@dataclass
class MacosQuotaActionResult(FeatureActionResult):
    """
        Result of a filesystem-quotas write operation.

        :param mountpoint: The mountpoint acted upon, when
                           applicable.
        :param principal: The user or group acted upon, when
                          applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` or ``FAILED`` outcomes.
    """
    mountpoint: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosQuotaInfo:
    """
        Per-principal quota record on a single mountpoint.

        :param mountpoint: Filesystem mountpoint the record
                           applies to.
        :param principal: Username or group name.
        :param principal_kind: USER or GROUP.
        :param used_bytes: Current block usage in bytes; ``None``
                           when the underlying tool did not
                           report a value.
        :param soft_bytes: Soft block limit in bytes; ``None``
                           when absent.
        :param hard_bytes: Hard block limit in bytes; ``None``
                           when absent.
        :param used_inodes: Current inode usage; ``None`` when
                            not reported.
        :param soft_inodes: Soft inode limit; ``None`` when not
                            reported.
        :param hard_inodes: Hard inode limit; ``None`` when not
                            reported.
        :param grace_period_seconds: Block grace-period duration
                                     in seconds, or ``None``.
    """
    mountpoint: str
    principal: str
    principal_kind: MacosQuotaPrincipalKind
    used_bytes: Optional[int]
    soft_bytes: Optional[int]
    hard_bytes: Optional[int]
    used_inodes: Optional[int]
    soft_inodes: Optional[int]
    hard_inodes: Optional[int]
    grace_period_seconds: Optional[int]


@dataclass
class MacosQuotaStatus:
    """
        Mountpoint-wide quota enforcement posture.

        :param mountpoint: Filesystem mountpoint.
        :param enabled: True when at least one of user / group
                        quotas is active.
        :param enforcing: True when at least one is enforced.  On
                          BSD-style systems this collapses to the
                          same value as ``enabled``; the field
                          exists for cross-OS parity.
        :param user_quotas_active: True when ``quotaon -p``
                                   reports user quotas as on.
        :param group_quotas_active: True when ``quotaon -p``
                                    reports group quotas as on.
        :param filesystem_type: Detected filesystem type
                                (``"hfs"``, ``"apfs"``, ...) or
                                ``None`` when undetectable.
    """
    mountpoint: str
    enabled: bool
    enforcing: bool
    user_quotas_active: bool
    group_quotas_active: bool
    filesystem_type: Optional[str]


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosQuotaQueryError(Exception):
    """
        Raised when a quota read fails for a reason other than
        the principal simply being absent.
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

        err_msg_lines = ["Quota query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosQuotaFilesystemNotMountedError(Exception):
    """
        Raised when a write verb is invoked against a mountpoint
        that is not currently mounted (per ``mount`` parsing).
    """

    def __init__(
        self,
        *,
        mountpoint: str,
    ) -> None:
        """
            :param mountpoint: The mountpoint that failed
                               validation.
        """
        self.mountpoint = mountpoint

        err_msg_lines = [
            "Filesystem-quotas write requires a mounted target.",
        ]
        err_msg_lines.append("    MOUNTPOINT: {}".format(mountpoint))
        err_msg_lines.append("    PROBE: mount")
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosFilesystemQuotasManager(BaseFeatureManager):
    """
        macOS filesystem-quotas manager.  Wraps the BSD ``quota``
        family through the injected :class:`ICommandRunner`.

        APFS is detected and surfaced as a typed FAILED result
        rather than dispatching commands that would fail with
        kernel-level errors.
    """

    result_class = MacosQuotaActionResult
    status_class = MacosQuotaActionStatus
    feature_name = "filesystem-quotas"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_quotas(
        self,
        mountpoint: str,
    ) -> MacosQuotaActionResult:
        """
            Enable quota enforcement on ``mountpoint``.

            Runs ``quotacheck -ug <mountpoint>`` to (re)build the
            on-disk accounting database, then ``quotaon -ug
            <mountpoint>`` to begin enforcement.  Returns
            ``SKIPPED`` when both user and group quotas are
            already active.  Returns ``FAILED`` with
            ``detail="apfs_no_quota_support"`` for APFS mounts.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`MacosQuotaActionResult`.

            :raises MacosQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        planned_command = [_QUOTACHECK, "-ug", mountpoint]

        if self._config.dry_run:
            dry_result = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
            )
            return dry_result

        fstype_value = self._detect_fstype(mountpoint=mountpoint)
        if not self._is_quota_aware(fstype=fstype_value):
            failed = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.FAILED,
                detail=_APFS_DETAIL,
                command=list(planned_command),
                mountpoint=mountpoint,
                message="filesystem '{}' has no quota subsystem on macOS".format(
                    fstype_value,
                ),
            )
            return failed

        current_status = self.get_quota_status(mountpoint=mountpoint)
        if current_status.user_quotas_active and current_status.group_quotas_active:
            skipped = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
                message="quotas are already active for both user and group",
            )
            return skipped

        check_result = self._dispatch(
            command=planned_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )

        if check_result.status != MacosQuotaActionStatus.SUCCESS:
            return check_result

        on_command = [_QUOTAON, "-ug", mountpoint]
        on_result = self._dispatch(
            command=on_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return on_result

    def disable_quotas(
        self,
        mountpoint: str,
    ) -> MacosQuotaActionResult:
        """
            Disable quota enforcement on ``mountpoint`` via
            ``quotaoff -ug <mountpoint>``.  Returns ``SKIPPED``
            when both kinds are already off.  Returns ``FAILED``
            with ``detail="apfs_no_quota_support"`` for APFS.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`MacosQuotaActionResult`.

            :raises MacosQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        command = [_QUOTAOFF, "-ug", mountpoint]

        if self._config.dry_run:
            dry_result = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
            )
            return dry_result

        fstype_value = self._detect_fstype(mountpoint=mountpoint)
        if not self._is_quota_aware(fstype=fstype_value):
            failed = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.FAILED,
                detail=_APFS_DETAIL,
                command=list(command),
                mountpoint=mountpoint,
                message="filesystem '{}' has no quota subsystem on macOS".format(
                    fstype_value,
                ),
            )
            return failed

        current_status = self.get_quota_status(mountpoint=mountpoint)
        already_off = (
            not current_status.user_quotas_active
            and not current_status.group_quotas_active
        )
        if already_off:
            skipped = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
                message="quotas are already off for both user and group",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return rtnval

    def set_user_quota(
        self,
        mountpoint: str,
        user: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> MacosQuotaActionResult:
        """
            Set the soft and hard block quota for ``user`` on
            ``mountpoint``.

            :param mountpoint: The filesystem mountpoint.
            :param user: The username.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.

            :returns: The :class:`MacosQuotaActionResult`.

            :raises MacosQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        rtnval = self._set_quota(
            mountpoint=mountpoint,
            principal=user,
            principal_kind=MacosQuotaPrincipalKind.USER,
            soft_bytes=soft_bytes,
            hard_bytes=hard_bytes,
        )
        return rtnval

    def set_group_quota(
        self,
        mountpoint: str,
        group: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> MacosQuotaActionResult:
        """
            Set the soft and hard block quota for ``group`` on
            ``mountpoint``.

            :param mountpoint: The filesystem mountpoint.
            :param group: The group name.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.

            :returns: The :class:`MacosQuotaActionResult`.

            :raises MacosQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        rtnval = self._set_quota(
            mountpoint=mountpoint,
            principal=group,
            principal_kind=MacosQuotaPrincipalKind.GROUP,
            soft_bytes=soft_bytes,
            hard_bytes=hard_bytes,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user_quota(
        self,
        mountpoint: str,
        user: str,
    ) -> Optional[MacosQuotaInfo]:
        """
            Return :class:`MacosQuotaInfo` for ``user`` on
            ``mountpoint`` or ``None`` when the principal has no
            record on this mount, or when the mountpoint is APFS
            (where there are no quota records by definition).

            :param mountpoint: The filesystem mountpoint.
            :param user: The username.

            :returns: The parsed info or ``None``.

            :raises MacosQuotaQueryError: when ``quota`` exits
                with an unexpected status.
        """
        rtnval = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=user,
            principal_kind=MacosQuotaPrincipalKind.USER,
        )
        return rtnval

    def get_group_quota(
        self,
        mountpoint: str,
        group: str,
    ) -> Optional[MacosQuotaInfo]:
        """
            Return :class:`MacosQuotaInfo` for ``group`` on
            ``mountpoint`` or ``None`` when the principal has no
            record on this mount, or when the mountpoint is APFS.

            :param mountpoint: The filesystem mountpoint.
            :param group: The group name.

            :returns: The parsed info or ``None``.

            :raises MacosQuotaQueryError: when ``quota`` exits
                with an unexpected status.
        """
        rtnval = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=group,
            principal_kind=MacosQuotaPrincipalKind.GROUP,
        )
        return rtnval

    def list_quotas(
        self,
        mountpoint: str,
    ) -> list[MacosQuotaInfo]:
        """
            Enumerate every user and group quota on ``mountpoint``
            via ``repquota -ug <mountpoint>``.  Returns ``[]`` for
            APFS mountpoints.

            :param mountpoint: The filesystem mountpoint.

            :returns: One :class:`MacosQuotaInfo` per principal.

            :raises MacosQuotaQueryError: when ``repquota`` exits
                non-zero on a quota-aware mount.
        """
        fstype_value = self._best_effort_fstype(mountpoint=mountpoint)
        if fstype_value is None or not self._is_quota_aware(fstype=fstype_value):
            rtnval: list[MacosQuotaInfo] = []
            return rtnval

        command = [_REPQUOTA, "-ug", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_repquota_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
        )
        return rtnval

    def get_quota_status(
        self,
        mountpoint: str,
    ) -> MacosQuotaStatus:
        """
            Query mountpoint-wide quota enforcement state.

            For APFS mountpoints returns
            ``MacosQuotaStatus(enabled=False, enforcing=False,
            user_quotas_active=False, group_quotas_active=False,
            filesystem_type="apfs")`` without invoking
            ``quotaon``.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`MacosQuotaStatus`.

            :raises MacosQuotaQueryError: when ``quotaon -p``
                exits non-zero on a quota-aware mount.
        """
        fstype_value = self._best_effort_fstype(mountpoint=mountpoint)

        if fstype_value is None or not self._is_quota_aware(fstype=fstype_value):
            disabled = MacosQuotaStatus(
                mountpoint=mountpoint,
                enabled=False,
                enforcing=False,
                user_quotas_active=False,
                group_quotas_active=False,
                filesystem_type=fstype_value,
            )
            return disabled

        command = [_QUOTAON, "-p", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        user_active, group_active = _parse_quotaon_status(
            stdout=run_result.stdout,
        )
        either_active = user_active or group_active
        rtnval = MacosQuotaStatus(
            mountpoint=mountpoint,
            enabled=either_active,
            enforcing=either_active,
            user_quotas_active=user_active,
            group_quotas_active=group_active,
            filesystem_type=fstype_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_fstype(
        self,
        *,
        mountpoint: str,
    ) -> str:
        """
            Run ``mount`` and return the fstype reported for
            ``mountpoint``.  Raises
            :class:`MacosQuotaFilesystemNotMountedError` when the
            mountpoint is not present in the output.

            :param mountpoint: The filesystem mountpoint.
        """
        command = [_MOUNT]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise MacosQuotaFilesystemNotMountedError(
                mountpoint=mountpoint,
            )

        fstype_value = _parse_mount_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
        )

        if fstype_value is None:
            raise MacosQuotaFilesystemNotMountedError(
                mountpoint=mountpoint,
            )

        rtnval = fstype_value
        return rtnval

    def _best_effort_fstype(
        self,
        *,
        mountpoint: str,
    ) -> Optional[str]:
        """
            Return the fstype for ``mountpoint`` or ``None``.
            Used by reads so they never raise on a stale
            mountpoint.

            :param mountpoint: The filesystem mountpoint.
        """
        command = [_MOUNT]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        rtnval = _parse_mount_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
        )
        return rtnval

    def _is_quota_aware(
        self,
        *,
        fstype: Optional[str],
    ) -> bool:
        """
            Return True when ``fstype`` is in the configured
            quota-aware list.

            :param fstype: The filesystem type token.
        """
        if fstype is None:
            rtnval = False
            return rtnval

        config = self._config
        if isinstance(config, MacosFilesystemQuotasManagerConfig):
            allowed = config.quota_aware_filesystems
        else:
            allowed = _default_quota_aware_filesystems()

        rtnval = fstype.lower() in [item.lower() for item in allowed]
        return rtnval

    def _set_quota(
        self,
        *,
        mountpoint: str,
        principal: str,
        principal_kind: MacosQuotaPrincipalKind,
        soft_bytes: int,
        hard_bytes: int,
    ) -> MacosQuotaActionResult:
        """
            Shared implementation of :meth:`set_user_quota` and
            :meth:`set_group_quota`.

            :param mountpoint: Filesystem mountpoint.
            :param principal: Username or group name.
            :param principal_kind: Which kind.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.
        """
        soft_kb = soft_bytes // _BLOCK_BYTES
        hard_kb = hard_bytes // _BLOCK_BYTES

        if principal_kind is MacosQuotaPrincipalKind.USER:
            kind_flag = "-u"
        else:
            kind_flag = "-g"

        command = [
            _SETQUOTA,
            kind_flag,
            principal,
            str(soft_kb),
            str(hard_kb),
            "0",
            "0",
            mountpoint,
        ]

        if self._config.dry_run:
            dry_result = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.DRY_RUN,
                detail=principal,
                command=list(command),
                mountpoint=mountpoint,
                principal=principal,
            )
            return dry_result

        fstype_value = self._detect_fstype(mountpoint=mountpoint)
        if not self._is_quota_aware(fstype=fstype_value):
            failed = MacosQuotaActionResult(
                status=MacosQuotaActionStatus.FAILED,
                detail=_APFS_DETAIL,
                command=list(command),
                mountpoint=mountpoint,
                principal=principal,
                message="filesystem '{}' has no quota subsystem on macOS".format(
                    fstype_value,
                ),
            )
            return failed

        current = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=principal,
            principal_kind=principal_kind,
        )
        if current is not None:
            soft_matches = current.soft_bytes == soft_bytes
            hard_matches = current.hard_bytes == hard_bytes
            if soft_matches and hard_matches:
                skipped = MacosQuotaActionResult(
                    status=MacosQuotaActionStatus.SKIPPED,
                    detail=principal,
                    command=list(command),
                    mountpoint=mountpoint,
                    principal=principal,
                    message="current quota already matches the requested limits",
                )
                return skipped

        rtnval = self._dispatch(
            command=command,
            detail=principal,
            mountpoint=mountpoint,
            principal=principal,
        )
        return rtnval

    def _query_principal_quota(
        self,
        *,
        mountpoint: str,
        principal: str,
        principal_kind: MacosQuotaPrincipalKind,
    ) -> Optional[MacosQuotaInfo]:
        """
            Shared implementation of :meth:`get_user_quota` and
            :meth:`get_group_quota`.

            :param mountpoint: Filesystem mountpoint.
            :param principal: Username or group name.
            :param principal_kind: Which kind.
        """
        fstype_value = self._best_effort_fstype(mountpoint=mountpoint)
        if fstype_value is None or not self._is_quota_aware(fstype=fstype_value):
            rtnval: Optional[MacosQuotaInfo] = None
            return rtnval

        if principal_kind is MacosQuotaPrincipalKind.USER:
            kind_flag = "-u"
        else:
            kind_flag = "-g"

        command = [_QUOTA, kind_flag, principal]
        run_result = self._runner.run(list(command), elevated=False)

        # ``quota`` returns 0 when the principal has at least
        # one row, 1 when no quotas are defined on the host at
        # all, and 2 when the principal has no quotas.  Anything
        # else is a query failure.
        if run_result.exit_code not in (0, 1, 2):
            raise MacosQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_quota_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
            principal=principal,
            principal_kind=principal_kind,
        )
        return rtnval


# ----------------------------------------------------------------------
# Parser helpers (module-scoped, pure functions)
# ----------------------------------------------------------------------


def _parse_mount_output(
    *,
    stdout: str,
    mountpoint: str,
) -> Optional[str]:
    """
        Parse the BSD ``mount`` output and return the fstype
        reported for ``mountpoint``.

        Output rows look like::

            /dev/disk1s1 on / (apfs, sealed, local, read-only)
            /dev/disk2s1 on /Volumes/Backup (hfs, local, journaled)

        Anchor on the FIRST `` on `` (which separates the device
        from the mountpoint and options), since the mountpoint
        itself may contain the literal ``" on "`` as part of its
        path; anchoring on the last occurrence would slice off
        any embedded `` on `` and fail to match.

        :param stdout: Raw mount stdout.
        :param mountpoint: The mountpoint to look up.

        :returns: The fstype token or ``None``.
    """
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            continue

        anchor = " on "
        anchor_index = line.find(anchor)
        if anchor_index < 0:
            continue

        right_side = line[anchor_index + len(anchor):]

        # The right side is "<mountpoint> (<fstype>, <options>)"
        paren_index = right_side.find(" (")
        if paren_index < 0:
            continue

        candidate_mount = right_side[:paren_index].strip()
        if candidate_mount != mountpoint:
            continue

        attrs_section = right_side[paren_index + 2:]
        # Strip the trailing ')'.
        if attrs_section.endswith(")"):
            attrs_section = attrs_section[:-1]

        first_comma = attrs_section.find(",")
        if first_comma < 0:
            fstype_token = attrs_section.strip()
        else:
            fstype_token = attrs_section[:first_comma].strip()

        rtnval: Optional[str] = fstype_token.lower()
        return rtnval

    rtnval = None
    return rtnval


def _parse_quota_output(
    *,
    stdout: str,
    mountpoint: str,
    principal: str,
    principal_kind: MacosQuotaPrincipalKind,
) -> Optional[MacosQuotaInfo]:
    """
        Parse ``quota -u <user>`` (or ``-g <group>``) output and
        return the row matching ``mountpoint``, or ``None``.

        :param stdout: Raw stdout from ``quota``.
        :param mountpoint: The mountpoint we want.
        :param principal: The principal name.
        :param principal_kind: USER or GROUP.

        :returns: A :class:`MacosQuotaInfo` or ``None``.
    """
    rows: list[list[str]] = []
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("Disk quotas"):
            continue
        if stripped.startswith("Filesystem"):
            continue
        if stripped.startswith("none"):
            continue
        if stripped.startswith("blocks"):
            continue

        tokens = stripped.split()
        rows.append(tokens)

    if len(rows) == 0:
        rtnval: Optional[MacosQuotaInfo] = None
        return rtnval

    matched: Optional[list[str]] = None
    for tokens in rows:
        first = tokens[0]
        if first == mountpoint:
            matched = tokens
            break
        if first.endswith(mountpoint):
            matched = tokens
            break
        if len(rows) == 1:
            matched = tokens
            break

    if matched is None:
        rtnval = None
        return rtnval

    rtnval = _build_quota_info_from_quota_row(
        row=matched,
        mountpoint=mountpoint,
        principal=principal,
        principal_kind=principal_kind,
    )
    return rtnval


def _build_quota_info_from_quota_row(
    *,
    row: list[str],
    mountpoint: str,
    principal: str,
    principal_kind: MacosQuotaPrincipalKind,
) -> MacosQuotaInfo:
    """
        Map a ``quota`` row token list into a
        :class:`MacosQuotaInfo`.

        :param row: The token list.
        :param mountpoint: Mountpoint to record.
        :param principal: Principal name.
        :param principal_kind: USER or GROUP.
    """
    used_blocks = _strip_warning_marker(token=row[1] if len(row) > 1 else "")
    soft_blocks = _strip_warning_marker(token=row[2] if len(row) > 2 else "")
    hard_blocks = _strip_warning_marker(token=row[3] if len(row) > 3 else "")

    used_bytes = _kb_token_to_bytes(token=used_blocks)
    soft_bytes = _kb_token_to_bytes(token=soft_blocks)
    hard_bytes = _kb_token_to_bytes(token=hard_blocks)

    used_inodes = None
    soft_inodes = None
    hard_inodes = None
    grace_period_seconds = None

    if len(row) >= 5:
        candidate_grace = row[4]
        grace_period_seconds = _grace_token_to_seconds(token=candidate_grace)

    if len(row) >= 8:
        used_inodes = _int_token_or_none(token=row[5])
        soft_inodes = _int_token_or_none(token=row[6])
        hard_inodes = _int_token_or_none(token=row[7])

    rtnval = MacosQuotaInfo(
        mountpoint=mountpoint,
        principal=principal,
        principal_kind=principal_kind,
        used_bytes=used_bytes,
        soft_bytes=soft_bytes,
        hard_bytes=hard_bytes,
        used_inodes=used_inodes,
        soft_inodes=soft_inodes,
        hard_inodes=hard_inodes,
        grace_period_seconds=grace_period_seconds,
    )
    return rtnval


def _parse_repquota_output(
    *,
    stdout: str,
    mountpoint: str,
) -> list[MacosQuotaInfo]:
    """
        Parse ``repquota -ug <mountpoint>`` two-section output
        into a flat ``list[MacosQuotaInfo]``.

        :param stdout: Raw repquota stdout.
        :param mountpoint: Mountpoint to record on every info.
    """
    rtnval: list[MacosQuotaInfo] = []
    current_kind: Optional[MacosQuotaPrincipalKind] = None

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue

        if stripped.startswith("***"):
            lower = stripped.lower()
            if "user quotas" in lower:
                current_kind = MacosQuotaPrincipalKind.USER
            elif "group quotas" in lower:
                current_kind = MacosQuotaPrincipalKind.GROUP
            else:
                current_kind = None
            continue

        lowered = stripped.lower()
        if lowered.startswith("block grace") or lowered.startswith("inode grace"):
            continue
        if lowered.startswith("user") and "used" in lowered:
            continue
        if lowered.startswith("group") and "used" in lowered:
            continue
        if stripped.startswith("---"):
            continue
        if "Block limits" in stripped or "File limits" in stripped:
            continue

        tokens = stripped.split()
        if len(tokens) < 5:
            continue

        if current_kind is None:
            continue

        info = _build_quota_info_from_repquota_row(
            row=tokens,
            mountpoint=mountpoint,
            principal_kind=current_kind,
        )
        if info is not None:
            rtnval.append(info)

    return rtnval


def _build_quota_info_from_repquota_row(
    *,
    row: list[str],
    mountpoint: str,
    principal_kind: MacosQuotaPrincipalKind,
) -> Optional[MacosQuotaInfo]:
    """
        Map a single repquota data row into a
        :class:`MacosQuotaInfo`.

        :param row: The token list.
        :param mountpoint: Mountpoint to record.
        :param principal_kind: USER or GROUP.
    """
    principal_name = row[0]

    if len(row) >= 6 and _is_repquota_flag_token(token=row[1]):
        offset = 2
    else:
        offset = 1

    if len(row) < offset + 3:
        rtnval: Optional[MacosQuotaInfo] = None
        return rtnval

    bused_token = row[offset]
    bsoft_token = row[offset + 1]
    bhard_token = row[offset + 2]

    used_bytes = _kb_token_to_bytes(token=bused_token)
    soft_bytes = _kb_token_to_bytes(token=bsoft_token)
    hard_bytes = _kb_token_to_bytes(token=bhard_token)

    used_inodes = None
    soft_inodes = None
    hard_inodes = None
    grace_period_seconds = None

    remaining = row[offset + 3:]
    int_tokens: list[str] = []
    grace_tokens: list[str] = []
    for token in remaining:
        if _is_integer_token(token=token):
            int_tokens.append(token)
        else:
            grace_tokens.append(token)

    if len(int_tokens) >= 3:
        used_inodes = _int_token_or_none(token=int_tokens[0])
        soft_inodes = _int_token_or_none(token=int_tokens[1])
        hard_inodes = _int_token_or_none(token=int_tokens[2])

    if len(grace_tokens) > 0:
        grace_period_seconds = _grace_token_to_seconds(
            token=grace_tokens[0],
        )

    rtnval = MacosQuotaInfo(
        mountpoint=mountpoint,
        principal=principal_name,
        principal_kind=principal_kind,
        used_bytes=used_bytes,
        soft_bytes=soft_bytes,
        hard_bytes=hard_bytes,
        used_inodes=used_inodes,
        soft_inodes=soft_inodes,
        hard_inodes=hard_inodes,
        grace_period_seconds=grace_period_seconds,
    )
    return rtnval


def _parse_quotaon_status(
    *,
    stdout: str,
) -> tuple[bool, bool]:
    """
        Parse ``quotaon -p <mountpoint>`` output and return
        ``(user_active, group_active)``.

        :param stdout: Raw quotaon stdout.
    """
    user_active = False
    group_active = False

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip().lower()
        if len(stripped) == 0:
            continue

        last_token = stripped.split()[-1]
        is_on = last_token == "on"

        if stripped.startswith("user quota"):
            user_active = is_on
            continue
        if stripped.startswith("group quota"):
            group_active = is_on
            continue

    rtnval = (user_active, group_active)
    return rtnval


def _strip_warning_marker(*, token: str) -> str:
    """
        Strip the trailing ``*`` warning marker from a ``quota``
        used-blocks token.

        :param token: The raw token.
    """
    if token.endswith("*"):
        rtnval = token[:-1]
        return rtnval
    rtnval = token
    return rtnval


def _kb_token_to_bytes(*, token: str) -> Optional[int]:
    """
        Convert a 1-KiB block token to bytes.

        :param token: The raw token.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    try:
        block_value = int(token)
    except ValueError:
        rtnval = None
        return rtnval

    rtnval = block_value * _BLOCK_BYTES
    return rtnval


def _int_token_or_none(*, token: str) -> Optional[int]:
    """
        Parse ``token`` as an integer or return ``None``.

        :param token: The raw token.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    try:
        rtnval = int(token)
    except ValueError:
        rtnval = None
    return rtnval


def _grace_token_to_seconds(*, token: str) -> Optional[int]:
    """
        Convert a grace token (``"none"`` / ``"7days"`` /
        ``"6:23"``) into seconds.

        :param token: The raw token.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    lowered = token.lower()
    if lowered == "none":
        rtnval = None
        return rtnval

    suffix_map = {
        "days": 86400,
        "day": 86400,
        "hours": 3600,
        "hour": 3600,
        "minutes": 60,
        "minute": 60,
        "seconds": 1,
        "second": 1,
    }
    for suffix, multiplier in suffix_map.items():
        if lowered.endswith(suffix):
            number_part = lowered[: -len(suffix)]
            try:
                value = int(number_part)
            except ValueError:
                continue
            rtnval = value * multiplier
            return rtnval

    if ":" in lowered:
        hours_token, minutes_token = lowered.split(":", 1)
        try:
            hours_value = int(hours_token)
            minutes_value = int(minutes_token)
        except ValueError:
            rtnval = None
            return rtnval
        rtnval = hours_value * 3600 + minutes_value * 60
        return rtnval

    rtnval = None
    return rtnval


def _is_repquota_flag_token(*, token: str) -> bool:
    """
        Return True when ``token`` looks like a 2-char repquota
        over-grace flag (``"--"``, ``"+-"``, ``"-+"``, ``"++"``).

        :param token: The raw token.
    """
    if len(token) != 2:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in token:
        if ch not in "+-":
            rtnval = False
            break
    return rtnval


def _is_integer_token(*, token: str) -> bool:
    """
        Return True when ``token`` parses as a plain integer.

        :param token: The raw token.
    """
    if len(token) == 0:
        rtnval = False
        return rtnval

    if token[0] in "+-":
        body = token[1:]
    else:
        body = token

    if len(body) == 0:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in body:
        if not ch.isdigit():
            rtnval = False
            break
    return rtnval
