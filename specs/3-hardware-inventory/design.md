# Design

## Overview

This spec implements the hardware-inventory contract defined at
`codecraftai-sysman-common/specs/contracts/hardware-inventory/contract.md` for
macOS using `system_profiler`, `sysctl`, `networksetup`, and `nvram`.

The hardware-inventory feature is implemented as a single module,
`ccai.sysman.macos.hardwareinventory`, that exposes a
`MacosHardwareInventoryManager` class.  The coordinator instantiates
`MacosHardwareInventoryManager` and calls its read verbs to query discrete
hardware aspects.  Every verb returns a typed `Macos*` dataclass, or raises
`MacosHardwareInventoryQueryError` when the underlying command fails or the
captured output cannot be parsed.

This feature is strictly **read-only**.  Per the cross-OS contract:

- `MacosHardwareInventoryActionStatus` enumerates `SUCCESS`, `FAILED`, and
  `SKIPPED` only; there is no `DRY_RUN` member.
- `MacosHardwareInventoryManagerConfig` inherits `elevated` and `dry_run`
  from `FeatureConfig`, but neither field gates any verb in the current
  pilot.  Every runner invocation passes `elevated=False`.
- No verb mutates host state, so dry-run would have no observable effect.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosHardwareInventoryManager        │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        MacosHardwareInventoryManager                        │
│  ccai.sysman.macos.hardwareinventory                    │
│                                                             │
│  get_system_info()       get_cpu_info()                     │
│  get_memory_info()       list_storage_devices()             │
│  list_network_devices()  list_pci_devices()                 │
│  list_usb_devices()      get_firmware_info()                │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated=False, ...) → RunResult                 │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosHardwareInventoryManager`

**Location:** `source/packages/ccai/sysman/macos/hardwareinventory.py`

The primary public class.  Constructed with an `ICommandRunner` and a
`MacosHardwareInventoryManagerConfig`.

| Method | Returns | Description |
|---|---|---|
| `get_system_info()` | `MacosSystemInfo` | Run `system_profiler SPHardwareDataType -json`, parse hardware overview |
| `get_cpu_info()` | `MacosCpuInfo` | Run `sysctl -a machdep.cpu` and `sysctl hw.physicalcpu hw.logicalcpu hw.cpufrequency` |
| `get_memory_info()` | `MacosMemoryInfo` | Run `system_profiler SPMemoryDataType -json` and `sysctl hw.memsize` |
| `list_storage_devices()` | `list[MacosStorageDeviceInfo]` | Run `system_profiler SPStorageDataType -json` |
| `list_network_devices()` | `list[MacosNetworkDeviceInfo]` | Run `system_profiler SPNetworkDataType -json` and `networksetup -listallhardwareports` |
| `list_pci_devices()` | `list[MacosPciDeviceInfo]` | Run `system_profiler SPPCIDataType -json` |
| `list_usb_devices()` | `list[MacosUsbDeviceInfo]` | Run `system_profiler SPUSBDataType -json`, flatten the bus / device tree |
| `get_firmware_info()` | `MacosFirmwareInfo` | Run `system_profiler SPHardwareDataType -json` and `nvram -p` |

### `MacosHardwareInventoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Inherited from `FeatureConfig`; ignored — feature is read-only |
| `elevated` | `bool` | `False` | Inherited from `FeatureConfig`; reserved for selective elevation, not consulted by any verb today |

---

## Data Models

