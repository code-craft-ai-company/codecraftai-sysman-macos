"""
    macOS filesystem-management feature.

    Implements the cross-OS filesystem-management contract for macOS
    using BSD ``mount`` / ``umount`` for runtime mounts, ``stat -f`` for
    per-path attributes, ``chown`` / ``chmod`` for permission writes,
    and read-modify-write of ``/etc/fstab`` via ``cat`` / ``tee``
    through the injected :class:`ICommandRunner`.

    File I/O policy:
        ``/etc/fstab`` is read with ``cat`` and written with ``tee``
        through the runner.  This module does NOT consume an
        :class:`IFileOperator`; every read / write / probe flows through
        the runner so dry-run, elevation, and aspect composition
        compose uniformly.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_mounts``, ``stat``, the idempotency probes) never
        elevate.

    Surface choices:
        * ``mount`` and ``unmount`` use the BSD ``mount`` / ``umount``
          binaries shipped on macOS.  ``diskutil mount`` is the
          preferred APFS surface but is non-portable across the
          cross-OS contract; the spec follow-up tracks the
          ``diskutil``-flavoured extension.
        * Persistent mounts flow through ``/etc/fstab``.  macOS reads
          ``/etc/fstab`` at boot, but the file is rarely populated on
          stock systems; this manager preserves the existing format
          (tab-separated ``device mountpoint fstype options dump
          pass``).
        * ``get_mounts`` parses the BSD ``mount`` output
          (``<device> on <mountpoint> (<fstype>, <options>)``).
          Lines whose mountpoint contains the literal string ``" on "``
          are correctly handled by anchoring on the LAST occurrence.
        * ``stat`` uses BSD-flavoured ``stat -f
          '%Su:%Sg:%Lp:%z:%Sm'`` (owner name, group name, mode in
          decimal-printed octal, size in bytes, modification timestamp
          string).  This differs from the GNU ``stat -c`` form used by
          the Linux peers; the format string is BSD-only.
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


_MOUNT = "mount"
_UMOUNT = "umount"
_STAT = "stat"
_CHOWN = "chown"
_CHMOD = "chmod"
_CAT = "cat"
_TEE = "tee"


# BSD ``stat -f`` format string.  Order matters: every parser below
# splits on this exact column ordering.
_STAT_FORMAT = "%Su:%Sg:%Lp:%z:%Sm"


MacosFilesystemActionStatus = ActionStatus


@dataclass
class MacosFilesystemManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosFilesystemManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param fstab_path: Path to the persistent mount table.
                           Defaults to ``/etc/fstab``; overridable for
                           testing and for unusual deployments.  macOS
                           reads ``/etc/fstab`` but rarely populates
                           it; this manager honours whatever path the
                           caller configures.
    """
    fstab_path: str = "/etc/fstab"


