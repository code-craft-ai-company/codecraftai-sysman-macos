"""
    macOS hardware-inventory feature.

    Implements the cross-OS hardware-inventory contract for macOS using
    ``system_profiler``, ``sysctl``, ``networksetup``, and ``nvram``.
    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    This feature is strictly read-only.  No verb mutates host state, none
    of them honour an idempotency guard, and there is intentionally no
    ``DRY_RUN`` status.  The :class:`MacosHardwareInventoryManagerConfig`
    inherits ``dry_run`` from :class:`FeatureConfig` for structural
    uniformity with the rest of the package, but the field is never
    consulted by any verb in this module.

    Elevation policy:
        Reads never elevate.  Each verb passes ``elevated=False`` to the
        runner unconditionally.  The ``elevated`` field on the config is
        reserved for the selective-elevation patch documented in
        ``specs/3-hardware-inventory/tasks.md`` and is honoured by no
        verb today.

    Notes on tool selection:
        - ``system_profiler ... -json`` is the canonical machine-readable
          surface for hardware identity, memory, storage, network, PCI,
          and USB enumeration on modern macOS (10.13+).
        - ``sysctl`` complements ``system_profiler`` for CPU topology
          (``machdep.cpu.*``, ``hw.physicalcpu``, ``hw.logicalcpu``,
          ``hw.cpufrequency``) and memory size (``hw.memsize``).
        - ``networksetup -listallhardwareports`` augments the
          ``SPNetworkDataType`` listing with hardware-port mappings.
        - ``nvram -p`` is read once for boot-args / firmware glue.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.featurebase import (
    BaseFeatureManager,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_SYSTEM_PROFILER = "system_profiler"
_SYSCTL = "sysctl"
_NETWORKSETUP = "networksetup"
_NVRAM = "nvram"

_SP_HARDWARE = "SPHardwareDataType"
_SP_MEMORY = "SPMemoryDataType"
_SP_STORAGE = "SPStorageDataType"
_SP_NETWORK = "SPNetworkDataType"
_SP_PCI = "SPPCIDataType"
_SP_USB = "SPUSBDataType"

_JSON_FLAG = "-json"


# ----------------------------------------------------------------------
# Status enum, exception, and config
# ----------------------------------------------------------------------


class MacosHardwareInventoryActionStatus(Enum):
    """
        Outcome status for a hardware-inventory operation.

        This feature is read-only; the cross-OS contract drops
        ``DRY_RUN`` from the status set, so this enum carries only
        ``SUCCESS``, ``FAILED``, and ``SKIPPED``.
    """
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"


@dataclass
class MacosHardwareInventoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosHardwareInventoryManager`.

        Inherits ``elevated`` and ``dry_run`` from :class:`FeatureConfig`
        for structural uniformity with the rest of the package.
        ``dry_run`` is intentionally ignored — the contract makes this
        feature read-only.  ``elevated`` is reserved for the
        selective-elevation patch documented in
        ``specs/3-hardware-inventory/tasks.md``: every verb in this
        module currently passes ``elevated=False`` to the runner so that
        a misconfigured caller cannot accidentally invoke ``sudo`` on a
        read.
    """
    pass


