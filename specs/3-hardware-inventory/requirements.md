# Requirements

## Introduction

This spec implements the hardware-inventory contract defined at
`codecraftai-sysman-common/specs/contracts/hardware-inventory/contract.md` for
macOS using `system_profiler`, `sysctl`, `networksetup`, and `nvram`.

The hardware-inventory feature provides the `codecraftai-sysman-macos` package
with the ability to query, classify, and aggregate hardware and platform identity
information on macOS systems: hardware identity, CPU topology, memory (aggregate
plus per-DIMM detail where the platform exposes it), storage volumes, network
link-layer hardware, PCI devices, USB devices, and firmware identity.

A maintenance coordinator drives every operation by calling into this feature's
public interfaces.  All system commands are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction, enabling auditing, tracing, and
test mocking without requiring a live macOS host.

This feature is **strictly read-only**.  Every verb is inherently idempotent and
every verb is safe to repeat.  As a consequence:

- There is no dry-run distinction from normal behavior — no command issued by
  this module mutates host state.  The `dry_run` field is inherited from
  `FeatureConfig` for structural uniformity but is not honored.
- There is no `DRY_RUN` member on `MacosHardwareInventoryActionStatus`.
- Every read passes `elevated=False` to the runner regardless of the
  `elevated` config flag.  The `elevated` field is reserved for the
  selective-elevation patch documented in `tasks.md`.

Verbs are READ-ONLY.  Failures surface as
`MacosHardwareInventoryQueryError`, never as a per-verb result dataclass.

---

## Requirements

### Requirement 1: Query System Identity

**User Story:** As a maintenance coordinator, I want to query the macOS hardware
identity (manufacturer, machine name, model, serial, UUID, BIOS / SMC version),
so that I can record host identity in a maintenance report.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_info()` THE SYSTEM SHALL invoke
   `system_profiler SPHardwareDataType -json` via the command runner with
   `elevated=False`.
2. THE SYSTEM SHALL JSON-decode the payload, take the first item from the
   `SPHardwareDataType` array, and populate `manufacturer="Apple Inc."`,
   `product_name=machine_name`, `family=machine_model`,
   `version=model_number`, `serial_number=serial_number`,
   `uuid=platform_UUID`, `bios_version=boot_rom_version`, and
   `firmware_version=smc_version_system`.
3. WHEN `system_profiler` returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError` carrying the argv, exit code, and stderr.
4. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError` with a parse-failure message.

---

### Requirement 2: Query CPU Topology and Features

**User Story:** As a maintenance coordinator, I want to query CPU topology
(physical / logical cores, model, vendor, frequency, feature flags), so that
I can record the CPU surface across both Intel and Apple silicon Macs.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cpu_info()` THE SYSTEM SHALL invoke
   `sysctl -a machdep.cpu` and `sysctl hw.physicalcpu hw.logicalcpu
   hw.cpufrequency` in that order.
2. THE SYSTEM SHALL parse `machdep.cpu.vendor`, `machdep.cpu.brand_string`,
   and `machdep.cpu.features` into `vendor`, `model`, and `flags`.
3. THE SYSTEM SHALL parse `hw.physicalcpu`, `hw.logicalcpu`, and
   `hw.cpufrequency` into `physical_cores`, `logical_cores`, and
   `frequency_mhz`, converting the `hw.cpufrequency` Hz value to MHz via
   integer division by 1,000,000.
4. WHEN either `sysctl` call returns a non-zero exit code THE SYSTEM SHALL
   raise `MacosHardwareInventoryQueryError`.
5. WHEN any expected `machdep.cpu` key is absent (e.g., on Apple silicon)
   THE SYSTEM SHALL leave the corresponding field at its dataclass default
   without raising.

---

### Requirement 3: Query Memory (Aggregate and Per-DIMM)

**User Story:** As a maintenance coordinator, I want to query installed memory
and per-DIMM detail, so that I can confirm slot population and DRAM speed
across both desktop Macs (with replaceable DIMMs) and laptops / Apple silicon
(with on-package memory).

#### Acceptance Criteria

1. WHEN the coordinator calls `get_memory_info()` THE SYSTEM SHALL invoke
   `system_profiler SPMemoryDataType -json` and `sysctl hw.memsize` in that
   order.
2. THE SYSTEM SHALL JSON-decode the `SPMemoryDataType` array, take the first
   item's `_items` list (or fall back to the item itself when `_items` is
   empty), and emit one `MacosMemoryModule` per entry whose `dimm_size`
   parses to a non-zero byte count.
3. THE SYSTEM SHALL convert `dimm_size` strings like `"16 GB"` /
   `"512 MB"` to bytes.
4. THE SYSTEM SHALL convert `dimm_speed` strings like `"2667 MHz"` to
   integer MHz.
5. THE SYSTEM SHALL set `slots_used` and `slots_total` to the count of
   populated modules.
6. THE SYSTEM SHALL parse `hw.memsize` (raw byte count) into `total_bytes`.
7. WHEN either command returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.

---

### Requirement 4: Enumerate Storage Devices