```python
class MacosHardwareInventoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    # DRY_RUN intentionally absent — feature is read-only.


class MacosHardwareInventoryQueryError(Exception):
    command: list[str]
    exit_code: int | None
    stderr: str


@dataclass
class MacosSystemInfo:
    manufacturer: str | None
    product_name: str | None
    serial_number: str | None
    uuid: str | None
    bios_version: str | None
    firmware_version: str | None
    family: str | None
    version: str | None


@dataclass
class MacosCpuInfo:
    model: str | None
    vendor: str | None
    architecture: str | None
    physical_cores: int
    logical_cores: int
    frequency_mhz: int | None
    cache_l1: int | None
    cache_l2: int | None
    cache_l3: int | None
    flags: list[str]


@dataclass
class MacosMemoryModule:
    slot: str | None
    size_bytes: int
    type: str | None
    speed_mhz: int | None
    manufacturer: str | None
    serial_number: str | None


@dataclass
class MacosMemoryInfo:
    total_bytes: int
    available_bytes: int | None
    slots_used: int | None
    slots_total: int | None
    modules: list[MacosMemoryModule]


@dataclass
class MacosStorageDeviceInfo:
    name: str
    size_bytes: int
    type: str
    model: str | None
    serial: str | None
    mountpoint: str | None
    filesystem: str | None


@dataclass
class MacosNetworkDeviceInfo:
    name: str
    mac_address: str | None
    link_state: str | None
    speed: str | None
    mtu: int | None
    is_virtual: bool


@dataclass
class MacosPciDeviceInfo:
    slot: str
    class_name: str | None
    vendor: str | None
    device: str | None
    vendor_id: str | None
    device_id: str | None
    driver: str | None


@dataclass
class MacosUsbDeviceInfo:
    bus: str
    device_id: str | None
    vendor_id: str | None
    product_id: str | None
    name: str | None
    serial: str | None


@dataclass
class MacosFirmwareInfo:
    vendor: str | None
    version: str | None
    release_date: str | None
    secure_boot: bool | None
    uefi: bool | None
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

Same protocol as the rest of the package; see
`ccai.interfaces.icommandrunner`.

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
```

The manager's `_read` helper raises
`MacosHardwareInventoryQueryError` on any non-zero exit code so that
every verb's caller sees a single, typed error path.

---

## Command Mappings

| Verb | Argv |
|---|---|
| `get_system_info` | `["system_profiler", "SPHardwareDataType", "-json"]` |
| `get_cpu_info` | `["sysctl", "-a", "machdep.cpu"]`, then `["sysctl", "hw.physicalcpu", "hw.logicalcpu", "hw.cpufrequency"]` |
| `get_memory_info` | `["system_profiler", "SPMemoryDataType", "-json"]`, then `["sysctl", "hw.memsize"]` |
| `list_storage_devices` | `["system_profiler", "SPStorageDataType", "-json"]` |
| `list_network_devices` | `["system_profiler", "SPNetworkDataType", "-json"]`, then `["networksetup", "-listallhardwareports"]` |
| `list_pci_devices` | `["system_profiler", "SPPCIDataType", "-json"]` |
| `list_usb_devices` | `["system_profiler", "SPUSBDataType", "-json"]` |
| `get_firmware_info` | `["system_profiler", "SPHardwareDataType", "-json"]`, then `["nvram", "-p"]` |

Every invocation passes `elevated=False`.

---

## Output Parsing per Verb

### `get_system_info` — `system_profiler SPHardwareDataType -json`

Real example (excerpt):

```json
{"SPHardwareDataType": [
  {"_name": "hardware_overview",
   "machine_name": "MacBook Pro",
   "machine_model": "MacBookPro18,3",
   "model_number": "MK183LL/A",
   "serial_number": "C02ABC123XYZ",
   "platform_UUID": "AAAA-BBBB-CCCC",
   "boot_rom_version": "8419.40.111.0.1",
   "smc_version_system": "3.0"}
]}
```

Parse rules:

- `manufacturer` is hard-coded to `"Apple Inc."` — Apple-branded hardware
  doesn't surface a manufacturer string in `SPHardwareDataType`.
- `machine_name` → `product_name`; `machine_model` → `family`;
  `model_number` → `version`.
- `boot_rom_version` → `bios_version`; `smc_version_system` →
  `firmware_version`.

### `get_cpu_info` — `sysctl -a machdep.cpu` + `sysctl hw.*`

Real example (excerpt):

