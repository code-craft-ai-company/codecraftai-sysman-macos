"""
    macOS storage-management feature.

    Implements the cross-OS storage-management contract for macOS using
    the ``diskutil`` family (``diskutil list``, ``diskutil info``,
    ``diskutil apfs list / addVolume / deleteVolume / createContainer
    / deleteContainer / resizeContainer``) plus ``plutil`` to convert
    the ``-plist`` output into JSON.  Every verb is dispatched through
    the injected :class:`ICommandRunner`.

    Concept mapping:
        The cross-OS contract talks about *pools* and *volumes*.  On
        macOS these map to APFS concepts:

        * **Pool** — APFS **Container**
          (``diskutil apfs createContainer`` /
          ``diskutil apfs deleteContainer``).
        * **Volume** — APFS **Volume**
          (``diskutil apfs addVolume`` /
          ``diskutil apfs deleteVolume`` /
          ``diskutil apfs resizeContainer``).
        * **Disk** — a whole-disk node reported by
          ``diskutil list -plist`` (``disk0``, ``disk1`` etc).

        APFS containers have a key property the Linux LVM stack does
        not: every volume in a container shares the container's free
        space dynamically.  ``resize_volume`` on macOS therefore
        resizes the *container*, not the volume — the contract verb is
        the same but the underlying tool changes.  The implementation
        documents this explicitly so a coordinator that drives
        ``resize_volume`` on a multi-volume container understands that
        every sibling volume's effective ceiling moves with it.

        See ``specs/3-storage-management/design.md`` for the full
        concept-mapping table.

    Volume identifier syntax:
        Volumes are identified by the APFS volume's BSD device id
        (``disk1s2``).  ``diskutil`` accepts either the bare id or the
        ``/dev/diskNsM`` form; this implementation passes the bare id
        for cross-OS-contract parity with the Windows ``DriveLetter``
        convention.

    Pool identifier syntax:
        Containers are identified by their APFS reference
        (``disk1`` for a container at ``/dev/disk1``).  The contract's
        ``pool_name`` parameter is taken to be that reference.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``list_disks``, ``list_volumes``, ``get_volume_info``,
        ``get_disk_info``, all idempotency probes) never elevate.

    Filesystem creation, mounting, and ``/etc/fstab`` editing live in
    ``filesystem-management``; this feature stops at the volume
    boundary.  ``diskutil apfs addVolume`` does create an APFS
    filesystem on the volume by definition (APFS volumes are
    inseparable from their filesystem) but the contract verb is
    ``create_volume`` not ``create_filesystem``; ``filesystem-management``
    on macOS handles the mount step.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level command tokens
# ----------------------------------------------------------------------


_DISKUTIL = "diskutil"
_PLUTIL = "plutil"


# Tolerance in bytes for `resize_volume`.  APFS allocates in 4 KiB
# blocks; we pick 1 MiB to be conservative.  See ``design.md`` for the
# rationale.
_RESIZE_TOLERANCE_BYTES = 1 * 1024 * 1024


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


MacosStorageActionStatus = ActionStatus


@dataclass
class MacosStorageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosStorageManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param default_filesystem: Filesystem hint recorded onto
                                   :class:`MacosVolumeSpec` when the
                                   caller does not override.  APFS is
                                   the default; ``"hfs"``,
                                   ``"jhfs+"``, and ``"exfat"`` are
                                   accepted by ``diskutil apfs
                                   addVolume`` (the type token is
                                   passed through verbatim).
        :param default_container_id: Optional default container id used
                                     when an :class:`MacosVolumeSpec`
                                     omits ``pool``.  Useful for
                                     systems where every volume lives
                                     in the same APFS container; left
                                     as ``None`` so the absence is
                                     surfaced as a typed FAILED result
                                     rather than silently routed to a
                                     hard-coded default.
    """
    default_filesystem: str = "apfs"
    default_container_id: Optional[str] = None