**User Story:** As a maintenance coordinator, I want to enumerate storage
volumes with name, size, type, model, serial, mount point, and filesystem,
so that the storage layout is captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_storage_devices()` THE SYSTEM SHALL
   invoke `system_profiler SPStorageDataType -json`.
2. THE SYSTEM SHALL JSON-decode the payload and emit one
   `MacosStorageDeviceInfo` per entry in `SPStorageDataType`.
3. THE SYSTEM SHALL populate `name=_name`, `size_bytes=size_in_bytes`,
   `mountpoint=mount_point`, and `filesystem=file_system`, and read
   `physical_drive.medium_type`, `physical_drive.device_name`, and
   `physical_drive.device_serial` into `type`, `model`, and `serial`.
4. WHEN `system_profiler` returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.
5. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.

---

### Requirement 5: Enumerate Network Devices

**User Story:** As a maintenance coordinator, I want to enumerate network
interfaces with BSD name, MAC address, link state, and a virtual / physical
classification, so that the network adapter layout is captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_network_devices()` THE SYSTEM SHALL
   invoke `system_profiler SPNetworkDataType -json` and
   `networksetup -listallhardwareports` in that order.
2. THE SYSTEM SHALL emit one `MacosNetworkDeviceInfo` per
   `SPNetworkDataType` entry.
3. THE SYSTEM SHALL populate `name=interface`,
   `mac_address=Ethernet["MAC Address"]`, and
   `link_state="active"|"inactive"` based on the
   `has_ip_assigned` field.
4. THE SYSTEM SHALL set `is_virtual=True` when `type` is `Bridge`,
   `Bond`, or `VPN`, or when `interface` begins with `utun`, `lo`,
   `gif`, or `stf`.
5. THE SYSTEM SHALL consult the BSD-name -> hardware-port map from
   `networksetup` for an optional `speed` label when the port string
   ends in `LAN` or contains `Ethernet`.
6. WHEN either command returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.

---

### Requirement 6: Enumerate PCI Devices

**User Story:** As a maintenance coordinator, I want to enumerate PCI devices
with slot, vendor, device, vendor / device IDs, and bound driver, so that
the PCI inventory is captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_pci_devices()` THE SYSTEM SHALL invoke
   `system_profiler SPPCIDataType -json`.
2. THE SYSTEM SHALL emit one `MacosPciDeviceInfo` per entry, populating
   `slot=sppci_slot_name`, `vendor=sppci_vendor`,
   `class_name=sppci_device_type`, `device=_name`,
   `vendor_id=sppci_vendor-id`, `device_id=sppci_device-id`, and
   `driver=sppci_driver`.
3. WHEN `system_profiler` returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.
4. WHEN `SPPCIDataType` is absent or empty THE SYSTEM SHALL return an empty
   list without raising.

---

### Requirement 7: Enumerate USB Devices

**User Story:** As a maintenance coordinator, I want to enumerate USB devices
across the host's USB buses, so that attached peripherals are captured.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_usb_devices()` THE SYSTEM SHALL invoke
   `system_profiler SPUSBDataType -json`.
2. THE SYSTEM SHALL flatten the bus / device tree depth-first, emitting one
   `MacosUsbDeviceInfo` per node (root buses included).
3. THE SYSTEM SHALL strip the `0x` prefix from `vendor_id` / `product_id`
   strings and drop any trailing parenthesised vendor name, leaving only the
   four-hex-digit identifier.
4. THE SYSTEM SHALL populate `serial` from `serial_num` when present;
   otherwise `serial` is `None`.
5. WHEN `system_profiler` returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.

---

### Requirement 8: Query Firmware

**User Story:** As a maintenance coordinator, I want to query the macOS
firmware identity (vendor, version, UEFI flag), so that firmware updates
can be scoped.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_firmware_info()` THE SYSTEM SHALL invoke
   `system_profiler SPHardwareDataType -json` and `nvram -p` in that order.
2. THE SYSTEM SHALL set `vendor="Apple"`, `version=boot_rom_version`, and
   `uefi=True` (every modern Mac boots via iBoot / EFI).
3. THE SYSTEM SHALL leave `secure_boot` as `None`; the proper read is via
   `bputil` and is reserved as a follow-up.
4. THE SYSTEM SHALL leave `release_date` as `None`; macOS does not surface a
   firmware release date the way SMBIOS does.
5. THE SYSTEM SHALL consume `nvram -p` for boot-args informational glue but
   SHALL NOT mutate any field based on its presence.
6. WHEN either command returns a non-zero exit code THE SYSTEM SHALL raise
   `MacosHardwareInventoryQueryError`.

---

### Requirement 9: Read-Only Discipline

**User Story:** As a maintenance coordinator, I want every hardware-inventory
verb to be strictly read-only, so that I can run it under aspect compositions
without risking host mutation.

#### Acceptance Criteria

1. THE `MacosHardwareInventoryActionStatus` enum SHALL contain only
   `SUCCESS`, `FAILED`, and `SKIPPED` members; `DRY_RUN` is intentionally
   absent.
2. `MacosHardwareInventoryManagerConfig` SHALL inherit `dry_run` and
   `elevated` from `FeatureConfig` for structural uniformity but SHALL NOT
   consult either field in any verb in the current pilot.
3. Every runner invocation in this module SHALL pass `elevated=False`.
4. No verb in this module SHALL invoke a command that mutates host state.

---

### Requirement 10: Aspect-Transparent Execution

**User Story:** As a maintenance coordinator, I want every hardware-inventory
verb to be safe under retry / persistence / timeout aspects, so that I can
collect inventory across a fleet under whatever runner configuration the
coordinator already has set up.

#### Acceptance Criteria

1. THE manager SHALL hold no state across calls beyond its injected runner and
   config.
2. Each public verb SHALL parse only its own command's output; it SHALL NOT
   read across verbs.
3. WHEN a retry aspect re-runs a transient failure THE SYSTEM SHALL produce
   the same dataclass shape from the retried success that it would have
   produced from a first-try success.