```
machdep.cpu.vendor: GenuineIntel
machdep.cpu.brand_string: Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz
machdep.cpu.features: FPU VME DE PSE TSC MSR PAE
hw.physicalcpu: 6
hw.logicalcpu: 12
hw.cpufrequency: 2600000000
```

Parse rules:

- One key=value record per line; the parser splits on the first `:` and
  trims whitespace.
- `machdep.cpu.vendor` / `brand_string` / `features` populate `vendor`,
  `model`, and `flags`.
- `hw.physicalcpu`, `hw.logicalcpu`, and `hw.cpufrequency` populate
  `physical_cores`, `logical_cores`, and `frequency_mhz` (Hz to MHz via
  integer division by 1,000,000).
- Apple silicon hosts return a sparse `machdep.cpu` MIB; missing keys
  leave the corresponding fields at their dataclass defaults rather than
  raising.

### `get_memory_info` — `system_profiler SPMemoryDataType -json` + `sysctl hw.memsize`

Real example (excerpt):

```json
{"SPMemoryDataType": [
  {"_name": "memory_overview",
   "_items": [
     {"_name": "BANK 0/DIMM 0",
      "dimm_size": "16 GB",
      "dimm_type": "DDR4",
      "dimm_speed": "2667 MHz",
      "dimm_manufacturer": "0x80AD",
      "dimm_serial_number": "ABC123"}
   ]}
]}
```

`hw.memsize` is `hw.memsize: 17179869184`.

Parse rules:

- The first `SPMemoryDataType` entry's `_items` array carries one element
  per DIMM.  Empty / not-installed entries report `dimm_size: "Empty"` and
  are skipped.
- `dimm_size` decoder accepts ``"<n> KB|MB|GB|TB"`` and returns bytes.
- `dimm_speed` decoder accepts ``"<n> MHz"`` / ``"<n> MT/s"`` and returns
  integer MHz.
- Apple silicon synthetic entries lack `_items`; the parser falls back to
  the overview entry itself in that case so a single-DIMM record is still
  emitted.
- `slots_used` and `slots_total` both equal the count of populated
  modules (macOS does not surface an unpopulated-slot count comparable to
  SMBIOS Type 16).
- `hw.memsize` populates `total_bytes` directly.

### `list_storage_devices` — `system_profiler SPStorageDataType -json`

Real example (excerpt):

```json
{"SPStorageDataType": [
  {"_name": "Macintosh HD",
   "size_in_bytes": 494384795648,
   "mount_point": "/",
   "file_system": "APFS",
   "physical_drive": {"device_name": "APPLE SSD AP0512Q",
                      "device_serial": "S1AB",
                      "medium_type": "SSD"}}
]}
```

Parse rules:

- One record per `SPStorageDataType` entry.  `_name`, `size_in_bytes`,
  `mount_point`, and `file_system` map directly.
- `physical_drive.medium_type` → `type`; `physical_drive.device_name` →
  `model`; `physical_drive.device_serial` → `serial`.

### `list_network_devices` — `system_profiler SPNetworkDataType -json` + `networksetup -listallhardwareports`

`SPNetworkDataType` excerpt:

```json
[{"_name": "Wi-Fi",
  "interface": "en0",
  "type": "AirPort",
  "Ethernet": {"MAC Address": "1c:1b:b5:11:22:33"},
  "has_ip_assigned": "Yes"}]
```

`networksetup -listallhardwareports` excerpt:

```
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 1c:1b:b5:11:22:33

Hardware Port: USB 10/100/1000 LAN
Device: en6
Ethernet Address: 00:e0:4c:aa:bb:cc
```

Parse rules:

- One record per `SPNetworkDataType` entry.  `interface` → `name`;
  `Ethernet["MAC Address"]` → `mac_address`.
- `link_state` is `"active"` when `has_ip_assigned == "Yes"`; otherwise
  `"inactive"`.
- `is_virtual` is `True` when `type` is `Bridge` / `Bond` / `VPN`, or
  when `interface` begins with `utun`, `lo`, `gif`, or `stf`.