class MacosHardwareInventoryQueryError(Exception):
    """
        Raised when an inventory read fails because the runner returned
        a non-zero exit code or the parser could not interpret the
        captured output.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception message.
                            When not None it replaces the default
                            command/exit/stderr block.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Hardware inventory query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Result dataclasses
# ----------------------------------------------------------------------


@dataclass
class MacosSystemInfo:
    """
        System-identity record returned by
        :meth:`MacosHardwareInventoryManager.get_system_info`.
    """
    manufacturer: Optional[str] = None
    product_name: Optional[str] = None
    serial_number: Optional[str] = None
    uuid: Optional[str] = None
    bios_version: Optional[str] = None
    firmware_version: Optional[str] = None
    family: Optional[str] = None
    version: Optional[str] = None


@dataclass
class MacosCpuInfo:
    """
        CPU topology and feature record returned by
        :meth:`MacosHardwareInventoryManager.get_cpu_info`.
    """
    model: Optional[str] = None
    vendor: Optional[str] = None
    architecture: Optional[str] = None
    physical_cores: int = 0
    logical_cores: int = 0
    frequency_mhz: Optional[int] = None
    cache_l1: Optional[int] = None
    cache_l2: Optional[int] = None
    cache_l3: Optional[int] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class MacosMemoryModule:
    """
        Per-DIMM record nested under :class:`MacosMemoryInfo`.
    """
    slot: Optional[str] = None
    size_bytes: int = 0
    type: Optional[str] = None
    speed_mhz: Optional[int] = None
    manufacturer: Optional[str] = None
    serial_number: Optional[str] = None


@dataclass
class MacosMemoryInfo:
    """
        Aggregate-memory record returned by
        :meth:`MacosHardwareInventoryManager.get_memory_info`.
    """
    total_bytes: int = 0
    available_bytes: Optional[int] = None
    slots_used: Optional[int] = None
    slots_total: Optional[int] = None
    modules: list[MacosMemoryModule] = field(default_factory=list)


@dataclass
class MacosStorageDeviceInfo:
    """
        One storage device record returned by
        :meth:`MacosHardwareInventoryManager.list_storage_devices`.
    """
    name: str
    size_bytes: int = 0
    type: str = ""
    model: Optional[str] = None
    serial: Optional[str] = None
    mountpoint: Optional[str] = None
    filesystem: Optional[str] = None


@dataclass
class MacosNetworkDeviceInfo:
    """
        One network-link record returned by
        :meth:`MacosHardwareInventoryManager.list_network_devices`.
    """
    name: str
    mac_address: Optional[str] = None
    link_state: Optional[str] = None
    speed: Optional[str] = None
    mtu: Optional[int] = None
    is_virtual: bool = False


@dataclass
class MacosPciDeviceInfo:
    """
        One PCI device record returned by
        :meth:`MacosHardwareInventoryManager.list_pci_devices`.
    """
    slot: str
    class_name: Optional[str] = None
    vendor: Optional[str] = None
    device: Optional[str] = None
    vendor_id: Optional[str] = None
    device_id: Optional[str] = None
    driver: Optional[str] = None


@dataclass
class MacosUsbDeviceInfo:
    """
        One USB device record returned by
        :meth:`MacosHardwareInventoryManager.list_usb_devices`.
    """
    bus: str
    device_id: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    name: Optional[str] = None
    serial: Optional[str] = None


@dataclass
class MacosFirmwareInfo:
    """
        Firmware identity returned by
        :meth:`MacosHardwareInventoryManager.get_firmware_info`.
    """
    vendor: Optional[str] = None
    version: Optional[str] = None
    release_date: Optional[str] = None
    secure_boot: Optional[bool] = None
    uefi: Optional[bool] = None


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class MacosHardwareInventoryManager(BaseFeatureManager):
    """
        Read-only hardware-inventory manager for macOS.

        Every verb shells out to a macOS inventory tool
        (``system_profiler -json``, ``sysctl``, ``networksetup``,
        ``nvram``) through the injected :class:`ICommandRunner`.
        Outputs are JSON-decoded or line-parsed in pure Python and
        returned as ``Macos*`` dataclasses.  Every runner invocation
        passes ``elevated=False`` regardless of the config so that a
        misconfigured caller cannot accidentally elevate a read.
    """

    status_class = MacosHardwareInventoryActionStatus
    feature_name = "hardware-inventory"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_system_info(self) -> MacosSystemInfo:
        """
            Query system identity via
            ``system_profiler SPHardwareDataType -json``.

            :returns: The :class:`MacosSystemInfo`.
            :raises MacosHardwareInventoryQueryError: When the command
                                                      fails or the JSON
                                                      payload is
                                                      malformed.
        """
        command = [_SYSTEM_PROFILER, _SP_HARDWARE, _JSON_FLAG]
        run_result = self._read(command=command)
        info = _parse_system_profiler_hardware(
            stdout=run_result.stdout, command=command,
        )
        return info

    def get_cpu_info(self) -> MacosCpuInfo:
        """
            Query CPU topology and feature flags via two ``sysctl``
            calls: ``sysctl -a machdep.cpu`` for vendor / brand /
            features, and ``sysctl hw.physicalcpu hw.logicalcpu
            hw.cpufrequency`` for the topology aggregates.

            On Apple silicon the ``machdep.cpu`` MIB does not carry the
            classic Intel keys; the parser tolerates missing keys and
            leaves the corresponding fields as ``None``.

            :returns: The :class:`MacosCpuInfo`.
            :raises MacosHardwareInventoryQueryError: When either
                                                      ``sysctl`` call
                                                      fails.
        """
        machdep_command = [_SYSCTL, "-a", "machdep.cpu"]
        machdep_result = self._read(command=machdep_command)

        hw_command = [
            _SYSCTL,
            "hw.physicalcpu", "hw.logicalcpu", "hw.cpufrequency",
        ]
        hw_result = self._read(command=hw_command)

        info = _parse_sysctl_machdep_cpu(stdout=machdep_result.stdout)
        _merge_sysctl_hw_cpu(info=info, stdout=hw_result.stdout)

        return info

    def get_memory_info(self) -> MacosMemoryInfo:
        """
            Query aggregate memory and per-DIMM detail via
            ``system_profiler SPMemoryDataType -json`` and
            ``sysctl hw.memsize``.

            On Apple silicon the memory is on-package and
            ``SPMemoryDataType`` reports a single synthetic entry; the
            parser still emits a single :class:`MacosMemoryModule` so
            callers see a uniform shape across architectures.

            :returns: The :class:`MacosMemoryInfo`.
            :raises MacosHardwareInventoryQueryError: When either
                                                      command fails.
        """
        sp_command = [_SYSTEM_PROFILER, _SP_MEMORY, _JSON_FLAG]
        sp_result = self._read(command=sp_command)

        sysctl_command = [_SYSCTL, "hw.memsize"]
        sysctl_result = self._read(command=sysctl_command)

        info = _parse_system_profiler_memory(
            stdout=sp_result.stdout, command=sp_command,
        )
        _merge_sysctl_memsize(info=info, stdout=sysctl_result.stdout)

        return info

    def list_storage_devices(self) -> list[MacosStorageDeviceInfo]:
        """
            Enumerate storage devices via
            ``system_profiler SPStorageDataType -json``.

            ``SPStorageDataType`` is a *volume*-centric listing rather
            than a raw-device listing.  Each entry's ``_name``,
            ``size_in_bytes``, ``mount_point``, ``file_system``, and
            ``physical_drive`` populate one
            :class:`MacosStorageDeviceInfo`.

            :returns: One record per volume.
            :raises MacosHardwareInventoryQueryError: When the command
                                                      fails or the JSON
                                                      payload is
                                                      malformed.
        """
        command = [_SYSTEM_PROFILER, _SP_STORAGE, _JSON_FLAG]
        run_result = self._read(command=command)
        devices = _parse_system_profiler_storage(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def list_network_devices(self) -> list[MacosNetworkDeviceInfo]:
        """
            Enumerate network link-layer hardware via
            ``system_profiler SPNetworkDataType -json`` and merge with
            ``networksetup -listallhardwareports`` for the BSD-name to
            hardware-port mapping.

            :returns: One record per interface.
            :raises MacosHardwareInventoryQueryError: When either
                                                      command fails or
                                                      the JSON payload
                                                      is malformed.
        """
        sp_command = [_SYSTEM_PROFILER, _SP_NETWORK, _JSON_FLAG]
        sp_result = self._read(command=sp_command)

        ns_command = [_NETWORKSETUP, "-listallhardwareports"]
        ns_result = self._read(command=ns_command)

        port_map = _parse_networksetup_hardware_ports(
            stdout=ns_result.stdout,
        )
        devices = _parse_system_profiler_network(
            stdout=sp_result.stdout,
            command=sp_command,
            port_map=port_map,
        )
        return devices

    def list_pci_devices(self) -> list[MacosPciDeviceInfo]:
        """
            Enumerate PCI devices via
            ``system_profiler SPPCIDataType -json``.

            Apple silicon hosts surface a sparse PCI tree; the parser
            returns whatever entries ``SPPCIDataType`` emits — empty
            list is a valid result.

            :returns: One record per PCI device.
            :raises MacosHardwareInventoryQueryError: When the command
                                                      fails or the JSON
                                                      payload is
                                                      malformed.
        """
        command = [_SYSTEM_PROFILER, _SP_PCI, _JSON_FLAG]
        run_result = self._read(command=command)
        devices = _parse_system_profiler_pci(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def list_usb_devices(self) -> list[MacosUsbDeviceInfo]:
        """
            Enumerate USB devices via
            ``system_profiler SPUSBDataType -json``.

            ``SPUSBDataType`` reports a tree of buses and child devices.
            The parser flattens the tree depth-first; root buses surface
            as records with ``vendor_id=None`` / ``product_id=None`` so
            callers can distinguish them from leaf devices.

            :returns: One record per USB device or hub.
            :raises MacosHardwareInventoryQueryError: When the command
                                                      fails or the JSON
                                                      payload is
                                                      malformed.
        """
        command = [_SYSTEM_PROFILER, _SP_USB, _JSON_FLAG]
        run_result = self._read(command=command)
        devices = _parse_system_profiler_usb(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def get_firmware_info(self) -> MacosFirmwareInfo:
        """
            Query firmware identity via
            ``system_profiler SPHardwareDataType -json`` (for
            ``boot_rom_version`` / ``smc_version_system``) and
            ``nvram -p`` (for boot args used as supplementary detail).

            macOS does not surface a separate "BIOS vendor / release
            date" pair the way SMBIOS does; the parser populates
            ``vendor`` with ``"Apple"``, ``version`` with the iBoot /
            Boot ROM version, and leaves ``release_date`` ``None``.
            ``uefi=True`` for every modern Mac since the platform boots
            via iBoot / EFI; ``secure_boot`` is left ``None`` for this
            pilot (the proper read is via ``bputil`` / EFI-vars and is
            documented as a follow-up).

            :returns: The :class:`MacosFirmwareInfo`.
            :raises MacosHardwareInventoryQueryError: When either
                                                      command fails.
        """
        sp_command = [_SYSTEM_PROFILER, _SP_HARDWARE, _JSON_FLAG]
        sp_result = self._read(command=sp_command)

        nvram_command = [_NVRAM, "-p"]
        nvram_result = self._read(command=nvram_command)

        info = _parse_system_profiler_firmware(
            stdout=sp_result.stdout, command=sp_command,
        )

        # boot-args is informational glue only (specs/3-hardware-inventory
        # Requirement 8.5): it is consumed but sets no field of ``info``.
        _merge_nvram_p(info=info, stdout=nvram_result.stdout)

        return info

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, *, command: list[str]) -> RunResult:
        """
            Invoke the runner with ``elevated=False`` and raise
            :class:`MacosHardwareInventoryQueryError` on non-zero exit.

            Read verbs share this single dispatch helper so that the
            "reads never elevate" property is enforced in exactly one
            place and so that the exception construction is centralised.

            :param command: The argv to dispatch.

            :returns: The raw :class:`RunResult` on success.
            :raises MacosHardwareInventoryQueryError: When the runner
                                                      reports a non-zero
                                                      exit code.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosHardwareInventoryQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        return run_result


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _decode_json(*, stdout: str, command: list[str]):
    """
        Decode JSON, raising
        :class:`MacosHardwareInventoryQueryError` on malformed input.

        :param stdout: The raw stdout text.
        :param command: The argv that produced the output (for the
                        exception message).

        :returns: The parsed Python object.
        :raises MacosHardwareInventoryQueryError: When ``stdout`` is
                                                  not valid JSON.
    """
    try:
        rtnval = json.loads(stdout)
    except json.JSONDecodeError as ex:
        raise MacosHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="{} emitted malformed JSON: {}".format(
                " ".join(command), ex,
            ),
        ) from None

    return rtnval