@dataclass
class MacosStorageActionResult(FeatureActionResult):
    """
        Result of a storage-management write operation.

        :param target: Generic target identifier (volume id, container
                       id, or disk node), populated for every result so
                       cross-OS coordinators can index uniformly.
        :param pool: The container id targeted, when applicable.
        :param volume: The volume id targeted, when applicable.
        :param message: Human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        on delete and remove-from-pool verbs).
    """
    target: Optional[str] = None
    pool: Optional[str] = None
    volume: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosDiskInfo:
    """
        Information about a single physical disk.

        :param id: Stable disk identifier (``disk0``).
        :param name: Display name (``Macintosh HD`` if available, else
                     the device id).
        :param size_bytes: Total disk size in bytes.
        :param model: Device model string (``MediaName`` from
                      ``diskutil info``).
        :param serial: Device serial when available.
        :param partition_style: Partition scheme (``GUID_partition_scheme``
                                / ``Apple_partition_scheme`` / ``Master
                                Boot Record`` / ``unknown``).
        :param health_status: Best-effort SMART hint
                              (``SMARTStatus``) when available.
        :param mount_points: Every mountpoint reachable from any
                             descendant volume on the disk.
        :param is_pool_member: True when the disk hosts an APFS
                               Physical Store (i.e. it backs an APFS
                               container).
    """
    id: str
    name: str
    size_bytes: int
    model: Optional[str] = None
    serial: Optional[str] = None
    partition_style: Optional[str] = None
    health_status: Optional[str] = None
    mount_points: list = field(default_factory=list)
    is_pool_member: bool = False


@dataclass
class MacosVolumeInfo:
    """
        Information about a single volume — APFS, HFS+, or other.

        :param id: Stable volume identifier (``disk1s2``).
        :param name: Volume name (e.g. ``Macintosh HD``).
        :param size_bytes: Allocated size in bytes.  For APFS volumes
                           this is the volume's *used* allocation;
                           callers wanting the container's free pool
                           should consult :meth:`get_disk_info` on the
                           container reference.
        :param used_bytes: Used bytes when reported, else ``None``.
        :param filesystem: Filesystem type (``apfs``, ``hfs``,
                           ``exfat``, ``msdos``, ...).
        :param mount_point: Active mountpoint when mounted.
        :param pool: Container id (``disk1``) for APFS volumes; ``None``
                     for non-APFS volumes.
        :param health_status: Volume status when reported.
    """
    id: str
    name: str
    size_bytes: int
    used_bytes: Optional[int] = None
    filesystem: Optional[str] = None
    mount_point: Optional[str] = None
    pool: Optional[str] = None
    health_status: Optional[str] = None


@dataclass
class MacosPoolSpec:
    """
        Specification for :meth:`MacosStorageManager.create_pool`.

        :param name: Recorded for cross-OS-contract parity.
                     ``diskutil apfs createContainer`` does NOT accept
                     a name — APFS containers are identified by the
                     allocated device id, not by a caller-chosen
                     string.  The name is preserved on the result for
                     the coordinator's audit log.
        :param disks: Disk identifiers to include at creation
                      (``["disk2", "disk3"]``).
        :param resiliency: Recorded for cross-OS-contract parity but
                           ignored on macOS — APFS container
                           resiliency is Apple-managed.
    """
    name: str
    disks: list
    resiliency: Optional[str] = None


