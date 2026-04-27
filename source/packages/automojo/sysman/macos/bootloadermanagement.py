"""
    macOS bootloader-management feature.

    Implements the cross-OS bootloader-management contract for macOS.
    macOS does not expose a multi-entry boot menu the way GRUB or
    systemd-boot does.  The native primitives are:

        * ``bless`` -- marks (blesses) a volume as bootable and
          optionally selects it as the active boot device.
        * ``nvram`` (read-only here) -- exposes the current
          ``boot-args`` payload and other firmware variables that
          influence boot behaviour.
        * ``/Library/Preferences/SystemConfiguration/com.apple.Boot.plist``
          -- pre-Apple-Silicon root-disk loader configuration; honoured
          by Intel Macs.

    Implication for the cross-OS contract:

        * "Boot entries" on macOS are blessed volumes, not menu
          items.  ``list_entries`` enumerates the canonical signed
          system volume plus any additional volume the operator has
          explicitly blessed.
        * ``add_entry`` accepts only ``MacosBootEntryKind.BLESS_VOLUME``
          specs; other kinds are rejected with ``FAILED``.
        * ``update_bootloader_config`` and ``regenerate_config`` are
          OS-managed -- macOS rebuilds its own loader caches when
          ``bless`` runs.  Both verbs return a ``SUCCESS`` no-op
          carrying a descriptive ``message``.

    **Important distinction.**  This feature is NOT
    ``boot-management``.  ``boot-management`` (in
    ``automojo-sysman-uefi``) addresses **firmware-level UEFI boot
    entries**; on Apple Silicon there is no UEFI surface at all and
    on Intel Macs the firmware boot entry is owned by Apple's iBoot
    rather than user-space tools.  This feature addresses the
    **OS-level bless / NVRAM-boot-args layer that sits between the
    firmware and the kernel**.  The two layers are independent.

    File I/O policy:
        Every command is dispatched through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator` so dry-run / elevation / aspect
        composition compose uniformly.

    Elevation policy:
        Every write verb (``set_default_entry``, ``add_entry``,
        ``remove_entry``) honours ``config.elevated`` -- ``bless``
        requires admin to mutate firmware boot device hints.  Reads
        (``list_entries``, ``get_default_entry``) never elevate.

    Destructive nature:
        ``bless`` writes affect the firmware boot device hint and a
        misconfiguration can cause the next boot to land on an
        unsigned, sealed, or absent volume.  Callers SHOULD verify
        the resulting state through ``get_default_entry`` and SHOULD
        keep a known-good fallback volume blessed before any change.
"""

import os
import re

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_BLESS = "bless"
_NVRAM = "nvram"

_NOOP_UPDATE_MESSAGE = (
    "macOS bootloader is OS-managed; no user-driven update step "
    "exists. update_bootloader_config / regenerate_config are no-ops."
)


MacosBootloaderActionStatus = ActionStatus


class MacosBootEntryKind(Enum):
    """
        macOS bootloader-entry kinds.

        Only ``BLESS_VOLUME`` results in an actual write.  Other
        kinds are listed for cross-OS contract conformance and the
        manager rejects ``add_entry`` requests for them with a typed
        ``FAILED`` result.
    """
    BLESS_VOLUME  = "bless_volume"
    KERNEL_ENTRY  = "kernel_entry"
    LOADER_ENTRY  = "loader_entry"