def _parse_system_profiler_hardware(
    *,
    stdout: str,
    command: list[str],
) -> MacosSystemInfo:
    """
        Parse ``system_profiler SPHardwareDataType -json`` output.

        Top-level shape::

            {"SPHardwareDataType": [
                {"_name": "hardware_overview",
                 "machine_model": "MacBookPro18,3",
                 "machine_name": "MacBook Pro",
                 "serial_number": "...",
                 "platform_UUID": "...",
                 "boot_rom_version": "...",
                 "smc_version_system": "...",
                 ... }
            ]}

        :param stdout: Raw stdout from the command.
        :param command: The argv that produced ``stdout``.

        :returns: The populated :class:`MacosSystemInfo`.
    """
    info = MacosSystemInfo(manufacturer="Apple Inc.")

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return info

    entries = data.get("SPHardwareDataType", [])
    if len(entries) == 0:
        return info

    entry = entries[0]
    info.product_name = entry.get("machine_name")
    info.family = entry.get("machine_model")
    info.version = entry.get("model_number")
    info.serial_number = entry.get("serial_number")
    info.uuid = entry.get("platform_UUID")
    info.bios_version = entry.get("boot_rom_version")
    info.firmware_version = entry.get("smc_version_system")

    return info


def _parse_system_profiler_firmware(
    *,
    stdout: str,
    command: list[str],
) -> MacosFirmwareInfo:
    """
        Parse ``system_profiler SPHardwareDataType -json`` output for
        firmware fields.

        :param stdout: Raw stdout from the command.
        :param command: The argv that produced ``stdout``.

        :returns: The populated :class:`MacosFirmwareInfo`.
    """
    info = MacosFirmwareInfo(vendor="Apple", uefi=True)

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return info

    entries = data.get("SPHardwareDataType", [])
    if len(entries) == 0:
        return info

    entry = entries[0]
    info.version = entry.get("boot_rom_version")
    return info


