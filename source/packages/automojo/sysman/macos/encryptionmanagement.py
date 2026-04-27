"""
    macOS encryption-management feature.

    Implements the cross-OS encryption-management contract for macOS by
    wrapping the FileVault (`fdesetup`) and APFS-encryption
    (`diskutil apfs`) toolchains through the injected
    :class:`ICommandRunner`.

    Concept mapping:
        macOS has TWO disjoint encryption surfaces:

        * **FileVault (boot volume).**  The macOS boot volume is
          encrypted via the ``fdesetup`` command.  ``fdesetup enable
          -user <user>`` enables FileVault for an existing user;
          ``fdesetup disable`` decrypts it.  Status, recovery-key
          rotation, and recovery-key retrieval all go through
          ``fdesetup``.  The boot volume's APFS volume role is
          ``"System"`` or ``"Data"``; in either case ``fdesetup`` is
          the right tool.
        * **APFS data volumes.**  Non-boot APFS volumes encrypt via
          ``diskutil apfs encryptVolume <volume> -user <user>
          -passphrase <pw>`` (passphrase via stdin, not on argv).
          Status comes from ``diskutil info <volume>`` (the
          ``FileVault: Yes/No`` line).  Decryption is
          ``diskutil apfs decryptVolume <volume>``.  Unlock for a
          locked APFS volume is ``diskutil apfs unlockVolume``.

        The :class:`MacosEncryptionVolumeKind` enum carries which
        surface a given verb call targets; every verb branches on
        the kind to dispatch the right tool.

        See ``specs/3-encryption-management/design.md`` for the
        cross-OS encryption-tool concept-mapping table.

    Idempotency:
        Every write verb consults a read probe before invoking the
        runner:

        * ``enable_encryption`` — skipped when the boot volume
          already reports ``FileVault is On`` (boot kind) or when
          ``diskutil info`` already shows ``FileVault: Yes`` (data
          kind).
        * ``disable_encryption`` — skipped when the volume is
          already plaintext.  Destructive on data volumes
          (``diskutil apfs decryptVolume`` rewrites the disk in
          place); guarded by ``config.allow_destructive``.
        * ``unlock_volume`` — skipped when ``diskutil info`` shows
          ``Encrypted: Yes`` and ``Locked: No``.
        * ``rotate_key`` and ``backup_recovery_key`` — unconditional;
          rotation always changes the recorded recovery key, and a
          recovery-key dump at the time the operator asks for one is
          the value proposition of the verb.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_encryption_status``, ``list_encrypted_volumes``, the
        idempotency probes) never elevate.  ``fdesetup status`` is
        callable by any user on macOS; ``diskutil info`` likewise.

    Passphrase / recovery-key handling:
        Plaintext passphrases NEVER appear on argv.  Every
        passphrase flows through the runner's ``stdin`` parameter so
        the recorded ``command`` field on every result preserves
        audit fidelity without leaking secrets.  ``fdesetup`` and
        ``diskutil apfs`` both accept ``-stdinpass`` (FileVault) or
        a stdin prompt (``diskutil`` reads passphrases from stdin
        when the ``-passphrase`` flag is omitted).

    Destructive operations:
        ``disable_encryption`` on a DATA volume is destructive
        (``diskutil apfs decryptVolume`` rewrites every block).  The
        manager refuses to dispatch the command unless
        ``config.allow_destructive=True``; otherwise it returns
        ``FAILED`` carrying
        ``detail="destructive_operation_requires_explicit_confirm"``.
        Disabling FileVault on the BOOT volume is non-destructive in
        the sense that the data is preserved in plaintext; the same
        guard applies to keep cross-OS behaviour uniform.
"""

import json
import os
import plistlib

from dataclasses import dataclass, field
from enum import Enum
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


_FDESETUP = "fdesetup"
_DISKUTIL = "diskutil"


# Sentinel placed into the recorded ``detail`` field when the
# destructive ``disable_encryption`` verb is invoked without
# ``config.allow_destructive=True``.  Exposed as a module constant so
# tests and downstream coordinators can match against it without
# duplicating the literal.
_DESTRUCTIVE_GUARD_DETAIL = "destructive_operation_requires_explicit_confirm"