- The hardware-port map provides an optional descriptive `speed` label
  for ports whose name ends in `LAN` or contains `Ethernet`; for Wi-Fi
  and other non-link-rate ports `speed` stays `None`.

### `list_pci_devices` — `system_profiler SPPCIDataType -json`

Real example (excerpt):

```json
[{"_name": "ethernet",
  "sppci_slot_name": "Thunderbolt@22,0,0",
  "sppci_vendor": "Intel",
  "sppci_device_type": "ethernet",
  "sppci_vendor-id": "0x8086",
  "sppci_device-id": "0x1539",
  "sppci_driver": "AppleIntelI210Ethernet"}]
```

Parse rules:

- One record per `SPPCIDataType` entry.  Keys map directly onto the
  dataclass fields.
- Apple silicon hosts often surface an empty list — that is a valid
  result, not an error.

### `list_usb_devices` — `system_profiler SPUSBDataType -json`

Real example (excerpt):

```json
[{"_name": "USB 3.1 Bus",
  "host_controller": "AppleUSBXHCI",
  "_items": [
    {"_name": "DataTraveler",
     "vendor_id": "0x0951  (Kingston)",
     "product_id": "0x1666",
     "serial_num": "ABC123",
     "location_id": "0x14000000 / 1"}
  ]}]
```

Parse rules:

- The bus / device tree is flattened depth-first; root buses surface as
  records with no `serial`.
- `vendor_id` / `product_id` strings are stripped of the leading `0x` and
  any trailing parenthesised vendor name, leaving the four-hex-digit id.
- `serial_num` → `serial`.

### `get_firmware_info` — `system_profiler SPHardwareDataType -json` + `nvram -p`

Parse rules:

- `vendor="Apple"`, `uefi=True`, and `version=boot_rom_version`.
- `release_date` and `secure_boot` are `None`.
- `nvram -p` is consumed but does not currently populate any field; the
  call exists so that a future patch can extract `boot-args`,
  `SecureBootPolicy`, etc.

---

## Error Handling

### Typed Exception

`MacosHardwareInventoryQueryError` carries `command`, `exit_code`, and
`stderr`.  Its message is built via `os.linesep.join()` so that command
arguments, exit code, and stderr are each on their own line.

### Error Sources

| Source | Mapped To |
|---|---|
| Non-zero exit code from runner | `MacosHardwareInventoryQueryError` with raw stderr |
| `json.JSONDecodeError` parsing `system_profiler` output | `MacosHardwareInventoryQueryError` with `message=` describing the parse failure |
| Missing field on a JSON object | Field stays at its dataclass default — does not raise |

### Partial Tolerance

Verbs that issue two commands (`get_cpu_info`, `get_memory_info`,
`list_network_devices`, `get_firmware_info`) raise on the first failure
in the call sequence.  This pilot does not implement the contract's
`collect_snapshot` partial-tolerance behaviour — see tasks.md.

---

## Correctness Properties

### Property 1: Read-Only Discipline

For every verb invocation, every runner call passes `elevated=False`,
regardless of `config.elevated`.  No verb invokes a mutating command.

### Property 2: JSON Parse Robustness

For every verb that consumes JSON, malformed JSON surfaces as
`MacosHardwareInventoryQueryError` with a parse-failure message rather
than a raw `json.JSONDecodeError`.

### Property 3: Empty-Output Safety

For the `system_profiler` listing verbs (storage / network / PCI / USB),
an empty array under the data-type key yields an empty list, not an
exception.

### Property 4: Apple-Silicon Tolerance

For every CPU / memory verb, a sparse or differently shaped Apple silicon
output (missing `machdep.cpu` keys, missing `_items` array) leaves
fields at their dataclass defaults instead of raising.

### Property 5: USB ID Normalisation

For every USB device, the parser strips `0x` and trailing
parenthesised vendor labels from `vendor_id` / `product_id`, leaving a
four-character hex string.