def _merge_nvram_p(*, info: MacosFirmwareInfo, stdout: str) -> Optional[str]:
    """
        Consume ``nvram -p`` output alongside an existing
        :class:`MacosFirmwareInfo`.

        ``nvram -p`` emits one ``KEY\\tVALUE`` row per variable.
        ``boot-args`` is the only variable consumed by this pilot and it
        is informational glue only: no field of ``info`` is set from it
        (``specs/3-hardware-inventory`` Requirement 8, criteria 4 and 5).
        In particular ``info.release_date`` is left untouched: it is the
        firmware release date (as on the Linux and Windows siblings,
        which fill it from SMBIOS / ``Win32_BIOS``), macOS does not
        surface one, and :class:`MacosFirmwareInfo` has no boot-args
        field.  The ``boot-args`` value is returned instead so that a
        caller, or a future field, can use it without re-parsing.
        Other variables are ignored.

        :param info: The :class:`MacosFirmwareInfo` built from
                     ``system_profiler``.  It is not modified.
        :param stdout: Raw stdout from ``nvram -p``.

        :returns: The ``boot-args`` value, or ``None`` when the host
                  carries no boot arguments.
    """
    boot_args: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            continue
        if "\t" in line:
            key, _, value = line.partition("\t")
        else:
            tokens = line.split(None, 1)
            if len(tokens) < 2:
                continue
            key = tokens[0]
            value = tokens[1]

        if key == "boot-args":
            # boot-args is informational; no field of info is set from it.
            boot_args = value.strip()

    return boot_args


def _parse_sysctl_machdep_cpu(*, stdout: str) -> MacosCpuInfo:
    """
        Parse ``sysctl -a machdep.cpu`` output.

        ``machdep.cpu`` MIBs available on Intel macOS::

            machdep.cpu.vendor: GenuineIntel
            machdep.cpu.brand_string: Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz
            machdep.cpu.features: FPU VME DE PSE TSC MSR PAE ...
            machdep.cpu.cache.size: 256
            machdep.cpu.cache.linesize: 64
            machdep.cpu.cores_per_package: 6

        On Apple silicon the ``machdep.cpu`` keys are sparse; the
        parser tolerates missing keys and leaves fields as defaults.

        :param stdout: Raw stdout from the command.

        :returns: The populated :class:`MacosCpuInfo`.
    """
    info = MacosCpuInfo(architecture=None)

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key == "machdep.cpu.vendor":
            info.vendor = value
        elif key == "machdep.cpu.brand_string":
            info.model = value
        elif key == "machdep.cpu.features":
            info.flags = value.split()

    return info


def _merge_sysctl_hw_cpu(*, info: MacosCpuInfo, stdout: str) -> None:
    """
        Merge ``sysctl hw.physicalcpu hw.logicalcpu hw.cpufrequency``
        onto an existing :class:`MacosCpuInfo`.
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key == "hw.physicalcpu":
            try:
                info.physical_cores = int(value)
            except ValueError:
                info.physical_cores = 0
        elif key == "hw.logicalcpu":
            try:
                info.logical_cores = int(value)
            except ValueError:
                info.logical_cores = 0
        elif key == "hw.cpufrequency":
            try:
                hz_value = int(value)
                info.frequency_mhz = hz_value // 1_000_000
            except ValueError:
                info.frequency_mhz = None

    return


def _parse_system_profiler_memory(
    *,
    stdout: str,
    command: list[str],
) -> MacosMemoryInfo:
    """
        Parse ``system_profiler SPMemoryDataType -json`` output.

        Top-level shape on Intel::

            {"SPMemoryDataType": [
                {"_name": "memory_overview",
                 "_items": [
                    {"_name": "BANK 0 / DIMM 0",
                     "dimm_size": "8 GB",
                     "dimm_type": "DDR4",
                     "dimm_speed": "2667 MHz",
                     "dimm_manufacturer": "0x80AD",
                     "dimm_part_number": "...",
                     "dimm_serial_number": "..."}
                 ]}
            ]}

        On Apple silicon the items array is replaced by a synthetic
        entry that lacks ``_items`` but has ``SPMemoryDataType[0]``
        carry ``dimm_size`` directly; the parser handles both shapes.

        :param stdout: Raw stdout from the command.
        :param command: The argv that produced ``stdout``.

        :returns: The populated :class:`MacosMemoryInfo`.
    """
    info = MacosMemoryInfo()

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return info

    entries = data.get("SPMemoryDataType", [])
    if len(entries) == 0:
        return info

    overview = entries[0]
    items = overview.get("_items")
    if items is None:
        items = []

    if len(items) == 0:
        # Apple silicon synthetic entry: take the overview directly.
        size_bytes = _parse_size_string(raw=overview.get("dimm_size"))
        if size_bytes > 0:
            module = MacosMemoryModule(
                slot=overview.get("_name"),
                size_bytes=size_bytes,
                type=overview.get("dimm_type"),
                speed_mhz=_parse_speed_mhz(raw=overview.get("dimm_speed")),
                manufacturer=overview.get("dimm_manufacturer"),
                serial_number=overview.get("dimm_serial_number"),
            )
            info.modules.append(module)
    else:
        for item in items:
            size_bytes = _parse_size_string(raw=item.get("dimm_size"))
            if size_bytes <= 0:
                continue
            module = MacosMemoryModule(
                slot=item.get("_name"),
                size_bytes=size_bytes,
                type=item.get("dimm_type"),
                speed_mhz=_parse_speed_mhz(raw=item.get("dimm_speed")),
                manufacturer=item.get("dimm_manufacturer"),
                serial_number=item.get("dimm_serial_number"),
            )
            info.modules.append(module)

    info.slots_used = len(info.modules)
    info.slots_total = info.slots_used

    return info


def _merge_sysctl_memsize(*, info: MacosMemoryInfo, stdout: str) -> None:
    """
        Merge ``sysctl hw.memsize`` (bytes) onto an existing
        :class:`MacosMemoryInfo` as ``total_bytes``.
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key == "hw.memsize":
            try:
                info.total_bytes = int(value)
            except ValueError:
                info.total_bytes = 0

    return


def _parse_size_string(*, raw) -> int:
    """
        Decode a ``system_profiler`` size string like ``"8 GB"``,
        ``"512 MB"``, or ``"1 TB"`` into bytes.  Returns 0 for
        ``None`` or unparseable input.
    """
    if raw is None:
        return 0

    tokens = str(raw).split()
    if len(tokens) < 2:
        return 0

    try:
        number = float(tokens[0])
    except ValueError:
        return 0

    unit = tokens[1].lower()
    if unit == "kb":
        scale = 1024
    elif unit == "mb":
        scale = 1024 ** 2
    elif unit == "gb":
        scale = 1024 ** 3
    elif unit == "tb":
        scale = 1024 ** 4
    else:
        scale = 1

    bytes_value = int(number * scale)
    return bytes_value


def _parse_speed_mhz(*, raw) -> Optional[int]:
    """
        Decode a ``system_profiler`` speed string like ``"2667 MHz"``
        or ``"2400 MT/s"`` into an integer MHz.  Returns ``None`` for
        ``None`` or unparseable input.
    """
    if raw is None:
        return None

    tokens = str(raw).split()
    if len(tokens) < 1:
        return None

    try:
        number = int(tokens[0])
    except ValueError:
        return None

    return number


def _parse_system_profiler_storage(
    *,
    stdout: str,
    command: list[str],
) -> list[MacosStorageDeviceInfo]:
    """
        Parse ``system_profiler SPStorageDataType -json`` output.

        Top-level shape::

            {"SPStorageDataType": [
                {"_name": "Macintosh HD",
                 "size_in_bytes": 494384795648,
                 "mount_point": "/",
                 "file_system": "APFS",
                 "physical_drive": {"device_name": "APPLE SSD ...",
                                    "media_name": "AppleAPFSMedia",
                                    "is_internal_disk": "yes"}}
            ]}

        :returns: One :class:`MacosStorageDeviceInfo` per volume.
    """
    devices: list[MacosStorageDeviceInfo] = []

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return devices

    entries = data.get("SPStorageDataType", [])

    for entry in entries:
        physical = entry.get("physical_drive", {})
        if not isinstance(physical, dict):
            physical = {}

        size_raw = entry.get("size_in_bytes", 0)
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            size_bytes = 0

        record = MacosStorageDeviceInfo(
            name=entry.get("_name", ""),
            size_bytes=size_bytes,
            type=physical.get("medium_type", "") or "",
            model=physical.get("device_name"),
            serial=physical.get("device_serial"),
            mountpoint=entry.get("mount_point"),
            filesystem=entry.get("file_system"),
        )
        devices.append(record)

    return devices