@dataclass
class MacosBootloaderManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosBootloaderManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param default_volume: Optional override for the canonical
                               root volume path used by
                               ``list_entries`` when ``nvram`` and
                               ``bless --info --getBoot`` cannot
                               resolve a current boot device.  When
                               ``None``, the manager treats an
                               unresolved boot volume as an empty
                               entry list rather than synthesising
                               one.
    """
    default_volume: Optional[str] = None


@dataclass
class MacosBootloaderActionResult(FeatureActionResult):
    """
        Result of a bootloader-management write operation.

        :param entry_id: The bootloader entry identifier acted upon,
                         when applicable.  For ``BLESS_VOLUME``
                         entries this is the absolute volume path.
        :param requires_reboot: ``True`` when the change takes
                                effect only after the next boot.
                                ``bless`` writes set this to ``True``.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` / no-op outcomes.
    """
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class MacosBootEntryInfo:
    """
        Snapshot of a single macOS bootloader entry.

        :param id: Native identifier -- the absolute volume path for
                   blessed-volume entries.
        :param name: Human-readable display name.  Defaults to the
                     volume path when no separate label is reported.
        :param kernel: Reserved -- macOS does not expose the kernel
                       image path through ``bless`` /  ``nvram``.
                       Always ``None``.
        :param initrd: Reserved -- macOS does not use an initramfs.
                       Always ``None``.
        :param cmdline: The current ``boot-args`` NVRAM payload, when
                        the entry corresponds to the active boot
                        volume.  ``None`` for non-active entries.
        :param device: Reserved -- diskutil-resolved BSD name (e.g.
                       ``disk1s5``).  Populated when known.
        :param is_default: ``True`` when the entry corresponds to the
                           active firmware boot device.
        :param boot_count: Reserved.  Always ``None`` on macOS.
    """
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None


@dataclass
class MacosBootEntrySpec:
    """
        Desired bootloader-entry shape for ``add_entry``.

        Only ``BLESS_VOLUME`` is honoured; other kinds produce a
        typed ``FAILED``.

        :param entry_kind: The :class:`MacosBootEntryKind` selector.
        :param volume_path: Absolute path to the volume to bless.
                            Required for ``BLESS_VOLUME``.
        :param name: Optional display name; defaults to the volume
                     path when omitted.
    """
    entry_kind: MacosBootEntryKind
    volume_path: Optional[str] = None
    name: Optional[str] = None


class MacosBootloaderQueryError(Exception):
    """
        Raised when a bootloader-management read fails for a reason
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
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["macOS bootloader query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


_BLESS_BOOT_PATH_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_ -]*)\s*:\s*(?P<value>.*?)\s*$",
)


def _parse_bless_info(*, text: str) -> dict:
    """
        Parse the human-readable ``bless --info`` output into a
        dict.

        Sample output::

            finderinfo[0]:    104  =>  blessed System folder is /System
            finderinfo[1]:      0  =>  blessed system file is none
            finderinfo[2]:      0  =>  Open-folder linked list empty
            finderinfo[3]:      0  =>  No alternate OS blessed file/folder
            finderinfo[4]:      0  =>  Unused field unset
            finderinfo[5]:    104  =>  OS X blessed folder is /System
            64-bit VSDB volume id:  0xC0FFEE
            current boot path:        /Volumes/Macintosh HD
            efi boot device:          /Volumes/Macintosh HD

        Only a small set of keys is consulted; unknown lines are
        ignored.

        :param text: Raw stdout text.

        :returns: A dict keyed by lower-cased canonical-form key
                  names.
    """
    rtnval: dict = {}
    for raw_line in text.splitlines():
        match = _BLESS_BOOT_PATH_RE.match(raw_line)
        if match is None:
            continue
        key = match.group("key").strip().lower()
        value = match.group("value").strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _parse_nvram_boot_args(*, text: str) -> Optional[str]:
    """
        Parse ``nvram boot-args`` output for the boot-args payload.

        Sample::

            boot-args   -v debug=0x144

        :param text: Raw stdout text.

        :returns: The boot-args payload as a single string, or
                  ``None`` when not present.
    """
    rtnval: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("boot-args"):
            rest = line[len("boot-args"):].strip()
            if len(rest) > 0:
                rtnval = rest
            return rtnval
    return rtnval


def _extract_current_boot_path(*, parsed: dict) -> Optional[str]:
    """
        Pick the active boot path from a parsed ``bless --info``
        dict.

        ``bless --info --getBoot`` emits a single bare line with the
        path; the verbose form interleaves ``current boot path:`` /
        ``efi boot device:`` lines.  This helper handles both shapes.

        :param parsed: The parsed dict from
                       :func:`_parse_bless_info`.

        :returns: The path, or ``None``.
    """
    for key in ("current boot path", "efi boot device", "boot device"):
        value = parsed.get(key)
        if value is not None and len(value) > 0:
            rtnval: Optional[str] = value
            return rtnval
    rtnval = None
    return rtnval


# ----------------------------------------------------------------------
# MacosBootloaderManager
# ----------------------------------------------------------------------


class MacosBootloaderManager(BaseFeatureManager):
    """
        macOS OS-level bootloader manager.  Translates cross-OS
        bootloader-management verbs onto ``bless`` and ``nvram``
        invocations dispatched through the injected
        :class:`ICommandRunner`.

        Writes (``set_default_entry``, ``add_entry``,
        ``remove_entry``) honour ``config.elevated`` -- mutating the
        firmware boot device hint requires admin.  Reads
        (``list_entries``, ``get_default_entry``) and idempotency
        probes always pass ``elevated=False``.  ``update_bootloader_config``
        and ``regenerate_config`` are SUCCESS no-ops because macOS
        rebuilds its own loader caches automatically.
    """

    result_class = MacosBootloaderActionResult
    status_class = MacosBootloaderActionStatus
    feature_name = "bootloader-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_entries(self) -> list[MacosBootEntryInfo]:
        """
            Enumerate the currently blessed boot volumes.

            macOS does not expose a multi-entry boot menu; the
            "entries" returned here are the volumes the firmware
            considers bootable, with the active boot volume marked
            as the default.

            :returns: A list of :class:`MacosBootEntryInfo`.
            :raises MacosBootloaderQueryError: When ``bless --info``
                                               fails for an
                                               unexpected reason.
        """
        rtnval: list[MacosBootEntryInfo] = []

        info_command = [_BLESS, "--info", "--verbose"]
        info_run = self._runner.run(list(info_command), elevated=False)
        if info_run.exit_code != 0:
            stderr_lower = (info_run.stderr or "").lower()
            if "no such" in stderr_lower or "not found" in stderr_lower:
                if self._config.default_volume is not None:
                    rtnval = self._synthesize_default_entry(
                        boot_path=self._config.default_volume,
                        cmdline=None,
                    )
                return rtnval
            raise MacosBootloaderQueryError(
                command=info_command,
                exit_code=info_run.exit_code,
                stderr=info_run.stderr or "",
            )

        parsed = _parse_bless_info(text=info_run.stdout)
        active_boot_path = _extract_current_boot_path(parsed=parsed)
        cmdline = self._read_boot_args()

        if active_boot_path is None and self._config.default_volume is not None:
            active_boot_path = self._config.default_volume

        if active_boot_path is None:
            return rtnval

        active_entry = MacosBootEntryInfo(
            id=active_boot_path,
            name=active_boot_path,
            kernel=None,
            initrd=None,
            cmdline=cmdline,
            device=None,
            is_default=True,
            boot_count=None,
        )
        rtnval.append(active_entry)

        return rtnval

    def get_default_entry(self) -> MacosBootEntryInfo:
        """
            Return the currently active boot volume.

            Resolved via ``bless --info --getBoot``; falls back to
            the verbose form when ``--getBoot`` is not supported.

            :returns: The :class:`MacosBootEntryInfo` whose
                      ``is_default`` field is ``True``.
            :raises MacosBootloaderQueryError: When the active boot
                                               device cannot be
                                               resolved.
        """
        get_boot_command = [_BLESS, "--info", "--getBoot"]
        get_boot_run = self._runner.run(
            list(get_boot_command), elevated=False,
        )

        if get_boot_run.exit_code == 0:
            boot_path = get_boot_run.stdout.strip()
            if len(boot_path) > 0:
                cmdline = self._read_boot_args()
                rtnval = MacosBootEntryInfo(
                    id=boot_path,
                    name=boot_path,
                    kernel=None,
                    initrd=None,
                    cmdline=cmdline,
                    device=None,
                    is_default=True,
                    boot_count=None,
                )
                return rtnval

        entries = self.list_entries()
        if len(entries) == 0:
            raise MacosBootloaderQueryError(
                command=get_boot_command,
                exit_code=get_boot_run.exit_code,
                stderr=get_boot_run.stderr or "",
            )

        for entry in entries:
            if entry.is_default:
                rtnval = entry
                return rtnval

        rtnval = entries[0]
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_default_entry(
        self,
        entry_id: str,
    ) -> MacosBootloaderActionResult:
        """
            Persist ``entry_id`` (a volume path) as the active boot
            device via ``bless --mount <path> --setBoot``.

            :param entry_id: The absolute volume path.

            :returns: The :class:`MacosBootloaderActionResult` with
                      ``requires_reboot=True`` on success.
        """
        action = "set_default_entry"
        command = [_BLESS, "--mount", entry_id, "--setBoot"]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        current = self._read_current_boot_path()
        if current is not None and current == entry_id:
            skipped = self._build_result(
                status=MacosBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=False,
                message="bless already reports this volume as boot device",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="set_default_entry",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def add_entry(
        self,
        spec: MacosBootEntrySpec,
    ) -> MacosBootloaderActionResult:
        """
            Bless a volume so the firmware treats it as bootable.

            Only ``MacosBootEntryKind.BLESS_VOLUME`` specs are
            honoured.  Other kinds return a typed ``FAILED``.

            :param spec: The :class:`MacosBootEntrySpec`.

            :returns: The :class:`MacosBootloaderActionResult`.
        """
        action = "add_entry"

        if spec.entry_kind != MacosBootEntryKind.BLESS_VOLUME:
            failed = self._build_result(
                status=MacosBootloaderActionStatus.FAILED,
                detail=action,
                command=None,
                entry_id=None,
                requires_reboot=False,
                message=(
                    "macOS only supports BLESS_VOLUME entries; "
                    "received {}".format(spec.entry_kind.value)
                ),
                stderr="unsupported_entry_kind",
            )
            return failed

        if spec.volume_path is None or len(spec.volume_path) == 0:
            failed = self._build_result(
                status=MacosBootloaderActionStatus.FAILED,
                detail=action,
                command=None,
                entry_id=None,
                requires_reboot=False,
                message="BLESS_VOLUME spec requires a non-empty volume_path",
                stderr="invalid_entry_spec",
            )
            return failed

        command = [_BLESS, "--mount", spec.volume_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=spec.volume_path,
                requires_reboot=True,
            )
            return dry

        if self._volume_already_blessed(volume_path=spec.volume_path):
            skipped = self._build_result(
                status=MacosBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=spec.volume_path,
                requires_reboot=False,
                message="volume is already blessed",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="add_entry",
            entry_id=spec.volume_path,
            requires_reboot=True,
        )
        return rtnval

    def remove_entry(
        self,
        entry_id: str,
    ) -> MacosBootloaderActionResult:
        """
            Unbless a volume via ``bless --unbless <path>``.

            macOS provides limited unbless support; on sealed system
            volumes the runner will surface a permission error.

            :param entry_id: The absolute volume path.

            :returns: The :class:`MacosBootloaderActionResult`.
        """
        action = "remove_entry"
        command = [_BLESS, "--unbless", entry_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        if not self._volume_already_blessed(volume_path=entry_id):
            skipped = self._build_result(
                status=MacosBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=False,
                message="volume is not currently blessed",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="remove_entry",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def update_bootloader_config(self) -> MacosBootloaderActionResult:
        """
            macOS bootloader is OS-managed; there is no user-driven
            update step.  Returns ``SUCCESS`` with a descriptive
            ``message`` so that compliance reporting captures the
            no-op honestly.

            :returns: The :class:`MacosBootloaderActionResult`.
        """
        action = "update_bootloader_config"
        rtnval = self._build_result(
            status=MacosBootloaderActionStatus.SUCCESS,
            detail=action,
            command=None,
            entry_id=None,
            requires_reboot=False,
            message=_NOOP_UPDATE_MESSAGE,
        )
        return rtnval

    def regenerate_config(self) -> MacosBootloaderActionResult:
        """
            macOS bootloader is OS-managed; there is no user-driven
            regenerate step.  Returns ``SUCCESS`` with a descriptive
            ``message``.

            :returns: The :class:`MacosBootloaderActionResult`.
        """
        action = "regenerate_config"
        rtnval = self._build_result(
            status=MacosBootloaderActionStatus.SUCCESS,
            detail=action,
            command=None,
            entry_id=None,
            requires_reboot=False,
            message=_NOOP_UPDATE_MESSAGE,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_current_boot_path(self) -> Optional[str]:
        """
            Return the active boot device path, or ``None`` when it
            cannot be resolved.

            Tries ``bless --info --getBoot`` first (terse) and falls
            back to ``bless --info --verbose`` parsing.
        """
        get_boot_command = [_BLESS, "--info", "--getBoot"]
        get_boot_run = self._runner.run(
            list(get_boot_command), elevated=False,
        )
        if get_boot_run.exit_code == 0:
            value = get_boot_run.stdout.strip()
            if len(value) > 0:
                rtnval: Optional[str] = value
                return rtnval

        info_command = [_BLESS, "--info", "--verbose"]
        info_run = self._runner.run(list(info_command), elevated=False)
        if info_run.exit_code != 0:
            rtnval = None
            return rtnval

        parsed = _parse_bless_info(text=info_run.stdout)
        rtnval = _extract_current_boot_path(parsed=parsed)
        return rtnval

    def _read_boot_args(self) -> Optional[str]:
        """
            Read ``nvram boot-args``.  Reads never elevate.

            :returns: The boot-args payload, or ``None`` when absent.
        """
        command = [_NVRAM, "boot-args"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = _parse_nvram_boot_args(text=run_result.stdout)
        return rtnval

    def _volume_already_blessed(self, *, volume_path: str) -> bool:
        """
            Return ``True`` when the supplied volume is the current
            boot device.

            macOS does not expose a multi-bless inventory; the only
            machine-readable check is whether the firmware boot
            device matches the supplied path.

            :param volume_path: The volume path to test.

            :returns: ``True`` when the volume is currently blessed.
        """
        current = self._read_current_boot_path()
        rtnval = current is not None and current == volume_path
        return rtnval

    def _synthesize_default_entry(
        self,
        *,
        boot_path: str,
        cmdline: Optional[str],
    ) -> list[MacosBootEntryInfo]:
        """
            Build a single-element entry list for the configured
            default volume.  Used when ``bless --info`` is
            unavailable (e.g. in container test fixtures).

            :param boot_path: The configured default volume path.
            :param cmdline: The current boot-args payload.

            :returns: A one-element list.
        """
        entry = MacosBootEntryInfo(
            id=boot_path,
            name=boot_path,
            kernel=None,
            initrd=None,
            cmdline=cmdline,
            device=None,
            is_default=True,
            boot_count=None,
        )
        rtnval = [entry]
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosBootloaderActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        entry_id: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> MacosBootloaderActionResult:
        """
            Construct a :class:`MacosBootloaderActionResult` without
            dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param entry_id: The entry identifier acted upon.
            :param requires_reboot: ``True`` when the change takes
                                    effect only after the next boot.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`MacosBootloaderActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = MacosBootloaderActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            entry_id=entry_id,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval
