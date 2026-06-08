"""
    Tests for :class:`MacosHardwareInventoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv.  No
    live ``system_profiler`` / ``sysctl`` / ``networksetup`` / ``nvram``
    is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.hardwareinventory import (
    MacosHardwareInventoryManager,
    MacosHardwareInventoryManagerConfig,
    MacosHardwareInventoryQueryError,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=1,
            stdout="",
            stderr="not implemented",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build a canonical success :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(*, exit_code: int = 1, stderr: str = "boom") -> RunResult:
    """
        Build a canonical failure :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# get_system_info
# ----------------------------------------------------------------------


def test_get_system_info_invokes_system_profiler_and_parses():
    """
        get_system_info() shells out to ``system_profiler
        SPHardwareDataType -json`` and parses the hardware overview.
    """
    payload = json.dumps({
        "SPHardwareDataType": [
            {
                "_name": "hardware_overview",
                "machine_name": "MacBook Pro",
                "machine_model": "MacBookPro18,3",
                "model_number": "MK183LL/A",
                "serial_number": "C02ABC123XYZ",
                "platform_UUID": "AAAA-BBBB-CCCC",
                "boot_rom_version": "8419.40.111.0.1",
                "smc_version_system": "3.0",
            }
        ]
    })
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPHardwareDataType", "-json",
    )] = _ok(stdout=payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    info = manager.get_system_info()

    testbase.assert_equal(info.manufacturer, "Apple Inc.")
    testbase.assert_equal(info.product_name, "MacBook Pro")
    testbase.assert_equal(info.family, "MacBookPro18,3")
    testbase.assert_equal(info.version, "MK183LL/A")
    testbase.assert_equal(info.serial_number, "C02ABC123XYZ")
    testbase.assert_equal(info.uuid, "AAAA-BBBB-CCCC")
    testbase.assert_equal(info.bios_version, "8419.40.111.0.1")
    testbase.assert_equal(info.firmware_version, "3.0")
    return


# ----------------------------------------------------------------------
# get_cpu_info
# ----------------------------------------------------------------------


def test_get_cpu_info_parses_sysctl_output():
    """
        get_cpu_info() parses ``sysctl -a machdep.cpu`` and merges
        ``sysctl hw.physicalcpu hw.logicalcpu hw.cpufrequency``.
    """
    machdep = (
        "machdep.cpu.vendor: GenuineIntel\n"
        "machdep.cpu.brand_string: Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz\n"
        "machdep.cpu.features: FPU VME DE PSE TSC MSR PAE\n"
    )
    hw_cpu = (
        "hw.physicalcpu: 6\n"
        "hw.logicalcpu: 12\n"
        "hw.cpufrequency: 2600000000\n"
    )
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-a", "machdep.cpu")] = _ok(stdout=machdep)
    runner.results[(
        "sysctl", "hw.physicalcpu", "hw.logicalcpu", "hw.cpufrequency",
    )] = _ok(stdout=hw_cpu)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    info = manager.get_cpu_info()

    testbase.assert_equal(info.vendor, "GenuineIntel")
    testbase.assert_equal(
        info.model, "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz",
    )
    testbase.assert_equal(info.flags, ["FPU", "VME", "DE", "PSE", "TSC", "MSR", "PAE"])
    testbase.assert_equal(info.physical_cores, 6)
    testbase.assert_equal(info.logical_cores, 12)
    testbase.assert_equal(info.frequency_mhz, 2600)
    return


# ----------------------------------------------------------------------
# get_memory_info
# ----------------------------------------------------------------------


def test_get_memory_info_aggregates_modules_and_total():
    """
        get_memory_info() aggregates per-DIMM rows from
        ``system_profiler SPMemoryDataType -json`` and merges
        ``sysctl hw.memsize``.
    """
    sp_memory = json.dumps({
        "SPMemoryDataType": [
            {
                "_name": "memory_overview",
                "_items": [
                    {
                        "_name": "BANK 0/DIMM 0",
                        "dimm_size": "16 GB",
                        "dimm_type": "DDR4",
                        "dimm_speed": "2667 MHz",
                        "dimm_manufacturer": "0x80AD",
                        "dimm_serial_number": "ABC123",
                    },
                    {
                        "_name": "BANK 0/DIMM 1",
                        "dimm_size": "Empty",
                    },
                ],
            }
        ]
    })
    sysctl_mem = "hw.memsize: 17179869184\n"
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPMemoryDataType", "-json",
    )] = _ok(stdout=sp_memory)
    runner.results[("sysctl", "hw.memsize")] = _ok(stdout=sysctl_mem)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    info = manager.get_memory_info()

    testbase.assert_equal(info.total_bytes, 17179869184)
    testbase.assert_equal(len(info.modules), 1)
    testbase.assert_equal(info.modules[0].size_bytes, 16 * 1024 ** 3)
    testbase.assert_equal(info.modules[0].slot, "BANK 0/DIMM 0")
    testbase.assert_equal(info.modules[0].type, "DDR4")
    testbase.assert_equal(info.modules[0].speed_mhz, 2667)
    testbase.assert_equal(info.modules[0].serial_number, "ABC123")
    testbase.assert_equal(info.slots_used, 1)
    return


# ----------------------------------------------------------------------
# list_storage_devices
# ----------------------------------------------------------------------


def test_list_storage_devices_parses_system_profiler_output():
    """
        list_storage_devices() parses ``system_profiler SPStorageDataType``.
    """
    payload = json.dumps({
        "SPStorageDataType": [
            {
                "_name": "Macintosh HD",
                "size_in_bytes": 494384795648,
                "mount_point": "/",
                "file_system": "APFS",
                "physical_drive": {
                    "device_name": "APPLE SSD AP0512Q",
                    "device_serial": "S1AB",
                    "medium_type": "SSD",
                },
            },
            {
                "_name": "Time Machine",
                "size_in_bytes": 2000000000000,
                "mount_point": "/Volumes/Time Machine",
                "file_system": "HFS+",
                "physical_drive": {
                    "device_name": "Seagate Backup Plus",
                    "medium_type": "Rotational",
                },
            },
        ]
    })
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPStorageDataType", "-json",
    )] = _ok(stdout=payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    devices = manager.list_storage_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].name, "Macintosh HD")
    testbase.assert_equal(devices[0].size_bytes, 494384795648)
    testbase.assert_equal(devices[0].type, "SSD")
    testbase.assert_equal(devices[0].model, "APPLE SSD AP0512Q")
    testbase.assert_equal(devices[0].serial, "S1AB")
    testbase.assert_equal(devices[0].mountpoint, "/")
    testbase.assert_equal(devices[0].filesystem, "APFS")
    testbase.assert_equal(devices[1].name, "Time Machine")
    testbase.assert_equal(devices[1].type, "Rotational")
    return


# ----------------------------------------------------------------------
# list_network_devices
# ----------------------------------------------------------------------


def test_list_network_devices_parses_system_profiler_output():
    """
        list_network_devices() parses ``system_profiler SPNetworkDataType``
        and merges hardware-port labels from ``networksetup
        -listallhardwareports``.
    """
    sp_payload = json.dumps({
        "SPNetworkDataType": [
            {
                "_name": "Wi-Fi",
                "interface": "en0",
                "type": "AirPort",
                "Ethernet": {"MAC Address": "1c:1b:b5:11:22:33"},
                "has_ip_assigned": "Yes",
            },
            {
                "_name": "Bridge",
                "interface": "bridge0",
                "type": "Bridge",
                "Ethernet": {"MAC Address": "ac:de:48:00:11:22"},
                "has_ip_assigned": "No",
            },
        ]
    })
    networksetup_payload = (
        "Hardware Port: Wi-Fi\n"
        "Device: en0\n"
        "Ethernet Address: 1c:1b:b5:11:22:33\n"
        "\n"
        "Hardware Port: USB 10/100/1000 LAN\n"
        "Device: en6\n"
        "Ethernet Address: 00:e0:4c:aa:bb:cc\n"
        "\n"
    )
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPNetworkDataType", "-json",
    )] = _ok(stdout=sp_payload)
    runner.results[(
        "networksetup", "-listallhardwareports",
    )] = _ok(stdout=networksetup_payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    devices = manager.list_network_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].name, "en0")
    testbase.assert_equal(devices[0].mac_address, "1c:1b:b5:11:22:33")
    testbase.assert_equal(devices[0].link_state, "active")
    testbase.assert_equal(devices[0].is_virtual, False)
    testbase.assert_equal(devices[1].name, "bridge0")
    testbase.assert_equal(devices[1].is_virtual, True)
    testbase.assert_equal(devices[1].link_state, "inactive")
    return


# ----------------------------------------------------------------------
# list_pci_devices
# ----------------------------------------------------------------------


def test_list_pci_devices_parses_system_profiler_output():
    """
        list_pci_devices() parses ``system_profiler SPPCIDataType``.
    """
    payload = json.dumps({
        "SPPCIDataType": [
            {
                "_name": "ethernet",
                "sppci_slot_name": "Thunderbolt@22,0,0",
                "sppci_vendor": "Intel",
                "sppci_device_type": "ethernet",
                "sppci_vendor-id": "0x8086",
                "sppci_device-id": "0x1539",
                "sppci_driver": "AppleIntelI210Ethernet",
            },
            {
                "_name": "graphics",
                "sppci_slot_name": "PCI@0,0",
                "sppci_vendor": "AMD",
                "sppci_device_type": "displaycontroller",
                "sppci_vendor-id": "0x1002",
                "sppci_device-id": "0x73DF",
                "sppci_driver": "AMDRadeonX6000",
            },
        ]
    })
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPPCIDataType", "-json",
    )] = _ok(stdout=payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    devices = manager.list_pci_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].slot, "Thunderbolt@22,0,0")
    testbase.assert_equal(devices[0].vendor, "Intel")
    testbase.assert_equal(devices[0].device, "ethernet")
    testbase.assert_equal(devices[0].vendor_id, "0x8086")
    testbase.assert_equal(devices[0].device_id, "0x1539")
    testbase.assert_equal(devices[0].driver, "AppleIntelI210Ethernet")
    testbase.assert_equal(devices[1].vendor, "AMD")
    testbase.assert_equal(devices[1].class_name, "displaycontroller")
    return


# ----------------------------------------------------------------------
# list_usb_devices
# ----------------------------------------------------------------------


def test_list_usb_devices_parses_system_profiler_output():
    """
        list_usb_devices() flattens the bus / device tree from
        ``system_profiler SPUSBDataType``.
    """
    payload = json.dumps({
        "SPUSBDataType": [
            {
                "_name": "USB 3.1 Bus",
                "host_controller": "AppleUSBXHCI",
                "_items": [
                    {
                        "_name": "DataTraveler",
                        "vendor_id": "0x0951  (Kingston)",
                        "product_id": "0x1666",
                        "serial_num": "ABC123",
                        "location_id": "0x14000000 / 1",
                    },
                ],
            }
        ]
    })
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPUSBDataType", "-json",
    )] = _ok(stdout=payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    devices = manager.list_usb_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].name, "USB 3.1 Bus")
    testbase.assert_equal(devices[1].name, "DataTraveler")
    testbase.assert_equal(devices[1].vendor_id, "0951")
    testbase.assert_equal(devices[1].product_id, "1666")
    testbase.assert_equal(devices[1].serial, "ABC123")
    return


# ----------------------------------------------------------------------
# get_firmware_info
# ----------------------------------------------------------------------


def test_get_firmware_info_parses_system_profiler_output():
    """
        get_firmware_info() parses ``system_profiler SPHardwareDataType``
        and consumes ``nvram -p`` for boot args (informational).
    """
    sp_payload = json.dumps({
        "SPHardwareDataType": [
            {
                "_name": "hardware_overview",
                "boot_rom_version": "8419.40.111.0.1",
            }
        ]
    })
    nvram_payload = "boot-args\t-v debug=0x144\n"
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPHardwareDataType", "-json",
    )] = _ok(stdout=sp_payload)
    runner.results[("nvram", "-p")] = _ok(stdout=nvram_payload)

    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )
    info = manager.get_firmware_info()

    testbase.assert_equal(info.vendor, "Apple")
    testbase.assert_equal(info.version, "8419.40.111.0.1")
    testbase.assert_equal(info.uefi, True)
    return


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


def test_failed_command_raises_query_error():
    """
        A non-zero runner result raises
        :class:`MacosHardwareInventoryQueryError` carrying the argv,
        exit code, and stderr.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "system_profiler", "SPHardwareDataType", "-json",
    )] = _fail(exit_code=1, stderr="permission denied")
    manager = MacosHardwareInventoryManager(
        runner, MacosHardwareInventoryManagerConfig(),
    )

    raised = False
    try:
        manager.get_system_info()
    except MacosHardwareInventoryQueryError as ex:
        raised = True
        testbase.assert_equal(ex.exit_code, 1)
        testbase.assert_equal(
            ex.command,
            ["system_profiler", "SPHardwareDataType", "-json"],
        )
        testbase.assert_true("permission" in ex.stderr)

    testbase.assert_true(raised)
    return


def test_reads_never_elevate():
    """
        Every read verb dispatches with ``elevated=False`` regardless of
        ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout="{}")
    runner.results[(
        "system_profiler", "SPHardwareDataType", "-json",
    )] = _ok(stdout='{"SPHardwareDataType": []}')
    runner.results[("sysctl", "-a", "machdep.cpu")] = _ok(stdout="")
    runner.results[(
        "sysctl", "hw.physicalcpu", "hw.logicalcpu", "hw.cpufrequency",
    )] = _ok(stdout="")
    runner.results[(
        "system_profiler", "SPMemoryDataType", "-json",
    )] = _ok(stdout='{"SPMemoryDataType": []}')
    runner.results[("sysctl", "hw.memsize")] = _ok(stdout="hw.memsize: 1\n")
    runner.results[(
        "system_profiler", "SPStorageDataType", "-json",
    )] = _ok(stdout='{"SPStorageDataType": []}')
    runner.results[(
        "system_profiler", "SPNetworkDataType", "-json",
    )] = _ok(stdout='{"SPNetworkDataType": []}')
    runner.results[(
        "networksetup", "-listallhardwareports",
    )] = _ok(stdout="")
    runner.results[(
        "system_profiler", "SPPCIDataType", "-json",
    )] = _ok(stdout='{"SPPCIDataType": []}')
    runner.results[(
        "system_profiler", "SPUSBDataType", "-json",
    )] = _ok(stdout='{"SPUSBDataType": []}')
    runner.results[("nvram", "-p")] = _ok(stdout="")

    config = MacosHardwareInventoryManagerConfig(elevated=True)
    manager = MacosHardwareInventoryManager(runner, config)

    manager.get_system_info()
    manager.get_cpu_info()
    manager.get_memory_info()
    manager.list_storage_devices()
    manager.list_network_devices()
    manager.list_pci_devices()
    manager.list_usb_devices()
    manager.get_firmware_info()

    elevated_calls: list = []
    for record in runner.calls:
        if record["elevated"] is True:
            elevated_calls.append(record)

    testbase.assert_equal(len(elevated_calls), 0)
    return