# Default cipher recorded onto status / result records when the
# underlying tool does not surface a cipher token.  FileVault and
# APFS both use ``aes-xts`` under the hood; the default exists so
# cross-OS coordinators can render a non-empty cipher field.
_DEFAULT_CIPHER = "aes-xts"


# Conventional path to the macOS boot volume.  The contract permits
# any volume identifier; this constant exists so the manager can
# detect a boot-volume verb call when the caller passes a relative
# token like ``"/"`` or ``"system"``.
_BOOT_VOLUME_PATHS = ("/", "/System", "system", "boot")


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


MacosEncryptionActionStatus = ActionStatus


class MacosEncryptionVolumeKind(Enum):
    """
        Which macOS encryption surface a verb call targets.

        ``BOOT`` — FileVault on the system boot volume.  Dispatches
        through ``fdesetup``.
        ``DATA`` — APFS encryption on a non-boot data volume.
        Dispatches through ``diskutil apfs``.
    """
    BOOT = "boot"
    DATA = "data"


@dataclass
class MacosEncryptionManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosEncryptionManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param allow_destructive: When True, ``disable_encryption`` is
                                  permitted to dispatch the underlying
                                  tool (``fdesetup disable`` or
                                  ``diskutil apfs decryptVolume``).
                                  Defaults to False so the destructive
                                  guard fires.
        :param default_cipher: Recorded onto status / result records
                               when the underlying tool does not
                               surface a cipher token.  Defaults to
                               :data:`_DEFAULT_CIPHER` (``"aes-xts"``);
                               FileVault and APFS encryption are both
                               fixed-cipher.
    """
    allow_destructive: bool = False
    default_cipher: str = _DEFAULT_CIPHER


@dataclass
class MacosEncryptionActionResult(FeatureActionResult):
    """
        Result of an encryption-management write operation.

        :param volume: Volume identifier the operation targeted (the
                       ``volume`` argument the caller passed —
                       ``"/Volumes/Data"``, ``"/"``, ``"disk2s5"``).
        :param device: Underlying APFS volume device path
                       (``/dev/disk2s5``) when resolvable, else
                       ``None``.
        :param requires_reboot: True when the operation modified the
                                boot volume's encryption posture
                                (FileVault enable / disable both
                                require a reboot to fully apply); False
                                for data-volume operations.
    """
    volume: Optional[str] = None
    device: Optional[str] = None
    requires_reboot: bool = False


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class MacosEncryptionSpec:
    """
        Specification for :meth:`MacosEncryptionManager.enable_encryption`.

        :param cipher: Reserved.  FileVault and APFS use a fixed
                       cipher (``aes-xts``); this field exists for
                       cross-OS shape parity and is ignored at
                       dispatch time.
        :param key_size: Reserved.  FileVault / APFS choose the key
                         size internally; ignored at dispatch time.
        :param passphrase: Plaintext passphrase used to bootstrap
                           encryption.  REQUIRED for data volumes
                           (``diskutil apfs encryptVolume`` consumes
                           it on stdin).  For boot volumes the
                           passphrase doubles as the recovery secret.
        :param recovery_key_protector: When True (default) the
                                       manager arranges for a
                                       personal recovery key to be
                                       generated.  Today this is a
                                       no-op on the data path; the
                                       boot path always emits one
                                       (``fdesetup enable`` returns
                                       it on stdout).
        :param mount_point: Optional intended mount point.  This
                            feature does NOT mount; the field is
                            preserved for round-trip compatibility
                            with cross-OS coordinators.
        :param user: macOS user account that owns the encryption
                     binding.  REQUIRED for boot volumes
                     (``fdesetup enable -user <user>``); REQUIRED for
                     data volumes (``-user <user>`` on
                     ``diskutil apfs encryptVolume``).
    """
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    passphrase: Optional[str] = None
    recovery_key_protector: bool = True
    mount_point: Optional[str] = None
    user: Optional[str] = None


@dataclass
class MacosEncryptionStatus:
    """
        Encryption status for a single volume.

        :param volume: Volume identifier.
        :param is_encrypted: True when FileVault / APFS encryption is
                             on for the volume.
        :param is_unlocked: True when the volume is currently
                            unlocked / mounted.  For boot volumes
                            this is always True while the system is
                            running.
        :param cipher: Cipher token (``"aes-xts"``).
        :param key_size: Reserved; the macOS tools do not surface a
                         key-size value.
        :param progress_percent: Percentage of an in-flight
                                 conversion (``fdesetup status``
                                 surfaces this when FileVault is mid-
                                 enable / mid-disable).
        :param protector_types: Strings describing the active key
                                holders.  For FileVault this is the
                                set of unlock-enabled users (``user``)
                                plus ``recovery`` when a personal
                                recovery key exists; for APFS this is
                                ``user`` plus ``passphrase``.
    """
    volume: str
    is_encrypted: bool
    is_unlocked: bool
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


@dataclass
class MacosEncryptedVolumeInfo:
    """
        Summary of a single encrypted volume on the host.

        :param volume: Mount point (``/Volumes/Data``) or volume name.
        :param device: APFS device path (``/dev/disk2s5``).
        :param mount_point: Mount point when mounted, else ``None``.
        :param is_encrypted: Always True for entries returned by
                             :meth:`MacosEncryptionManager.list_encrypted_volumes`.
        :param is_unlocked: True when the volume is currently
                            unlocked / mounted.
        :param cipher: Cipher token (``"aes-xts"``).
        :param key_size: Reserved.
        :param progress_percent: Reserved.
        :param protector_types: Strings describing the active key
                                holders.
    """
    volume: str
    device: str
    mount_point: Optional[str] = None
    is_encrypted: bool = True
    is_unlocked: bool = False
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class MacosEncryptionQueryError(Exception):
    """
        Raised when an encryption read fails for a reason other than
        the target simply being absent from the host's encryption
        surface.
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

        err_msg_lines = ["Encryption query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# MacosEncryptionManager
# ----------------------------------------------------------------------


class MacosEncryptionManager(BaseFeatureManager):
    """
        macOS encryption manager wrapping ``fdesetup`` (FileVault on
        the boot volume) and ``diskutil apfs`` (APFS encryption on
        data volumes) through an injected :class:`ICommandRunner`.
        See the module docstring for the boot-vs-data divergence,
        idempotency, elevation, and passphrase-handling contracts.
    """

    result_class = MacosEncryptionActionResult
    status_class = MacosEncryptionActionStatus
    feature_name = "encryption-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_encryption_status(
        self,
        volume: str,
    ) -> MacosEncryptionStatus:
        """
            Probe the encryption status of ``volume``.

            For the boot volume the manager dispatches
            ``fdesetup status`` and parses the canonical "FileVault
            is On." / "FileVault is Off." output.  For a data volume
            the manager dispatches ``diskutil info -plist <volume>``
            and parses the binary plist into ``Encryption`` /
            ``FileVault`` / ``Locked`` keys.

            :param volume: Mount point (``/`` for boot,
                           ``/Volumes/Data`` for a data volume), a
                           volume UUID, or a device node.

            :returns: The :class:`MacosEncryptionStatus`.
        """
        kind = self._classify_volume(volume=volume)
        if kind == MacosEncryptionVolumeKind.BOOT:
            rtnval = self._boot_status(volume=volume)
        else:
            rtnval = self._data_status(volume=volume)
        return rtnval

    def list_encrypted_volumes(self) -> list:
        """
            Enumerate every encrypted APFS volume on the host.

            Dispatches ``diskutil apfs list -plist`` and walks the
            resulting plist tree, collecting volumes whose
            ``Encryption`` key is True.  The boot volume is always
            included when FileVault is on.

            :returns: A list (possibly empty) of
                      :class:`MacosEncryptedVolumeInfo`.
        """
        command = [_DISKUTIL, "apfs", "list", "-plist"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_apfs_list_plist(output=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_encryption(
        self,
        volume: str,
        spec: MacosEncryptionSpec,
    ) -> MacosEncryptionActionResult:
        """
            Enable FileVault (boot) or APFS encryption (data) on
            ``volume``.

            For the boot volume::

                fdesetup enable -user <user> -inputplist

            with an XML plist on stdin carrying the user's password
            and the personal-recovery flag.  ``fdesetup`` returns the
            generated personal recovery key on stdout.

            For a data volume::

                diskutil apfs encryptVolume <volume> -user <user>

            with the passphrase delivered via runner stdin.

            Idempotent: when the volume is already encrypted the
            operation returns ``SKIPPED``.

            :param volume: Mount point or device path.
            :param spec: The :class:`MacosEncryptionSpec`.

            :returns: The :class:`MacosEncryptionActionResult`.
        """
        kind = self._classify_volume(volume=volume)
        device_path = self._resolve_device(volume=volume)

        if kind == MacosEncryptionVolumeKind.BOOT:
            command = [
                _FDESETUP, "enable",
                "-user", spec.user or "",
                "-inputplist",
            ]
        else:
            command = [
                _DISKUTIL, "apfs", "encryptVolume",
                volume,
                "-user", spec.user or "",
            ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=(kind == MacosEncryptionVolumeKind.BOOT),
                detail=volume,
            )
            return dry

        if spec.passphrase is None:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="passphrase_required",
                stderr="passphrase is required for enable_encryption",
            )
            return failed

        if spec.user is None or len(spec.user) == 0:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="user_required",
                stderr=(
                    "spec.user is required for enable_encryption on macOS"
                ),
            )
            return failed

        already = self._is_encrypted(volume=volume, kind=kind)
        if already:
            skipped = self._build_result(
                status=MacosEncryptionActionStatus.SKIPPED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="volume {} is already encrypted".format(volume),
            )
            return skipped

        if kind == MacosEncryptionVolumeKind.BOOT:
            stdin_payload = _build_fdesetup_inputplist(
                user=spec.user, passphrase=spec.passphrase,
            )
            requires_reboot = True
        else:
            stdin_payload = spec.passphrase
            requires_reboot = False

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=stdin_payload,
            volume=volume,
            device=device_path,
            requires_reboot=requires_reboot,
        )
        return rtnval

    def disable_encryption(
        self,
        volume: str,
    ) -> MacosEncryptionActionResult:
        """
            Disable FileVault (boot) or APFS encryption (data) on
            ``volume``.

            For boot::

                fdesetup disable

            For data::

                diskutil apfs decryptVolume <volume>

            Both paths are gated by
            ``config.allow_destructive=True``.  ``decryptVolume``
            rewrites every block on the volume and is the destructive
            form; ``fdesetup disable`` is non-destructive but is
            guarded uniformly so cross-OS behaviour stays consistent.

            Idempotent: when the volume is already plaintext the
            operation returns ``SKIPPED``.

            :param volume: Mount point or device path.

            :returns: The :class:`MacosEncryptionActionResult`.
        """
        kind = self._classify_volume(volume=volume)
        device_path = self._resolve_device(volume=volume)

        if kind == MacosEncryptionVolumeKind.BOOT:
            command = [_FDESETUP, "disable"]
            requires_reboot = True
        else:
            command = [_DISKUTIL, "apfs", "decryptVolume", volume]
            requires_reboot = False

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=requires_reboot,
                detail=volume,
            )
            return dry

        already = self._is_encrypted(volume=volume, kind=kind)
        if not already:
            skipped = self._build_result(
                status=MacosEncryptionActionStatus.SKIPPED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="volume {} is not encrypted".format(volume),
            )
            return skipped

        allow_destructive = self._typed_config().allow_destructive
        if not allow_destructive:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail=_DESTRUCTIVE_GUARD_DETAIL,
                stderr=(
                    "disable_encryption is destructive; set "
                    "config.allow_destructive=True to proceed"
                ),
            )
            return failed

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=None,
            volume=volume,
            device=device_path,
            requires_reboot=requires_reboot,
        )
        return rtnval

    def rotate_key(
        self,
        volume: str,
    ) -> MacosEncryptionActionResult:
        """
            Rotate the recovery key on ``volume``.

            Boot path::

                fdesetup changerecovery -personal

            ``fdesetup`` returns the new personal recovery key on
            stdout.  Data-volume rotation is a limited surface on
            macOS (``diskutil apfs`` does not expose a
            change-passphrase verb that does not require both old and
            new keys); the manager dispatches the same fdesetup
            command and returns FAILED for data-volume calls until a
            future enhancement lands.

            :param volume: Mount point or device path.

            :returns: The :class:`MacosEncryptionActionResult`.
        """
        kind = self._classify_volume(volume=volume)
        device_path = self._resolve_device(volume=volume)

        command = [_FDESETUP, "changerecovery", "-personal"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail=volume,
            )
            return dry

        if kind == MacosEncryptionVolumeKind.DATA:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="rotate_key_unsupported_for_data_volume_on_macos",
                stderr=(
                    "rotate_key is only supported for the boot volume "
                    "on macOS"
                ),
            )
            return failed

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=None,
            volume=volume,
            device=device_path,
            requires_reboot=False,
        )
        return rtnval

    def backup_recovery_key(
        self,
        volume: str,
        path: str,
    ) -> MacosEncryptionActionResult:
        """
            Back up the recovery key for ``volume`` to ``path``.

            Boot path::

                fdesetup showrecoverykey

            ``fdesetup showrecoverykey`` prints the personal recovery
            key to stdout (when one exists).  This verb captures
            stdout from the runner result and persists it to ``path``
            via the runner using ``tee``::

                tee <path>

            with the recovery-key string on stdin.  The manager
            never holds the recovery key in a Python-string variable
            beyond the local frame.

            Data-volume backup is not supported by macOS native
            tooling; the manager returns FAILED for data-volume
            calls.

            :param volume: Mount point or device path (boot only).
            :param path: Destination filesystem path for the
                         recovery-key file.

            :returns: The :class:`MacosEncryptionActionResult`.
        """
        kind = self._classify_volume(volume=volume)
        device_path = self._resolve_device(volume=volume)

        command = [_FDESETUP, "showrecoverykey"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail=path,
            )
            return dry

        if kind == MacosEncryptionVolumeKind.DATA:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="backup_recovery_key_unsupported_for_data_volume_on_macos",
                stderr=(
                    "backup_recovery_key is only supported for the boot "
                    "volume on macOS"
                ),
            )
            return failed

        if self._config.elevated:
            run_elevated = True
        else:
            run_elevated = False

        run_result = self._runner.run(
            list(command), elevated=run_elevated, stdin=None,
        )
        if run_result.exit_code != 0:
            failed = self._build_result(
                status=MacosEncryptionActionStatus.FAILED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="fdesetup_showrecoverykey_failed",
                stderr=run_result.stderr,
            )
            failed.exit_code = run_result.exit_code
            failed.stdout = run_result.stdout
            return failed

        recovery_key_text = run_result.stdout.strip()
        tee_command = ["tee", path]
        tee_result = self._runner.run(
            list(tee_command),
            elevated=run_elevated,
            stdin=recovery_key_text,
        )

        if tee_result.exit_code == 0:
            mapped_status = MacosEncryptionActionStatus.SUCCESS
        else:
            mapped_status = MacosEncryptionActionStatus.FAILED

        rtnval = MacosEncryptionActionResult(
            status=mapped_status,
            detail=path,
            exit_code=tee_result.exit_code,
            stdout=tee_result.stdout,
            stderr=tee_result.stderr,
            command=list(command),
            volume=volume,
            device=device_path,
            requires_reboot=False,
        )
        return rtnval

    def unlock_volume(
        self,
        volume: str,
        passphrase: str,
    ) -> MacosEncryptionActionResult:
        """
            Unlock an encrypted APFS volume.

            Dispatches::

                diskutil apfs unlockVolume <volume>

            with the passphrase delivered via runner stdin
            (``diskutil`` reads the passphrase interactively when
            ``-passphrase`` is omitted; the runner pipes the passphrase
            on stdin to satisfy that prompt without ever placing the
            value on argv).

            Boot-volume unlock is implicit (the volume is unlocked
            by the bootloader / SecureToken flow); calling
            ``unlock_volume`` for the boot path is a no-op that
            returns SKIPPED.

            Idempotent: when ``diskutil info`` reports
            ``Locked: No`` the operation returns ``SKIPPED``.

            :param volume: Mount point or device path.
            :param passphrase: Plaintext passphrase.

            :returns: The :class:`MacosEncryptionActionResult`.
        """
        kind = self._classify_volume(volume=volume)
        device_path = self._resolve_device(volume=volume)

        command = [_DISKUTIL, "apfs", "unlockVolume", volume]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail=volume,
            )
            return dry

        if kind == MacosEncryptionVolumeKind.BOOT:
            skipped = self._build_result(
                status=MacosEncryptionActionStatus.SKIPPED,
                command=command,
                volume=volume,
                device=device_path,
                requires_reboot=False,
                detail="boot_volume_unlock_is_implicit",
            )
            return skipped

        info = self._diskutil_info(volume=volume)
        if info is not None:
            locked_value = info.get("Locked")
            if isinstance(locked_value, bool) and not locked_value:
                skipped = self._build_result(
                    status=MacosEncryptionActionStatus.SKIPPED,
                    command=command,
                    volume=volume,
                    device=device_path,
                    requires_reboot=False,
                    detail="volume {} is already unlocked".format(volume),
                )
                return skipped

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=passphrase,
            volume=volume,
            device=device_path,
            requires_reboot=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — config, identity, dispatch
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosEncryptionManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`MacosEncryptionManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, MacosEncryptionManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosEncryptionManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _classify_volume(
        self, *, volume: str,
    ) -> MacosEncryptionVolumeKind:
        """
            Decide whether ``volume`` targets the boot volume
            (FileVault / ``fdesetup``) or a data volume (APFS /
            ``diskutil apfs``).
        """
        if not isinstance(volume, str):
            rtnval = MacosEncryptionVolumeKind.DATA
            return rtnval
        normalized = volume.strip()
        if normalized in _BOOT_VOLUME_PATHS:
            rtnval = MacosEncryptionVolumeKind.BOOT
            return rtnval
        if normalized.lower() == "boot":
            rtnval = MacosEncryptionVolumeKind.BOOT
            return rtnval
        rtnval = MacosEncryptionVolumeKind.DATA
        return rtnval

    def _resolve_device(self, *, volume: str) -> Optional[str]:
        """
            Resolve a device path for ``volume`` when the value is
            obviously a device, else ``None``.

            Today the resolver is a string-pattern check; a future
            enhancement would call ``diskutil info -plist`` to
            resolve mount points to device nodes.
        """
        if isinstance(volume, str) and volume.startswith("/dev/"):
            rtnval: Optional[str] = volume
            return rtnval
        rtnval = None
        return rtnval

    def _is_encrypted(
        self,
        *,
        volume: str,
        kind: MacosEncryptionVolumeKind,
    ) -> bool:
        """
            Probe whether ``volume`` is currently encrypted.
        """
        if kind == MacosEncryptionVolumeKind.BOOT:
            command = [_FDESETUP, "status"]
            run_result = self._runner.run(list(command), elevated=False)
            if run_result.exit_code != 0:
                rtnval = False
                return rtnval
            stdout_text = run_result.stdout
            if "FileVault is On" in stdout_text:
                rtnval = True
            else:
                rtnval = False
            return rtnval

        info = self._diskutil_info(volume=volume)
        if info is None:
            rtnval = False
            return rtnval
        encryption_value = info.get("Encryption")
        filevault_value = info.get("FileVault")
        if isinstance(encryption_value, bool) and encryption_value:
            rtnval = True
        elif isinstance(filevault_value, bool) and filevault_value:
            rtnval = True
        else:
            rtnval = False
        return rtnval

    def _diskutil_info(self, *, volume: str) -> Optional[dict]:
        """
            Run ``diskutil info -plist <volume>`` and return the
            parsed plist as a dict, or ``None`` on failure.
        """
        command = [_DISKUTIL, "info", "-plist", volume]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_plist_dict(output=run_result.stdout)
        return rtnval

    def _boot_status(self, *, volume: str) -> MacosEncryptionStatus:
        """
            Build the :class:`MacosEncryptionStatus` for the boot
            volume.
        """
        command = [_FDESETUP, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = MacosEncryptionStatus(
                volume=volume,
                is_encrypted=False,
                is_unlocked=False,
                cipher=None,
                key_size=None,
                progress_percent=None,
                protector_types=[],
            )
            return rtnval

        is_encrypted, progress = _parse_fdesetup_status(
            output=run_result.stdout,
        )
        if is_encrypted:
            cipher_value: Optional[str] = self._typed_config().default_cipher
            protectors = ["user", "recovery"]
        else:
            cipher_value = None
            protectors = []

        rtnval = MacosEncryptionStatus(
            volume=volume,
            is_encrypted=is_encrypted,
            is_unlocked=True,
            cipher=cipher_value,
            key_size=None,
            progress_percent=progress,
            protector_types=protectors,
        )
        return rtnval

    def _data_status(self, *, volume: str) -> MacosEncryptionStatus:
        """
            Build the :class:`MacosEncryptionStatus` for a data
            volume.
        """
        info = self._diskutil_info(volume=volume)
        if info is None:
            rtnval = MacosEncryptionStatus(
                volume=volume,
                is_encrypted=False,
                is_unlocked=False,
                cipher=None,
                key_size=None,
                progress_percent=None,
                protector_types=[],
            )
            return rtnval

        encryption_value = info.get("Encryption")
        filevault_value = info.get("FileVault")
        if isinstance(encryption_value, bool):
            is_encrypted = encryption_value
        elif isinstance(filevault_value, bool):
            is_encrypted = filevault_value
        else:
            is_encrypted = False

        locked_value = info.get("Locked")
        if isinstance(locked_value, bool):
            is_unlocked = not locked_value
        else:
            is_unlocked = True

        if is_encrypted:
            cipher_value: Optional[str] = self._typed_config().default_cipher
            protectors = ["user", "passphrase"]
        else:
            cipher_value = None
            protectors = []

        rtnval = MacosEncryptionStatus(
            volume=volume,
            is_encrypted=is_encrypted,
            is_unlocked=is_unlocked,
            cipher=cipher_value,
            key_size=None,
            progress_percent=None,
            protector_types=protectors,
        )
        return rtnval

    def _dispatch_encryption_write(
        self,
        *,
        command: list,
        stdin: Optional[str],
        volume: Optional[str] = None,
        device: Optional[str] = None,
        requires_reboot: bool = False,
        detail: Optional[str] = None,
    ) -> MacosEncryptionActionResult:
        """
            Dispatch an encryption-management write through the runner
            and wrap the result with the ``volume`` / ``device`` /
            ``requires_reboot`` fields.
        """
        if detail is None:
            detail_value: Optional[str] = volume
        else:
            detail_value = detail
        rtnval = self._dispatch(
            command=command,
            detail=detail_value,
            stdin=stdin,
            operation="encryption_write",
            volume=volume,
            device=device,
            requires_reboot=requires_reboot,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosEncryptionActionStatus,
        command: list,
        volume: Optional[str] = None,
        device: Optional[str] = None,
        requires_reboot: bool = False,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosEncryptionActionResult:
        """
            Construct a :class:`MacosEncryptionActionResult` without
            invoking the runner (used by SKIPPED / DRY_RUN /
            safety-rail paths).
        """
        rtnval = MacosEncryptionActionResult(
            status=status,
            detail=detail,
            command=list(command),
            volume=volume,
            device=device,
            stderr=stderr,
            requires_reboot=requires_reboot,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — input plist + parsers
# ----------------------------------------------------------------------


def _build_fdesetup_inputplist(
    *,
    user: str,
    passphrase: str,
) -> str:
    """
        Build the XML plist payload that ``fdesetup enable -inputplist``
        consumes on stdin.

        ``fdesetup`` accepts a username / password dictionary on
        stdin to avoid the interactive prompt; this helper renders
        the canonical shape::

            <plist><dict>
                <key>Username</key><string>...</string>
                <key>Password</key><string>...</string>
            </dict></plist>

        :param user: macOS account name.
        :param passphrase: Plaintext passphrase.

        :returns: The plist XML string.
    """
    payload = {
        "Username": user,
        "Password": passphrase,
    }
    rendered = plistlib.dumps(payload).decode("utf-8")
    rtnval = rendered
    return rtnval


def _parse_fdesetup_status(
    *,
    output: str,
) -> tuple:
    """
        Parse ``fdesetup status`` output.

        ``fdesetup status`` prints one of::

            FileVault is On.
            FileVault is Off.
            FileVault is On.
            Encryption in progress: Percent completed = 47.
            Decryption in progress: Percent completed = 12.

        :param output: Raw stdout text.

        :returns: A 2-tuple of (is_encrypted: bool,
                                progress_percent: float | None).
    """
    is_encrypted = False
    progress_percent: Optional[float] = None
    for raw_line in output.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        if "FileVault is On" in line_text:
            is_encrypted = True
            continue
        if "FileVault is Off" in line_text:
            is_encrypted = False
            continue
        if "Percent completed" in line_text:
            try:
                head, tail = line_text.rsplit("=", 1)
                value = tail.strip().rstrip(".")
                progress_percent = float(value)
            except (ValueError, AttributeError):
                progress_percent = None
    rtnval = (is_encrypted, progress_percent)
    return rtnval


def _parse_plist_dict(*, output: str) -> Optional[dict]:
    """
        Parse a plist payload into a dict.

        ``diskutil info -plist`` emits XML-format plist output by
        default; this parser tolerates both XML and binary plist
        bytes via :mod:`plistlib`.

        :param output: Raw stdout text.

        :returns: The parsed plist dict, or ``None`` on parse error.
    """
    if output is None:
        rtnval: Optional[dict] = None
        return rtnval
    text = output.strip()
    if len(text) == 0:
        rtnval = None
        return rtnval
    try:
        parsed = plistlib.loads(text.encode("utf-8"))
    except (ValueError, plistlib.InvalidFileException):
        rtnval = None
        return rtnval
    if not isinstance(parsed, dict):
        rtnval = None
        return rtnval
    rtnval = parsed
    return rtnval


def _parse_apfs_list_plist(*, output: str) -> list:
    """
        Parse ``diskutil apfs list -plist`` output and emit one
        :class:`MacosEncryptedVolumeInfo` per encrypted volume.

        The plist tree shape is::

            Containers: [
                Volumes: [
                    {
                        DeviceIdentifier: "disk2s5",
                        Encryption: True/False,
                        FileVault: True/False,
                        Locked: True/False,
                        Name: "Data",
                        MountPoint: "/Volumes/Data",
                        ...
                    },
                    ...
                ],
                ...
            ]

        :param output: Raw stdout text.

        :returns: A list (possibly empty) of
                  :class:`MacosEncryptedVolumeInfo`.
    """
    rtnval: list = []
    parsed = _parse_plist_dict(output=output)
    if parsed is None:
        return rtnval
    containers = parsed.get("Containers")
    if not isinstance(containers, list):
        return rtnval
    for container in containers:
        if not isinstance(container, dict):
            continue
        volumes = container.get("Volumes")
        if not isinstance(volumes, list):
            continue
        for volume_entry in volumes:
            if not isinstance(volume_entry, dict):
                continue
            encryption_value = volume_entry.get("Encryption")
            filevault_value = volume_entry.get("FileVault")
            if isinstance(encryption_value, bool) and encryption_value:
                is_encrypted = True
            elif isinstance(filevault_value, bool) and filevault_value:
                is_encrypted = True
            else:
                is_encrypted = False
            if not is_encrypted:
                continue
            device_id = volume_entry.get("DeviceIdentifier", "")
            if isinstance(device_id, str) and len(device_id) > 0:
                device_path = "/dev/{}".format(device_id)
            else:
                device_path = ""
            mount_point_value = volume_entry.get("MountPoint")
            if (
                isinstance(mount_point_value, str)
                and len(mount_point_value) > 0
            ):
                mp_value: Optional[str] = mount_point_value
            else:
                mp_value = None
            name_value = volume_entry.get("Name", "")
            if isinstance(name_value, str) and len(name_value) > 0:
                volume_label: str = name_value
            elif mp_value is not None:
                volume_label = mp_value
            else:
                volume_label = device_path
            locked_value = volume_entry.get("Locked")
            if isinstance(locked_value, bool):
                is_unlocked = not locked_value
            else:
                is_unlocked = True
            info = MacosEncryptedVolumeInfo(
                volume=volume_label,
                device=device_path,
                mount_point=mp_value,
                is_encrypted=True,
                is_unlocked=is_unlocked,
                cipher=_DEFAULT_CIPHER,
                key_size=None,
                progress_percent=None,
                protector_types=["user", "passphrase"],
            )
            rtnval.append(info)
    return rtnval