@dataclass
class MacosVolumeSpec:
    """
        Specification for :meth:`MacosStorageManager.create_volume`.

        :param name: Volume name (the second positional argument to
                     ``diskutil apfs addVolume``).
        :param size_bytes: Requested size hint.  Mapped onto ``-size
                           <bytes>B``.  APFS volumes share the
                           container's free pool, so this is an upper
                           bound rather than a fixed allocation.
        :param pool: Container id (``disk1``).  Required.  When
                     ``None`` the manager falls back to the config's
                     ``default_container_id``; if that is also ``None``
                     the call returns a typed FAILED result.
        :param filesystem: Filesystem token passed through to
                           ``diskutil apfs addVolume`` (``apfs``,
                           ``hfs``, ``jhfs+``, ``exfat``, ``msdos``).
                           Defaults to ``"apfs"``.
    """
    name: str
    size_bytes: int
    pool: Optional[str] = None
    filesystem: str = "apfs"


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosStorageQueryError(Exception):
    """
        Raised when a storage read fails for a reason other than the
        target simply being absent.
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
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Storage query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# MacosStorageManager
# ----------------------------------------------------------------------


class MacosStorageManager(BaseFeatureManager):
    """
        macOS storage manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`.  See the module docstring for
        the APFS concept mapping and elevation policy.
    """

    result_class = MacosStorageActionResult
    status_class = MacosStorageActionStatus
    feature_name = "storage-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_disks(self) -> list:
        """
            Enumerate physical disks via ``diskutil list -plist
            physical`` piped through ``plutil`` for JSON conversion.

            :returns: One :class:`MacosDiskInfo` per whole-disk entry.
                      Returns an empty list when the read fails or when
                      the JSON is malformed.
        """
        run_result = self._run_diskutil_json(
            command=[_DISKUTIL, "list", "-plist", "physical"],
        )
        if run_result is None:
            rtnval: list = []
            return rtnval
        rtnval = _parse_diskutil_list_output(payload=run_result)
        return rtnval

    def list_volumes(self) -> list:
        """
            Enumerate volumes via ``diskutil apfs list -plist`` (APFS)
            merged with ``diskutil list -plist`` (HFS+ and other
            non-APFS volumes).

            :returns: A list of :class:`MacosVolumeInfo`.  APFS volumes
                      come first, then non-APFS volumes from the
                      ``diskutil list`` walk that were not already
                      surfaced by the APFS probe.
        """
        apfs_payload = self._run_diskutil_json(
            command=[_DISKUTIL, "apfs", "list", "-plist"],
        )
        list_payload = self._run_diskutil_json(
            command=[_DISKUTIL, "list", "-plist"],
        )

        if apfs_payload is None:
            apfs_volumes: list = []
        else:
            apfs_volumes = _parse_diskutil_apfs_list(payload=apfs_payload)

        if list_payload is None:
            non_apfs_volumes: list = []
        else:
            seen_ids = {v.id for v in apfs_volumes}
            non_apfs_volumes = _parse_diskutil_list_volumes(
                payload=list_payload, exclude_ids=seen_ids,
            )

        rtnval = list(apfs_volumes)
        rtnval.extend(non_apfs_volumes)
        return rtnval

    def get_volume_info(
        self,
        volume_id: str,
    ) -> Optional[MacosVolumeInfo]:
        """
            Return the :class:`MacosVolumeInfo` matching ``volume_id``,
            or ``None`` when no such volume exists.

            :param volume_id: APFS / HFS volume id (``disk1s2``).

            :returns: The matching info, or ``None``.
        """
        all_volumes = self.list_volumes()
        rtnval: Optional[MacosVolumeInfo] = None
        for volume in all_volumes:
            if volume.id == volume_id:
                rtnval = volume
                break
        return rtnval

    def get_disk_info(
        self,
        disk_id: str,
    ) -> Optional[MacosDiskInfo]:
        """
            Return the :class:`MacosDiskInfo` matching ``disk_id``, or
            ``None`` when no such disk exists.

            :param disk_id: ``disk0``, ``disk1``, etc.

            :returns: The matching info, or ``None``.
        """
        all_disks = self.list_disks()
        rtnval: Optional[MacosDiskInfo] = None
        for disk in all_disks:
            if disk.id == disk_id:
                rtnval = disk
                break
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — volumes
    # ------------------------------------------------------------------

    def create_volume(
        self,
        spec: MacosVolumeSpec,
    ) -> MacosStorageActionResult:
        """
            Create an APFS volume via ``diskutil apfs addVolume
            <container> <fstype> <name> -size <bytes>B``.

            Idempotent: when an APFS volume with the same name already
            exists in the same container with a matching size (within
            :data:`_RESIZE_TOLERANCE_BYTES`), returns ``SKIPPED``.

            :param spec: The :class:`MacosVolumeSpec` describing the
                         desired volume.

            :returns: The :class:`MacosStorageActionResult`.
        """
        container_id = self._resolve_container(pool=spec.pool)
        if container_id is None:
            failed = self._build_result(
                status=MacosStorageActionStatus.FAILED,
                command=[],
                target=spec.name,
                pool=None,
                volume=spec.name,
                message=(
                    "MacosVolumeSpec.pool is required (no "
                    "default_container_id configured)"
                ),
                stderr="missing pool",
            )
            return failed

        size_token = "{}B".format(spec.size_bytes)
        command = [
            _DISKUTIL, "apfs", "addVolume",
            container_id, spec.filesystem, spec.name,
            "-size", size_token,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                pool=container_id,
                volume=spec.name,
            )
            return dry

        existing = self._lookup_volume_by_name(
            container_id=container_id, name=spec.name,
        )
        if existing is not None:
            size_delta = abs(existing.size_bytes - spec.size_bytes)
            if size_delta <= _RESIZE_TOLERANCE_BYTES:
                skipped = self._build_result(
                    status=MacosStorageActionStatus.SKIPPED,
                    command=command,
                    target=existing.id,
                    pool=container_id,
                    volume=spec.name,
                    message=(
                        "APFS volume {} already exists in container "
                        "{}".format(spec.name, container_id)
                    ),
                )
                return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=spec.name,
            pool=container_id,
            volume=spec.name,
        )
        return rtnval

    def delete_volume(
        self,
        volume_id: str,
    ) -> MacosStorageActionResult:
        """
            Delete an APFS volume via ``diskutil apfs deleteVolume
            <volume_id>``.

            Idempotent: when no volume with ``volume_id`` exists,
            returns ``SKIPPED``.  On ``SUCCESS`` the result's
            ``message`` carries a data-loss advisory.

            :param volume_id: APFS volume id (``disk1s2``).

            :returns: The :class:`MacosStorageActionResult`.
        """
        command = [_DISKUTIL, "apfs", "deleteVolume", volume_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=volume_id,
                volume=volume_id,
            )
            return dry

        existing = self._lookup_volume_by_id(volume_id=volume_id)
        if existing is None:
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=volume_id,
                volume=volume_id,
                message="APFS volume {} does not exist".format(volume_id),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=volume_id,
            pool=existing.pool,
            volume=volume_id,
        )
        if result.status == MacosStorageActionStatus.SUCCESS:
            advisory = (
                "APFS volume {} was deleted; all data on the volume "
                "has been destroyed".format(volume_id)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def resize_volume(
        self,
        volume_id: str,
        size_bytes: int,
    ) -> MacosStorageActionResult:
        """
            Resize the APFS *container* that hosts ``volume_id`` via
            ``diskutil apfs resizeContainer <container> <size>``.

            Note carefully: APFS volumes share their container's free
            space dynamically.  There is no per-volume resize on APFS
            — resizing the container is the closest analogue.  Every
            sibling volume's effective ceiling moves with the
            container's new size.  See ``design.md`` § "APFS shared
            sizing" for the full discussion.

            For HFS+ volumes the implementation falls back to
            ``diskutil resizeVolume <volume_id> <size>``.

            Idempotent: when the resolved target's current size is
            within :data:`_RESIZE_TOLERANCE_BYTES` of ``size_bytes``,
            returns ``SKIPPED``.

            :param volume_id: APFS / HFS volume id.
            :param size_bytes: Requested new container size in bytes.

            :returns: The :class:`MacosStorageActionResult`.
        """
        existing = self._lookup_volume_by_id(volume_id=volume_id)
        size_token = "{}B".format(size_bytes)

        if existing is not None and existing.pool is not None:
            container_id = existing.pool
            command = [
                _DISKUTIL, "apfs", "resizeContainer",
                container_id, size_token,
            ]
            target_value = container_id
        else:
            container_id = None
            command = [_DISKUTIL, "resizeVolume", volume_id, size_token]
            target_value = volume_id

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=target_value,
                pool=container_id,
                volume=volume_id,
            )
            return dry

        if existing is None:
            failed = self._build_result(
                status=MacosStorageActionStatus.FAILED,
                command=command,
                target=volume_id,
                volume=volume_id,
                message="volume {} does not exist".format(volume_id),
                stderr="missing volume",
            )
            return failed

        size_delta = abs(existing.size_bytes - size_bytes)
        if size_delta <= _RESIZE_TOLERANCE_BYTES:
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=target_value,
                pool=container_id,
                volume=volume_id,
                message=(
                    "volume {} already at {} bytes".format(
                        volume_id, existing.size_bytes,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=target_value,
            pool=container_id,
            volume=volume_id,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — pools (containers)
    # ------------------------------------------------------------------

    def create_pool(
        self,
        spec: MacosPoolSpec,
    ) -> MacosStorageActionResult:
        """
            Create an APFS container via ``diskutil apfs
            createContainer <disks...>``.

            Idempotent: when one of the listed disks is already a
            Physical Store of an existing container, returns
            ``SKIPPED`` and surfaces the existing container's id on
            ``target``.

            :param spec: The :class:`MacosPoolSpec` describing the
                         container's member disks.

            :returns: The :class:`MacosStorageActionResult`.
        """
        command = (
            [_DISKUTIL, "apfs", "createContainer"] + list(spec.disks)
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                pool=spec.name,
            )
            return dry

        existing_container = self._lookup_container_for_disks(
            disks=spec.disks,
        )
        if existing_container is not None:
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=existing_container,
                pool=existing_container,
                message=(
                    "APFS container {} already includes one of the "
                    "requested disks".format(existing_container)
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=spec.name,
            pool=spec.name,
        )
        return rtnval

    def delete_pool(
        self,
        pool_name: str,
    ) -> MacosStorageActionResult:
        """
            Delete an APFS container via ``diskutil apfs
            deleteContainer <pool_name>``.

            Idempotent: when no container with that id exists, returns
            ``SKIPPED``.  On ``SUCCESS`` the result's ``message``
            carries a data-loss advisory.

            :param pool_name: APFS container id (``disk1``).

            :returns: The :class:`MacosStorageActionResult`.
        """
        command = [_DISKUTIL, "apfs", "deleteContainer", pool_name]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=pool_name,
                pool=pool_name,
            )
            return dry

        if not self._container_exists(container_id=pool_name):
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=pool_name,
                pool=pool_name,
                message=(
                    "APFS container {} does not exist".format(pool_name)
                ),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=pool_name,
            pool=pool_name,
        )
        if result.status == MacosStorageActionStatus.SUCCESS:
            advisory = (
                "APFS container {} was deleted; every volume in the "
                "container has been destroyed".format(pool_name)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def add_to_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> MacosStorageActionResult:
        """
            Add ``disk_id`` to an APFS container via ``diskutil apfs
            resizeContainer <pool> 0 <new-disk>`` (the magic ``0``
            tells diskutil to keep the container's current size while
            adding the new physical store).

            Idempotent: when ``disk_id`` is already a Physical Store of
            ``pool_name``, returns ``SKIPPED``.

            :param pool_name: APFS container id.
            :param disk_id: Disk identifier (``disk2``).

            :returns: The :class:`MacosStorageActionResult`.
        """
        command = [
            _DISKUTIL, "apfs", "resizeContainer",
            pool_name, "0", disk_id,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if self._disk_in_container(
            container_id=pool_name, disk_id=disk_id,
        ):
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is already a physical store of {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=disk_id,
            pool=pool_name,
        )
        return rtnval

    def remove_from_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> MacosStorageActionResult:
        """
            Remove ``disk_id`` from an APFS container via ``diskutil
            apfs resizeContainer <pool> 0 -none <disk_id>``.

            Documented limitation: APFS containers do not generally
            permit removing a physical store while the container is in
            use.  ``diskutil`` enforces the constraint server-side; on
            failure this method surfaces the runner's stderr verbatim.
            Coordinators that need guaranteed eviction should drain
            volumes first.

            Idempotent: when ``disk_id`` is not currently a Physical
            Store of ``pool_name``, returns ``SKIPPED``.  On
            ``SUCCESS`` the result's ``message`` carries a data-loss
            advisory.

            :param pool_name: APFS container id.
            :param disk_id: Disk identifier.

            :returns: The :class:`MacosStorageActionResult`.
        """
        command = [
            _DISKUTIL, "apfs", "resizeContainer",
            pool_name, "0", "-none", disk_id,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosStorageActionStatus.DRY_RUN,
                command=command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if not self._disk_in_container(
            container_id=pool_name, disk_id=disk_id,
        ):
            skipped = self._build_result(
                status=MacosStorageActionStatus.SKIPPED,
                command=command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is not a physical store of {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=disk_id,
            pool=pool_name,
        )
        if result.status == MacosStorageActionStatus.SUCCESS:
            advisory = (
                "disk {} was removed from APFS container {}; the "
                "physical-store association has been wiped".format(
                    disk_id, pool_name,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosStorageManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosStorageManagerConfig`.
        """
        if isinstance(self._config, MacosStorageManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosStorageManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_container(self, *, pool: Optional[str]) -> Optional[str]:
        """
            Resolve a caller-supplied pool argument to a concrete
            container id, falling back to the config's
            ``default_container_id`` when absent.
        """
        if pool is not None and len(pool) > 0:
            rtnval: Optional[str] = pool
            return rtnval
        rtnval = self._typed_config().default_container_id
        return rtnval

    def _run_diskutil_json(self, *, command: list) -> Optional[dict]:
        """
            Run ``command`` (which MUST already include ``-plist``) and
            convert its plist output to JSON via ``plutil -convert
            json -o - -``.  Returns the parsed dict, or ``None`` on any
            failure.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval

        plutil_command = [_PLUTIL, "-convert", "json", "-o", "-", "-"]
        convert_result = self._runner.run(
            list(plutil_command),
            elevated=False,
            stdin=run_result.stdout,
        )
        if convert_result.exit_code != 0:
            rtnval = None
            return rtnval

        text = convert_result.stdout.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval

        try:
            rtnval = json.loads(text)
        except (ValueError, TypeError):
            rtnval = None
        return rtnval

    def _lookup_volume_by_name(
        self,
        *,
        container_id: str,
        name: str,
    ) -> Optional[MacosVolumeInfo]:
        """
            Probe ``diskutil apfs list`` for a single volume by container
            id + name.
        """
        all_volumes = self.list_volumes()
        rtnval: Optional[MacosVolumeInfo] = None
        for volume in all_volumes:
            if volume.pool != container_id:
                continue
            if volume.name != name:
                continue
            rtnval = volume
            break
        return rtnval

    def _lookup_volume_by_id(
        self,
        *,
        volume_id: str,
    ) -> Optional[MacosVolumeInfo]:
        """
            Probe ``diskutil apfs list`` (and fall back to ``diskutil
            list``) for a single volume by id.
        """
        rtnval = self.get_volume_info(volume_id=volume_id)
        return rtnval

    def _container_exists(self, *, container_id: str) -> bool:
        """
            Return True when ``diskutil apfs list`` reports a container
            with the given id.
        """
        payload = self._run_diskutil_json(
            command=[_DISKUTIL, "apfs", "list", "-plist"],
        )
        if payload is None:
            rtnval = False
            return rtnval
        rtnval = _container_in_apfs_payload(
            payload=payload, container_id=container_id,
        )
        return rtnval

    def _disk_in_container(
        self,
        *,
        container_id: str,
        disk_id: str,
    ) -> bool:
        """
            Return True when ``diskutil apfs list`` reports ``disk_id``
            as a Physical Store of ``container_id``.
        """
        payload = self._run_diskutil_json(
            command=[_DISKUTIL, "apfs", "list", "-plist"],
        )
        if payload is None:
            rtnval = False
            return rtnval
        rtnval = _disk_in_apfs_container(
            payload=payload,
            container_id=container_id,
            disk_id=disk_id,
        )
        return rtnval

    def _lookup_container_for_disks(
        self,
        *,
        disks: list,
    ) -> Optional[str]:
        """
            Return the id of an APFS container that already includes
            any of ``disks`` as a Physical Store, or ``None``.
        """
        payload = self._run_diskutil_json(
            command=[_DISKUTIL, "apfs", "list", "-plist"],
        )
        rtnval: Optional[str] = None
        if payload is None:
            return rtnval
        rtnval = _first_container_for_any_disk(
            payload=payload, disks=disks,
        )
        return rtnval

    def _dispatch_storage_write(
        self,
        *,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> MacosStorageActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant storage-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="storage_write",
            target=target,
            pool=pool,
            volume=volume,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosStorageActionStatus,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosStorageActionResult:
        """
            Construct a :class:`MacosStorageActionResult` without
            invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = target
        rtnval = MacosStorageActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            target=target,
            pool=pool,
            volume=volume,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and helpers
# ----------------------------------------------------------------------


def _coerce_int(*, raw_value) -> int:
    """
        Best-effort coercion to int.  Returns ``0`` on failure.
    """
    if raw_value is None:
        rtnval = 0
        return rtnval
    if isinstance(raw_value, bool):
        rtnval = 0
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if len(text) == 0:
            rtnval = 0
            return rtnval
        try:
            rtnval = int(text)
        except ValueError:
            rtnval = 0
        return rtnval
    rtnval = 0
    return rtnval


def _coerce_optional_int(*, raw_value):
    """
        Like :func:`_coerce_int` but returns ``None`` on failure.
    """
    if raw_value is None:
        rtnval = None
        return rtnval
    if isinstance(raw_value, bool):
        rtnval = None
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if len(text) == 0:
            rtnval = None
            return rtnval
        try:
            rtnval = int(text)
        except ValueError:
            rtnval = None
        return rtnval
    rtnval = None
    return rtnval


def _parse_diskutil_list_output(*, payload: dict) -> list:
    """
        Parse ``diskutil list -plist physical`` JSON output into
        :class:`MacosDiskInfo` rows.

        The payload shape (plist→json) carries:
        - ``"AllDisks"``: list of every disk id (``"disk0"``, etc).
        - ``"AllDisksAndPartitions"``: list of dicts with
          ``DeviceIdentifier``, ``Size``, ``Content``, plus a nested
          ``Partitions`` list.

        :param payload: Parsed JSON (already converted from plist).

        :returns: A list (possibly empty) of :class:`MacosDiskInfo`.
    """
    rtnval: list = []
    all_entries = payload.get("AllDisksAndPartitions", [])
    if not isinstance(all_entries, list):
        return rtnval

    for entry in all_entries:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("DeviceIdentifier", "")
        if len(device_id) == 0:
            continue

        size_bytes = _coerce_int(raw_value=entry.get("Size", 0))
        content = entry.get("Content", "")
        partitions = entry.get("Partitions", [])

        # Walk partitions for descendant mountpoints and APFS-stores.
        mount_points: list = []
        is_pool_member = False
        if isinstance(partitions, list):
            for part in partitions:
                if not isinstance(part, dict):
                    continue
                part_mount = part.get("MountPoint")
                if isinstance(part_mount, str) and len(part_mount) > 0:
                    mount_points.append(part_mount)
                if part.get("Content") == "Apple_APFS":
                    is_pool_member = True

        if isinstance(content, str) and len(content) > 0:
            partition_style: Optional[str] = content
        else:
            partition_style = None

        info = MacosDiskInfo(
            id=device_id,
            name=device_id,
            size_bytes=size_bytes,
            model=None,
            serial=None,
            partition_style=partition_style,
            health_status=None,
            mount_points=mount_points,
            is_pool_member=is_pool_member,
        )
        rtnval.append(info)
    return rtnval


def _parse_diskutil_apfs_list(*, payload: dict) -> list:
    """
        Parse ``diskutil apfs list -plist`` JSON output into
        :class:`MacosVolumeInfo` rows.

        Shape (plist→json):
        - ``"Containers"``: list of container dicts, each with:
            - ``"ContainerReference"``: ``"disk1"``.
            - ``"Volumes"``: list of volume dicts with
              ``DeviceIdentifier``, ``Name``, ``CapacityCeiling``,
              ``CapacityInUse``, ``MountPoint``, ``MountedStatus``,
              ``FilesystemType``, etc.
            - ``"PhysicalStores"``: list of physical-store dicts.

        :param payload: Parsed JSON.

        :returns: One :class:`MacosVolumeInfo` per APFS volume.
    """
    rtnval: list = []
    containers = payload.get("Containers", [])
    if not isinstance(containers, list):
        return rtnval

    for container in containers:
        if not isinstance(container, dict):
            continue
        container_ref = container.get("ContainerReference", "")
        volumes = container.get("Volumes", [])
        if not isinstance(volumes, list):
            continue

        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            volume_id = volume.get("DeviceIdentifier", "")
            if len(volume_id) == 0:
                continue
            name_value = volume.get("Name", volume_id)
            size_bytes = _coerce_int(
                raw_value=volume.get("CapacityCeiling", 0),
            )
            used_bytes = _coerce_optional_int(
                raw_value=volume.get("CapacityInUse"),
            )
            mount_raw = volume.get("MountPoint")
            if isinstance(mount_raw, str) and len(mount_raw) > 0:
                mount_value: Optional[str] = mount_raw
            else:
                mount_value = None
            fs_value = volume.get("FilesystemType")
            if isinstance(fs_value, str) and len(fs_value) > 0:
                filesystem_value: Optional[str] = fs_value
            else:
                filesystem_value = "apfs"

            info = MacosVolumeInfo(
                id=volume_id,
                name=name_value,
                size_bytes=size_bytes,
                used_bytes=used_bytes,
                filesystem=filesystem_value,
                mount_point=mount_value,
                pool=container_ref if len(container_ref) > 0 else None,
                health_status=None,
            )
            rtnval.append(info)
    return rtnval


def _parse_diskutil_list_volumes(
    *,
    payload: dict,
    exclude_ids: set,
) -> list:
    """
        Walk ``diskutil list -plist`` output for non-APFS volumes
        (HFS+, MS-DOS, etc.) and emit :class:`MacosVolumeInfo` rows for
        any partition whose id is NOT already in ``exclude_ids``.

        :param payload: Parsed JSON.
        :param exclude_ids: Volume ids to skip (already surfaced from
                            the APFS probe).

        :returns: The list of supplemental volumes.
    """
    rtnval: list = []
    all_entries = payload.get("AllDisksAndPartitions", [])
    if not isinstance(all_entries, list):
        return rtnval
    for entry in all_entries:
        if not isinstance(entry, dict):
            continue
        partitions = entry.get("Partitions", [])
        if not isinstance(partitions, list):
            continue
        for part in partitions:
            if not isinstance(part, dict):
                continue
            part_id = part.get("DeviceIdentifier", "")
            if len(part_id) == 0:
                continue
            if part_id in exclude_ids:
                continue
            content = part.get("Content", "")
            # Skip Apple_APFS partitions — they are containers, not
            # volumes; their volumes are surfaced by the APFS probe.
            if content == "Apple_APFS":
                continue
            size_bytes = _coerce_int(raw_value=part.get("Size", 0))
            mount_raw = part.get("MountPoint")
            if isinstance(mount_raw, str) and len(mount_raw) > 0:
                mount_value: Optional[str] = mount_raw
            else:
                mount_value = None
            name_raw = part.get("VolumeName", part_id)
            if isinstance(name_raw, str) and len(name_raw) > 0:
                name_value = name_raw
            else:
                name_value = part_id
            info = MacosVolumeInfo(
                id=part_id,
                name=name_value,
                size_bytes=size_bytes,
                used_bytes=None,
                filesystem=content if len(content) > 0 else None,
                mount_point=mount_value,
                pool=None,
                health_status=None,
            )
            rtnval.append(info)
    return rtnval


def _container_in_apfs_payload(
    *,
    payload: dict,
    container_id: str,
) -> bool:
    """
        Return True when ``payload`` (already parsed JSON from
        ``diskutil apfs list -plist``) contains a container whose
        ``ContainerReference`` matches ``container_id``.
    """
    containers = payload.get("Containers", [])
    if not isinstance(containers, list):
        rtnval = False
        return rtnval
    rtnval = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        if container.get("ContainerReference", "") == container_id:
            rtnval = True
            break
    return rtnval


def _disk_in_apfs_container(
    *,
    payload: dict,
    container_id: str,
    disk_id: str,
) -> bool:
    """
        Return True when ``payload`` reports ``disk_id`` as a Physical
        Store of ``container_id``.

        Physical-store ids look like ``"disk2s2"`` (a partition on a
        physical disk).  This helper matches both the bare disk id
        (``disk2``) and the trailing-numeric form by stripping
        partition suffixes.
    """
    containers = payload.get("Containers", [])
    if not isinstance(containers, list):
        rtnval = False
        return rtnval
    target_disk = _strip_partition_suffix(text=disk_id)
    rtnval = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        if container.get("ContainerReference", "") != container_id:
            continue
        stores = container.get("PhysicalStores", [])
        if not isinstance(stores, list):
            continue
        for store in stores:
            if not isinstance(store, dict):
                continue
            store_id = store.get("DeviceIdentifier", "")
            if store_id == disk_id:
                rtnval = True
                break
            if _strip_partition_suffix(text=store_id) == target_disk:
                rtnval = True
                break
        if rtnval:
            break
    return rtnval


def _first_container_for_any_disk(
    *,
    payload: dict,
    disks: list,
) -> Optional[str]:
    """
        Return the ``ContainerReference`` of the first container in
        ``payload`` whose Physical Stores include any of ``disks`` (or
        any partition on those disks); ``None`` when no container
        matches.
    """
    containers = payload.get("Containers", [])
    if not isinstance(containers, list):
        rtnval: Optional[str] = None
        return rtnval
    target_disks = {_strip_partition_suffix(text=d) for d in disks}
    rtnval = None
    for container in containers:
        if not isinstance(container, dict):
            continue
        stores = container.get("PhysicalStores", [])
        if not isinstance(stores, list):
            continue
        for store in stores:
            if not isinstance(store, dict):
                continue
            store_id = store.get("DeviceIdentifier", "")
            stripped = _strip_partition_suffix(text=store_id)
            if store_id in disks or stripped in target_disks:
                rtnval = container.get("ContainerReference", "")
                if isinstance(rtnval, str) and len(rtnval) > 0:
                    return rtnval
                rtnval = None
    return rtnval


def _strip_partition_suffix(*, text: str) -> str:
    """
        Strip any trailing ``sN`` (where N is one or more digits) from
        ``text``.  ``"disk2s2"`` -> ``"disk2"``; ``"disk0"`` ->
        ``"disk0"``; ``""`` -> ``""``.
    """
    if len(text) == 0:
        rtnval = text
        return rtnval
    # Find the LAST 's' with digits after it.
    index = len(text) - 1
    while index >= 0 and text[index].isdigit():
        index -= 1
    if index < 0:
        rtnval = text
        return rtnval
    if text[index] != "s":
        rtnval = text
        return rtnval
    if index == 0:
        rtnval = text
        return rtnval
    rtnval = text[:index]
    return rtnval