### Property 6: USB Tree Flattening

For every `SPUSBDataType` tree, `list_usb_devices()` emits the root bus
before its descendants and yields exactly one record per node.

### Property 7: Network Virtual Classification

For every network record, `is_virtual=True` when `type` is `Bridge` /
`Bond` / `VPN`, or when `interface` begins with `utun` / `lo` / `gif` /
`stf`.  Physical links (`en*`) report `False`.

### Property 8: No Hidden Dry-Run

For every verb, a `dry_run=True` config does not change the runner-call
sequence.  The feature ignores the `dry_run` flag entirely.

---

## Module Layout

```
source/packages/ccai/sysman/macos/
└── hardwareinventory.py                ← manager + dataclasses + parsers + exception

source/testroots/testbase/ccai/tests/sysman/macos/
└── test_hardwareinventory.py           ← unit tests using a MockCommandRunner
```

---

## Usage Examples

### Local execution

```python
from ccai.sysman.macos.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.macos.hardwareinventory import (
    MacosHardwareInventoryManager,
    MacosHardwareInventoryManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosHardwareInventoryManagerConfig()
inventory = MacosHardwareInventoryManager(runner, config)

system = inventory.get_system_info()
cpu = inventory.get_cpu_info()
memory = inventory.get_memory_info()
storage = inventory.list_storage_devices()
network = inventory.list_network_devices()
pci = inventory.list_pci_devices()
usb = inventory.list_usb_devices()
firmware = inventory.get_firmware_info()
```

### SSH execution

```python
from ccai.sysman.macos.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from ccai.sysman.macos.hardwareinventory import (
    MacosHardwareInventoryManager,
    MacosHardwareInventoryManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(
    host="192.168.1.100", username="admin",
    key_filename="/Users/admin/.ssh/id_rsa",
))
inventory = MacosHardwareInventoryManager(
    runner, MacosHardwareInventoryManagerConfig(),
)
storage = inventory.list_storage_devices()
```

### Aspect composition

```python
from ccai.sysman.macos.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.macos.storage import SQLiteStorage

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))
runner.add_aspect(PersistenceAspect(PersistenceAspectConfig(
    storage_backend=SQLiteStorage("/var/log/maintenance/inventory.db"),
)))
runner.add_aspect(RetryAspect(RetryAspectConfig(
    max_retries=3, retryable_exit_codes=[255],
)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=120.0)))

inventory = MacosHardwareInventoryManager(
    runner, MacosHardwareInventoryManagerConfig(),
)
network = inventory.list_network_devices()
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call and
  returns pre-seeded `RunResult` values keyed by argv tuple.
- Mocks are seeded with canned stdout from real `system_profiler -json`,
  `sysctl`, `networksetup -listallhardwareports`, and `nvram -p`
  captures.
- Tests verify the *arguments* passed to the runner and the
  `elevated=False` flag — not system state.
- Each public verb has at minimum a success-path test demonstrating both
  argv construction and field-level parsing.
- A failure-path test asserts that a non-zero runner result raises
  `MacosHardwareInventoryQueryError` carrying the argv, exit code, and
  stderr.
- A read-elevation test exercises every verb with
  `config.elevated=True` and asserts that no runner call received
  `elevated=True`.

### Test File

`source/testroots/testbase/ccai/tests/sysman/macos/test_hardwareinventory.py`

### Tests Implemented

1. `test_get_system_info_invokes_system_profiler_and_parses`
2. `test_get_cpu_info_parses_sysctl_output`
3. `test_get_memory_info_aggregates_modules_and_total`
4. `test_list_storage_devices_parses_system_profiler_output`
5. `test_list_network_devices_parses_system_profiler_output`
6. `test_list_pci_devices_parses_system_profiler_output`
7. `test_list_usb_devices_parses_system_profiler_output`
8. `test_get_firmware_info_parses_system_profiler_output`
9. `test_failed_command_raises_query_error`
10. `test_reads_never_elevate`