def _parse_networksetup_hardware_ports(*, stdout: str) -> dict:
    """
        Parse ``networksetup -listallhardwareports`` output into a map
        of BSD device name -> hardware-port description.

        Output shape::

            Hardware Port: Wi-Fi
            Device: en0
            Ethernet Address: 1c:1b:b5:11:22:33

            Hardware Port: USB 10/100/1000 LAN
            Device: en6
            Ethernet Address: 00:e0:4c:aa:bb:cc

        :returns: ``{"en0": "Wi-Fi", ...}``.
    """
    port_map: dict = {}
    current_port: Optional[str] = None
    current_device: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            if current_device is not None and current_port is not None:
                port_map[current_device] = current_port
            current_port = None
            current_device = None
            continue

        if line.startswith("Hardware Port:"):
            _, _, value = line.partition(":")
            current_port = value.strip()
        elif line.startswith("Device:"):
            _, _, value = line.partition(":")
            current_device = value.strip()

    if current_device is not None and current_port is not None:
        port_map[current_device] = current_port

    return port_map


def _parse_system_profiler_network(
    *,
    stdout: str,
    command: list[str],
    port_map: dict,
) -> list[MacosNetworkDeviceInfo]:
    """
        Parse ``system_profiler SPNetworkDataType -json``.

        Top-level shape::

            {"SPNetworkDataType": [
                {"_name": "Wi-Fi",
                 "interface": "en0",
                 "type": "AirPort",
                 "hardware": "AirPort",
                 "Ethernet": {"MAC Address": "1c:1b:b5:...", "MediaSubType": "..."},
                 "has_ip_assigned": "Yes"}
            ]}

        :param port_map: BSD-name -> hardware-port mapping from
                         ``networksetup -listallhardwareports``.

        :returns: One :class:`MacosNetworkDeviceInfo` per interface.
    """
    devices: list[MacosNetworkDeviceInfo] = []

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return devices

    entries = data.get("SPNetworkDataType", [])

    for entry in entries:
        interface = entry.get("interface", "") or ""
        ethernet = entry.get("Ethernet", {})
        if not isinstance(ethernet, dict):
            ethernet = {}

        mac = ethernet.get("MAC Address")
        type_value = entry.get("type", "")
        is_virtual = type_value in ("Bridge", "Bond", "VPN") \
            or interface.startswith("utun") \
            or interface.startswith("lo") \
            or interface.startswith("gif") \
            or interface.startswith("stf")

        # ``has_ip_assigned`` reports "Yes" / "No"; map to a coarse
        # link_state.  The "active" probe is in
        # ``ifconfig_data.status_value``, but
        # ``has_ip_assigned`` is the cross-version-stable proxy.
        if entry.get("has_ip_assigned") == "Yes":
            link_state = "active"
        else:
            link_state = "inactive"

        record = MacosNetworkDeviceInfo(
            name=interface,
            mac_address=mac,
            link_state=link_state,
            speed=None,
            mtu=None,
            is_virtual=is_virtual,
        )
        # Hardware-port mapping is informational; not a record field
        # in this pilot, but reads of port_map happen here so the
        # parser still advertises that the port_map is consumed.
        if interface in port_map:
            # The hardware-port string maps cleanly onto ``speed`` for
            # the few cases where a port string carries link speed
            # ("USB 10/100/1000 LAN").  For Wi-Fi the value is the
            # human-readable hardware port name, not a link speed.
            port_label = port_map[interface]
            if port_label.endswith(" LAN") or "Ethernet" in port_label:
                record.speed = port_label

        devices.append(record)

    return devices