@dataclass
class MacosFilesystemActionResult(FeatureActionResult):
    """
        Result of a filesystem-management write operation.

        :param path: The target path, when applicable.  ``None`` for
                     verbs that target a mount-table entry by device.
        :param device: The device targeted, when applicable.
        :param mountpoint: The mountpoint targeted, when applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    device: Optional[str] = None
    mountpoint: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosFstabEntry:
    """
        Representation of a single ``/etc/fstab`` entry.

        :param device: Device or UUID specifier.
        :param mountpoint: Mount point path.
        :param fstype: Filesystem type.
        :param options: Mount options.  Defaults to ``"rw"`` (the BSD
                        default for non-virtual filesystems on macOS).
        :param dump: Dump field.  Defaults to ``0``.
        :param pass_: ``fsck`` pass-number field.  Trailing underscore
                      avoids the ``pass`` keyword.  Defaults to ``0``.
    """
    device: str
    mountpoint: str
    fstype: str
    options: str = "rw"
    dump: int = 0
    pass_: int = 0


@dataclass
class MacosMountInfo:
    """
        Information about an active mount as enumerated by
        :meth:`MacosFilesystemManager.get_mounts`.

        :param device: The backing device identifier (the token before
                       the literal ``" on "`` in the BSD ``mount``
                       output).
        :param mountpoint: The mount point path (the token between
                           ``" on "`` and the parenthesised attribute
                           list).
        :param fstype: The filesystem type from the parenthesised
                       attribute list (the first comma-separated
                       token).
        :param options: The remaining comma-separated tokens, joined
                        with ``", "``.
    """
    device: str
    mountpoint: str
    fstype: str
    options: str


@dataclass
class MacosFileInfo:
    """
        Information returned by :meth:`MacosFilesystemManager.stat`.

        :param path: The absolute path queried.
        :param owner: Owner name (BSD ``%Su``).
        :param group: Group name (BSD ``%Sg``).
        :param mode: Octal mode string (BSD ``%Lp`` — mode rendered as
                     decimal that, when interpreted as octal, gives the
                     mode bits).
        :param size: Size in bytes (BSD ``%z``).
        :param modified: Modification timestamp string (BSD ``%Sm``).
    """
    path: str
    owner: str
    group: str
    mode: str
    size: int
    modified: str


class MacosFilesystemQueryError(Exception):
    """
        Raised when a filesystem read fails for a reason other than
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

        err_msg_lines = ["Filesystem query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosFilesystemManager(BaseFeatureManager):
    """
        macOS filesystem manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; file I/O for ``/etc/fstab``
        is performed via ``cat`` / ``tee`` rather than a separate file
        operator.
    """

    result_class = MacosFilesystemActionResult
    status_class = MacosFilesystemActionStatus
    feature_name = "filesystem-management"

    # ------------------------------------------------------------------
    # Mount write verbs
    # ------------------------------------------------------------------

    def mount(
        self,
        device: str,
        mountpoint: str,
        fstype: str,
        options: Optional[str] = None,
    ) -> MacosFilesystemActionResult:
        """
            Mount ``device`` at ``mountpoint`` with type ``fstype``
            using BSD ``mount -t <fstype> [-o <options>] <device>
            <mountpoint>``.

            Skipped when an active mount already covers ``mountpoint``
            with the same device.

            :param device: The device to mount.
            :param mountpoint: The target directory.
            :param fstype: The filesystem type passed through ``-t``.
            :param options: Optional mount options passed through
                            ``-o``.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        command: list[str] = [_MOUNT, "-t", fstype]
        if options is not None and len(options) > 0:
            command.append("-o")
            command.append(options)
        command.append(device)
        command.append(mountpoint)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=command,
                path=mountpoint,
                device=device,
                mountpoint=mountpoint,
            )
            return dry

        if self._is_already_mounted(
            device=device,
            mountpoint=mountpoint,
        ):
            skipped = self._build_result(
                status=MacosFilesystemActionStatus.SKIPPED,
                command=command,
                path=mountpoint,
                device=device,
                mountpoint=mountpoint,
                message="device is already mounted at mountpoint",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=mountpoint,
            device=device,
            mountpoint=mountpoint,
        )
        return rtnval

    def unmount(
        self,
        mountpoint: str,
    ) -> MacosFilesystemActionResult:
        """
            Unmount ``mountpoint`` via ``umount <mountpoint>``.  Skipped
            when the mountpoint is not currently mounted.

            BSD ``umount`` is preferred over ``diskutil unmount`` for
            cross-OS parity; callers needing the diskutil flavour can
            override the verb on a subclass.

            :param mountpoint: The mount point to unmount.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        command = [_UMOUNT, mountpoint]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=command,
                path=mountpoint,
                mountpoint=mountpoint,
            )
            return dry

        if not self._is_mountpoint_mounted(mountpoint=mountpoint):
            skipped = self._build_result(
                status=MacosFilesystemActionStatus.SKIPPED,
                command=command,
                path=mountpoint,
                mountpoint=mountpoint,
                message="mountpoint is not currently mounted",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=mountpoint,
            mountpoint=mountpoint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # fstab write verbs
    # ------------------------------------------------------------------

    def add_fstab_entry(
        self,
        spec: MacosFstabEntry,
    ) -> MacosFilesystemActionResult:
        """
            Append ``spec`` to ``/etc/fstab`` when no matching entry
            already exists.

            Skipped when the file already contains a non-comment line
            whose ``device``, ``mountpoint``, ``fstype``, and
            ``options`` fields all match ``spec``.

            :param spec: The :class:`MacosFstabEntry` to append.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        rendered_line = _render_fstab_line(entry=spec)
        write_command = [_TEE, "-a", self._fstab_path()]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=write_command,
                path=self._fstab_path(),
                device=spec.device,
                mountpoint=spec.mountpoint,
            )
            return dry

        existing = self._read_fstab()
        if _fstab_contains_match(content=existing, spec=spec):
            skipped = self._build_result(
                status=MacosFilesystemActionStatus.SKIPPED,
                command=write_command,
                path=self._fstab_path(),
                device=spec.device,
                mountpoint=spec.mountpoint,
                message="fstab already contains a matching entry",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_line,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosFilesystemActionStatus.SUCCESS
        else:
            mapped_status = MacosFilesystemActionStatus.FAILED

        rtnval = MacosFilesystemActionResult(
            status=mapped_status,
            detail=spec.mountpoint,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            path=self._fstab_path(),
            device=spec.device,
            mountpoint=spec.mountpoint,
        )
        return rtnval

    def remove_fstab_entry(
        self,
        mountpoint: str,
    ) -> MacosFilesystemActionResult:
        """
            Remove every ``/etc/fstab`` line whose ``mountpoint`` field
            matches ``mountpoint``.  Comments and unrelated entries are
            preserved.

            Skipped when no matching entry exists.

            :param mountpoint: The mountpoint to evict from
                               ``/etc/fstab``.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        write_command = [_TEE, self._fstab_path()]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=write_command,
                path=self._fstab_path(),
                mountpoint=mountpoint,
            )
            return dry

        existing = self._read_fstab()
        new_content, removed = _fstab_filter_out_mountpoint(
            content=existing,
            mountpoint=mountpoint,
        )
        if not removed:
            skipped = self._build_result(
                status=MacosFilesystemActionStatus.SKIPPED,
                command=write_command,
                path=self._fstab_path(),
                mountpoint=mountpoint,
                message="fstab does not contain a matching entry",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )

        if run_result.exit_code == 0:
            mapped_status = MacosFilesystemActionStatus.SUCCESS
        else:
            mapped_status = MacosFilesystemActionStatus.FAILED

        rtnval = MacosFilesystemActionResult(
            status=mapped_status,
            detail=mountpoint,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            path=self._fstab_path(),
            mountpoint=mountpoint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Permissions write verbs
    # ------------------------------------------------------------------

    def chown(
        self,
        path: str,
        user: str,
        group: Optional[str] = None,
        recursive: bool = False,
    ) -> MacosFilesystemActionResult:
        """
            Change ``path``'s owner via BSD ``chown``.  When ``group`` is
            supplied the ``user:group`` shorthand is emitted (BSD chown
            accepts the same colon syntax as GNU chown).

            Skipped (non-recursive only) when ``stat`` shows that
            ``path`` already has the requested owner / group.
            Recursive invocations always run because per-entry
            comparison is out of scope for this verb.

            :param path: The target path.
            :param user: The desired owner name.
            :param group: Optional desired group name.
            :param recursive: True to add ``-R``.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        if group is not None:
            owner_token = "{}:{}".format(user, group)
        else:
            owner_token = user

        command: list[str] = [_CHOWN]
        if recursive:
            command.append("-R")
        command.append(owner_token)
        command.append(path)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        if not recursive:
            current = self.stat(path=path)
            if current is not None:
                owner_matches = current.owner == user
                if group is None:
                    group_matches = True
                else:
                    group_matches = current.group == group
                if owner_matches and group_matches:
                    skipped = self._build_result(
                        status=MacosFilesystemActionStatus.SKIPPED,
                        command=command,
                        path=path,
                        message="path already has the requested owner/group",
                    )
                    return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    def chmod(
        self,
        path: str,
        mode: str,
        recursive: bool = False,
    ) -> MacosFilesystemActionResult:
        """
            Change ``path``'s permissions via BSD ``chmod``.

            Skipped (non-recursive only) when ``stat`` shows that
            ``path`` already has the requested mode.  Recursive
            invocations always run.

            :param path: The target path.
            :param mode: The mode passed verbatim to ``chmod`` (numeric
                         like ``"0644"`` or symbolic like
                         ``"u+rwx"``).
            :param recursive: True to add ``-R``.

            :returns: The :class:`MacosFilesystemActionResult`.
        """
        command: list[str] = [_CHMOD]
        if recursive:
            command.append("-R")
        command.append(mode)
        command.append(path)

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosFilesystemActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        if not recursive:
            current = self.stat(path=path)
            if current is not None and _mode_matches(
                requested=mode,
                observed=current.mode,
            ):
                skipped = self._build_result(
                    status=MacosFilesystemActionStatus.SKIPPED,
                    command=command,
                    path=path,
                    message="path already has the requested mode",
                )
                return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mounts(self) -> list[MacosMountInfo]:
        """
            Enumerate active mounts via the BSD ``mount`` command (no
            arguments).

            Reads do not elevate.  When ``mount`` exits non-zero an
            empty list is returned.

            :returns: One :class:`MacosMountInfo` per parsed line.
        """
        command = [_MOUNT]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list[MacosMountInfo] = []
            return rtnval

        rtnval = _parse_bsd_mount_output(output=run_result.stdout)
        return rtnval

    def stat(
        self,
        path: str,
    ) -> Optional[MacosFileInfo]:
        """
            Return :class:`MacosFileInfo` for ``path`` or ``None`` when
            ``path`` does not exist.

            Runs ``stat -f '%Su:%Sg:%Lp:%z:%Sm' <path>`` (BSD-flavoured;
            the format string differs from the GNU peers).  Reads do
            not elevate.

            :param path: The absolute path to query.

            :returns: The parsed info or ``None``.
        """
        command = [_STAT, "-f", _STAT_FORMAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[MacosFileInfo] = None
            return rtnval

        rtnval = _parse_bsd_stat_output(output=run_result.stdout, path=path)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosFilesystemManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosFilesystemManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, MacosFilesystemManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosFilesystemManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _fstab_path(self) -> str:
        """
            Return the configured ``/etc/fstab`` path.
        """
        rtnval = self._typed_config().fstab_path
        return rtnval

    def _read_fstab(self) -> str:
        """
            Read ``/etc/fstab`` via ``cat``.  Reads never elevate.

            :returns: The file's contents on success; the empty string
                      when ``cat`` reports a non-zero exit code (path
                      missing or unreadable).
        """
        command = [_CAT, self._fstab_path()]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _is_mountpoint_mounted(self, *, mountpoint: str) -> bool:
        """
            Return True when ``mountpoint`` appears in ``get_mounts``.
        """
        mounts = self.get_mounts()
        rtnval = False
        for mount in mounts:
            if mount.mountpoint == mountpoint:
                rtnval = True
                break
        return rtnval

    def _is_already_mounted(
        self,
        *,
        device: str,
        mountpoint: str,
    ) -> bool:
        """
            Return True when an active mount matches ``device`` and
            ``mountpoint``.
        """
        mounts = self.get_mounts()
        rtnval = False
        for mount in mounts:
            if mount.mountpoint != mountpoint:
                continue
            if mount.device != device:
                continue
            rtnval = True
            break
        return rtnval

    def _dispatch_filesystem_write(
        self,
        *,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
    ) -> MacosFilesystemActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant filesystem-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="filesystem_write",
            path=path,
            device=device,
            mountpoint=mountpoint,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosFilesystemActionStatus,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosFilesystemActionResult:
        """
            Construct a :class:`MacosFilesystemActionResult` without
            invoking the runner.
        """
        if path is not None:
            detail_value = path
        else:
            detail_value = mountpoint
        rtnval = MacosFilesystemActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            path=path,
            device=device,
            mountpoint=mountpoint,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and helpers
# ----------------------------------------------------------------------


def _render_fstab_line(*, entry: MacosFstabEntry) -> str:
    """
        Render a :class:`MacosFstabEntry` into a single ``\\n``-
        terminated tab-separated line.
    """
    rtnval = "{}\t{}\t{}\t{}\t{}\t{}\n".format(
        entry.device,
        entry.mountpoint,
        entry.fstype,
        entry.options,
        entry.dump,
        entry.pass_,
    )
    return rtnval


def _fstab_contains_match(
    *,
    content: str,
    spec: MacosFstabEntry,
) -> bool:
    """
        Return True when ``content`` already contains a non-comment
        line that matches ``spec`` on device, mountpoint, fstype, and
        options.
    """
    rtnval = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue

        fields = stripped.split()
        if len(fields) < 4:
            continue

        line_device = fields[0]
        line_mountpoint = fields[1]
        line_fstype = fields[2]
        line_options = fields[3]

        if line_device != spec.device:
            continue
        if line_mountpoint != spec.mountpoint:
            continue
        if line_fstype != spec.fstype:
            continue
        if line_options != spec.options:
            continue

        rtnval = True
        break

    return rtnval


def _fstab_filter_out_mountpoint(
    *,
    content: str,
    mountpoint: str,
) -> tuple[str, bool]:
    """
        Return ``(new_content, removed)`` where ``new_content`` omits
        every non-comment line whose mountpoint field matches
        ``mountpoint`` and ``removed`` is True when at least one such
        line was filtered out.

        Comments and unrelated entries are preserved verbatim.
    """
    kept_lines: list[str] = []
    removed = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            kept_lines.append(raw_line)
            continue

        fields = stripped.split()
        if len(fields) >= 2 and fields[1] == mountpoint:
            removed = True
            continue

        kept_lines.append(raw_line)

    if len(kept_lines) == 0:
        joined = ""
    else:
        joined = "\n".join(kept_lines) + "\n"

    rtnval = (joined, removed)
    return rtnval


def _parse_bsd_mount_output(*, output: str) -> list[MacosMountInfo]:
    """
        Parse BSD ``mount`` output into :class:`MacosMountInfo` rows.

        Output shape::

            /dev/disk1s1 on / (apfs, local, journaled)
            map auto_home on /System/Volumes/Data/home (autofs, automounted)

        We anchor on the LAST occurrence of ``" on "`` rather than the
        first so device names that themselves contain ``" on "`` parse
        correctly.  The trailing parenthesised attribute list is
        comma-split; the first token is the fstype and the remainder
        joins back into ``options`` for the ``MacosMountInfo``.

        :param output: Raw stdout text.

        :returns: One :class:`MacosMountInfo` per parsed line.
    """
    rtnval: list[MacosMountInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if len(stripped) == 0:
            continue

        # Locate the trailing ``(<attrs>)`` block.  Without it we cannot
        # determine fstype / options and skip the line.
        open_paren = stripped.rfind("(")
        close_paren = stripped.rfind(")")
        if open_paren <= 0:
            continue
        if close_paren <= open_paren:
            continue

        head = stripped[:open_paren].rstrip()
        attrs_section = stripped[open_paren + 1:close_paren].strip()

        # Anchor on the LAST `` on `` so device names that contain
        # ``" on "`` literally are parsed correctly.
        anchor = " on "
        anchor_index = head.rfind(anchor)
        if anchor_index <= 0:
            continue

        device_value = head[:anchor_index].strip()
        mountpoint_value = head[anchor_index + len(anchor):].strip()
        if len(device_value) == 0:
            continue
        if len(mountpoint_value) == 0:
            continue

        attrs_tokens: list[str] = []
        for token in attrs_section.split(","):
            cleaned = token.strip()
            if len(cleaned) > 0:
                attrs_tokens.append(cleaned)

        if len(attrs_tokens) == 0:
            continue

        fstype_value = attrs_tokens[0]
        options_value = ", ".join(attrs_tokens[1:])

        info = MacosMountInfo(
            device=device_value,
            mountpoint=mountpoint_value,
            fstype=fstype_value,
            options=options_value,
        )
        rtnval.append(info)

    return rtnval


def _parse_bsd_stat_output(
    *,
    output: str,
    path: str,
) -> Optional[MacosFileInfo]:
    """
        Parse ``stat -f '%Su:%Sg:%Lp:%z:%Sm'`` output into a
        :class:`MacosFileInfo`.

        BSD ``%Sm`` (the modification timestamp) defaults to a
        human-readable form like ``"Apr 24 12:34:56 2026"`` (whitespace
        but no colons by default).  We split with ``maxsplit=4`` so any
        embedded colons in the modification field are preserved
        verbatim.

        :param output: Raw stdout from the BSD ``stat`` command.
        :param path: The path the stat was for (echoed onto the result).

        :returns: The parsed info, or ``None`` when the output is
                  empty.
    """
    line = output.strip()
    if len(line) == 0:
        rtnval: Optional[MacosFileInfo] = None
        return rtnval

    parts = line.split(":", 4)
    if len(parts) < 5:
        rtnval = None
        return rtnval

    try:
        size_value = int(parts[3])
    except ValueError:
        size_value = 0

    rtnval = MacosFileInfo(
        path=path,
        owner=parts[0],
        group=parts[1],
        mode=parts[2],
        size=size_value,
        modified=parts[4],
    )
    return rtnval


def _mode_matches(*, requested: str, observed: str) -> bool:
    """
        Return True when ``requested`` and ``observed`` mode strings
        denote the same numeric mode.

        BSD ``stat -f '%Lp'`` reports the mode bits as decimal-printed
        digits which, when interpreted as octal, give the mode.  This
        matches the GNU ``%a`` format closely enough that the same
        comparator can compare requested numeric modes (with or
        without a leading zero) against observed values.  Symbolic
        modes (``"u+rwx"``) cannot be compared without a full chmod
        evaluator and always return False so the caller falls through
        to the actual ``chmod`` invocation.

        :param requested: The mode the caller passed.
        :param observed: The mode reported by ``stat``.
    """
    if not _is_numeric_mode(token=requested):
        rtnval = False
        return rtnval
    if not _is_numeric_mode(token=observed):
        rtnval = False
        return rtnval

    requested_int = int(requested, 8)
    observed_int = int(observed, 8)
    rtnval = requested_int == observed_int
    return rtnval


def _is_numeric_mode(*, token: str) -> bool:
    """
        Return True when ``token`` consists only of octal digits.
    """
    if len(token) == 0:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in token:
        if ch not in "01234567":
            rtnval = False
            break
    return rtnval