def _parse_system_profiler_pci(
    *,
    stdout: str,
    command: list[str],
) -> list[MacosPciDeviceInfo]:
    """
        Parse ``system_profiler SPPCIDataType -json``.

        Top-level shape::

            {"SPPCIDataType": [
                {"_name": "ethernet",
                 "sppci_slot_name": "Thunderbolt@22,0,0",
                 "sppci_vendor": "Intel",
                 "sppci_device_type": "ethernet",
                 "sppci_revision-id": "...",
                 "sppci_driver": "AppleIntelI210Ethernet"}
            ]}

        :returns: One :class:`MacosPciDeviceInfo` per entry.
    """
    devices: list[MacosPciDeviceInfo] = []

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return devices

    entries = data.get("SPPCIDataType", [])

    for entry in entries:
        slot = entry.get("sppci_slot_name", "") or ""
        record = MacosPciDeviceInfo(
            slot=slot,
            class_name=entry.get("sppci_device_type"),
            vendor=entry.get("sppci_vendor"),
            device=entry.get("_name"),
            vendor_id=entry.get("sppci_vendor-id"),
            device_id=entry.get("sppci_device-id"),
            driver=entry.get("sppci_driver"),
        )
        devices.append(record)

    return devices


def _parse_system_profiler_usb(
    *,
    stdout: str,
    command: list[str],
) -> list[MacosUsbDeviceInfo]:
    """
        Parse ``system_profiler SPUSBDataType -json``.

        Top-level shape::

            {"SPUSBDataType": [
                {"_name": "USB 3.1 Bus",
                 "host_controller": "AppleUSBXHCI",
                 "_items": [
                    {"_name": "DataTraveler",
                     "vendor_id": "0x0951  (Kingston)",
                     "product_id": "0x1666",
                     "serial_num": "...",
                     "location_id": "0x14000000 / 1"}
                 ]}
            ]}

        The parser flattens the bus / device tree depth-first.

        :returns: One :class:`MacosUsbDeviceInfo` per node.
    """
    devices: list[MacosUsbDeviceInfo] = []

    data = _decode_json(stdout=stdout, command=command)
    if not isinstance(data, dict):
        return devices

    roots = data.get("SPUSBDataType", [])

    def _flatten(node: dict, bus_label: str) -> None:
        bus_value = node.get("location_id") or bus_label
        vendor_id = _strip_macos_id(raw=node.get("vendor_id"))
        product_id = _strip_macos_id(raw=node.get("product_id"))

        record = MacosUsbDeviceInfo(
            bus=str(bus_value),
            device_id=node.get("_name"),
            vendor_id=vendor_id,
            product_id=product_id,
            name=node.get("_name"),
            serial=node.get("serial_num"),
        )
        devices.append(record)

        children = node.get("_items", [])
        for child in children:
            _flatten(child, bus_label=str(bus_value))
        return

    for root in roots:
        _flatten(root, bus_label=root.get("_name", ""))

    return devices


def _strip_macos_id(*, raw) -> Optional[str]:
    """
        Extract the leading hex token from a ``system_profiler`` USB id
        string like ``"0x0951  (Kingston)"``.
    """
    if raw is None:
        return None

    text = str(raw).strip()
    if len(text) == 0:
        return None

    tokens = text.split()
    head = tokens[0]
    if head.startswith("0x"):
        return head[2:]
    return head
