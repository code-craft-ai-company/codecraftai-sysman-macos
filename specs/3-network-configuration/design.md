# Design

## Overview

The network configuration feature is implemented as a single module,
`automojo.sysman.macos.networkconfiguration`, that exposes a
`MacosNetworkConfigurationManager` class.  This spec implements the
network-configuration contract defined at
`automojo-sysman-common/specs/contracts/network-configuration/contract.md` for
macOS using `networksetup` as the primary persistent surface (it writes to the
System Configuration framework, which `configd` persists on its own), `ifconfig`
and BSD `route`/`netstat -rn` for runtime state, and `scutil` for the three
hostname surfaces macOS maintains (`ComputerName`, `LocalHostName`, `HostName`).

All read paths used for idempotency guards go through `networksetup -getinfo` /
`-getdnsservers` / `-getsearchdomains`, `ifconfig`, `netstat -rn`, and
`scutil --get`.  All system interaction is delegated to a command runner
implementing the `ICommandRunner` interface.  No `IFileOperator` is required;
`networksetup` owns its own persistence layer.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosNetworkConfigurationManager     │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│      MacosNetworkConfigurationManager                       │
│  automojo.sysman.macos.networkconfiguration                 │
│                                                             │
│  get_interfaces()    get_interface()                        │
│  configure_interface()                                      │
│  enable_interface()  disable_interface()                    │
│  add_route()         remove_route()      get_routes()       │
│  set_dns_servers()   get_dns_servers()                      │
│  set_search_domains()                                       │
│  get_hostname()      set_hostname()                         │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect  - RetryAspect  - TimeoutAspect        │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosNetworkConfigurationManager`

**Location:** `source/packages/automojo/sysman/macos/networkconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_interfaces` | `() → list[MacosInterfaceInfo]` | `networksetup -listallhardwareports` + per-device `ifconfig` |
| `get_interface` | `(name) → MacosInterfaceInfo \| None` | `ifconfig <name>` |
| `configure_interface` | `(name, address, netmask, gateway) → MacosNetworkActionResult` | `networksetup -setmanual` |
| `enable_interface` | `(name) → MacosNetworkActionResult` | `networksetup -setnetworkserviceenabled on` |
| `disable_interface` | `(name) → MacosNetworkActionResult` | `networksetup -setnetworkserviceenabled off` |
| `add_route` | `(dest, gateway, interface) → MacosNetworkActionResult` | `route -n add` |
| `remove_route` | `(dest) → MacosNetworkActionResult` | `route -n delete` |
| `get_routes` | `() → list[MacosRouteInfo]` | `netstat -rn` |
| `set_dns_servers` | `(servers) → MacosNetworkActionResult` | `networksetup -setdnsservers` |
| `get_dns_servers` | `() → list[str]` | `networksetup -getdnsservers` |
| `set_search_domains` | `(domains) → MacosNetworkActionResult` | `networksetup -setsearchdomains` |
| `get_hostname` | `() → str` | `scutil --get ComputerName` |
| `set_hostname` | `(name) → MacosNetworkActionResult` | three `scutil --set` calls |

### `MacosNetworkConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

### Result Types

```python
class MacosNetworkActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosNetworkActionResult:
    status: MacosNetworkActionStatus
    interface: str | None
    detail: str | None
    diff: str | None                    # unused on macOS (no file-backed writes)
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Interface / Route Types

```python
@dataclass
class MacosInterfaceInfo:
    name: str                           # BSD ifname (en0, en1, ...)
    mac_address: str | None
    addresses: list[str]                # <addr>/<prefix>
    gateway: str | None
    state: str                          # "up" / "down"
    mtu: int | None

@dataclass
class MacosRouteInfo:
    destination: str
    gateway: str | None
    interface: str | None
    metric: int | None                  # always None on macOS
```

### Exceptions

```python
class MacosNetworkQueryError(Exception):
    """Raised when a networksetup / ifconfig / scutil query fails unexpectedly."""
```

---

## Interface-Name Mapping

macOS has two parallel naming surfaces:

- **BSD device names** (`en0`, `en1`, `bridge0`, `utun0`, …) — used by
  `ifconfig`, `route`, `netstat`.
- **Network-service names** (`Wi-Fi`, `Ethernet`, `USB 10/100/1000 LAN`, …) —
  used by `networksetup`.

The manager accepts the BSD device name as the canonical `name` parameter and
internally resolves it to the service name by scanning `networksetup
-listallhardwareports` output, which yields blocks like:

```
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: aa:bb:cc:dd:ee:ff
```

The mapping is cached for the lifetime of a single `MacosNetworkConfigurationManager`
instance to avoid repeating the `networksetup` listing on every verb.

Writes reach both runtime and persistent state naturally:

- `networksetup` writes to `configd`, which reconciles to the running interface.
- `ifconfig` / `route` writes affect runtime state only.

Because `networksetup` already covers both surfaces for addressing and DNS, the
manager does NOT add runtime-only fallbacks except for `add_route` / `remove_route`
(where macOS has no persistent-route equivalent — see the Extensions section).

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| configure_interface | `networksetup -setmanual <servicename> <addr> <netmask> <gw_or_empty>` |
| enable_interface | `networksetup -setnetworkserviceenabled <servicename> on` |
| disable_interface | `networksetup -setnetworkserviceenabled <servicename> off` |
| add_route | `route -n add <dest> <gateway> [-ifscope <interface>]` |
| remove_route | `route -n delete <dest>` |
| set_dns_servers | `networksetup -setdnsservers <servicename> <servers...>` (or `empty`) |
| set_search_domains | `networksetup -setsearchdomains <servicename> <domains...>` (or `empty`) |
| set_hostname | three `scutil --set` commands: `ComputerName`, `LocalHostName`, `HostName` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_interfaces | `networksetup -listallhardwareports` + per-device `ifconfig <dev>` |
| get_interface | `ifconfig <name>` |
| get_routes | `netstat -rn` |
| get_dns_servers | `networksetup -getdnsservers <servicename>` |
| get_hostname | `scutil --get ComputerName` |
| service-list (for enable/disable guard) | `networksetup -listallnetworkservices` |

All writes pass `elevated=True` when the config enables it.  Reads never elevate.

---

## Output Parsing

### `networksetup -listallhardwareports` output

```
Hardware Port: Ethernet
Device: en0
Ethernet Address: 00:11:22:33:44:55

Hardware Port: Wi-Fi
Device: en1
Ethernet Address: 00:aa:bb:cc:dd:ee
```

The parser splits on blank lines into blocks; from each block extracts
`hardware_port`, `device`, `mac_address` by matching `^<label>: ` prefixes.  The
result is a dict keyed by device name.

### `networksetup -listallnetworkservices` output

```
An asterisk (*) denotes that a network service is disabled.
Ethernet
Wi-Fi
*USB 10/100/1000 LAN
```

Parser rule: skip the first line; each remaining line is a service name; a
leading `*` means "disabled".

### `ifconfig <device>` output

```
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ether 00:11:22:33:44:55
    inet 10.0.1.50 netmask 0xffffff00 broadcast 10.0.1.255
    inet6 fe80::...%en0 prefixlen 64 scopeid 0x4
```

Parser rules:

- First line: extract `mtu` (integer after `mtu `) and `state` (`"up"` iff the
  flags-angle-bracket string contains `UP`).
- `ether ...` → `mac_address`.
- `inet <addr> netmask 0x<hex>` — convert the hex netmask to a prefix length by
  counting set bits; emit `<addr>/<prefix>`.
- `inet6 <addr>[%iface] prefixlen <n>` — emit `<addr>/<n>`.
- Link-local (`fe80::.../64`) is included; loopback (`lo0`) has no `ether` line.

### `netstat -rn` output (truncated)

```
Internet:
Destination        Gateway            Flags        Netif
default            10.0.1.1           UGScg        en0
10.0.1/24          link#4             UCS          en0
10.0.1.1           0:11:22:33:44:55   UHLWIir      en0
```

Parser rules:

- Skip through the `Destination … Netif` header, then read rows of the table.
- `default` → `destination = "0.0.0.0/0"` (IPv4) or `::/0` (IPv6, in the IPv6
  section).
- `Gateway` that starts with `link#` or looks like a MAC address is treated as
  "link-local / none" and produces `gateway = None`.
- `Netif` → `interface`.

### `networksetup -getinfo <servicename>` output

```
Manual Configuration
IP address: 10.0.1.50
Subnet mask: 255.255.255.0
Router: 10.0.1.1
IPv6: Automatic
IPv6 IP address: none
IPv6 Router: none
Wi-Fi ID: 00:aa:bb:cc:dd:ee
```

The parser extracts `IP address`, `Subnet mask`, and `Router` by line prefix;
these are compared against the requested `(address, netmask, gateway)` to
implement the `configure_interface` idempotency guard.

### `scutil --get` output

Single-line output — the parser strips and returns it.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `configure_interface` | current `networksetup -getinfo` already matches `(address, netmask, gateway)` | `networksetup -getinfo <servicename>` |
| `enable_interface` | service already enabled (no `*` in listing) | `networksetup -listallnetworkservices` |
| `disable_interface` | service already disabled (`*` in listing) | `networksetup -listallnetworkservices` |
| `add_route` | equivalent route already present | `get_routes()` |
| `remove_route` | no route for `dest` | `get_routes()` |
| `set_dns_servers` | current list already matches | `get_dns_servers()` |
| `set_search_domains` | current list already matches | `networksetup -getsearchdomains` |
| `set_hostname` | all three `scutil --get` values already equal `<name>` (LocalHostName compared after `.` → `-` normalisation) | `scutil --get` ×3 |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosNetworkQueryError` | `networksetup`, `ifconfig`, `netstat`, `scutil` exits non-zero for a reason other than "interface does not exist" |

All write failures are surfaced as
`MacosNetworkActionResult(status=FAILED, ...)` carrying the runner's exit code,
stdout, stderr, and a `message` identifying the failing `scutil` surface for
`set_hostname`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: get_interfaces Hardware-Port Enumeration

*For any* `networksetup -listallhardwareports` output, `get_interfaces()` should enumerate every `Device:` entry and invoke `ifconfig <device>` for each.

**Validates: Requirements 1.1**

### Property 2: get_interfaces ifconfig Parsing

*For any* `ifconfig <device>` output, the parser should extract `mac_address`, `addresses` (with hex-netmask → prefix-length conversion for IPv4), `state`, and `mtu`.

**Validates: Requirements 1.2**

### Property 3: get_interface Missing Returns None

*For any* `get_interface(name)` where `ifconfig` exits non-zero with "interface does not exist", the return should be `None`.

**Validates: Requirements 2.3**

### Property 4: configure_interface Service-Name Resolution

*For any* `configure_interface(name, ...)` call, `name` (BSD device) should be resolved to its service name via the cached `networksetup -listallhardwareports` map before dispatching `networksetup -setmanual`.

**Validates: Requirements 3.1**

### Property 5: configure_interface Empty-Gateway Handling

*For any* `configure_interface(name, address, netmask, gateway=None)` call, the dispatched argv should contain an empty string `""` in the gateway position, as required by `networksetup -setmanual`.

**Validates: Requirements 3.2**

### Property 6: configure_interface Idempotency

*For any* `networksetup -getinfo` already reporting the requested `(address, netmask, gateway)`, `configure_interface()` should return `SKIPPED`.

**Validates: Requirements 3.3**

### Property 7: enable_interface Idempotency

*For any* service not prefixed with `*` in `-listallnetworkservices`, `enable_interface()` should return `SKIPPED`.

**Validates: Requirements 4.2**

### Property 8: disable_interface Idempotency

*For any* service prefixed with `*`, `disable_interface()` should return `SKIPPED`.

**Validates: Requirements 5.2**

### Property 9: add_route Runtime-Only Message

*For any* successful `add_route()` call, the result's `message` should note that macOS routes are runtime-only.

**Validates: Requirements 6.3**

### Property 10: add_route Idempotency

*For any* route already present in `netstat -rn`, `add_route()` should return `SKIPPED`.

**Validates: Requirements 6.2**

### Property 11: remove_route Idempotency

*For any* destination absent from `netstat -rn`, `remove_route()` should return `SKIPPED`.

**Validates: Requirements 7.2**

### Property 12: get_routes netstat Parsing

*For any* `netstat -rn` output, `get_routes()` should map `default` to `0.0.0.0/0` in the IPv4 section and `::/0` in the IPv6 section, and should treat `link#N` gateways as `None`.

**Validates: Requirements 8.2**

### Property 13: set_dns_servers Empty Clears

*For any* `set_dns_servers([])` call, the dispatched argv should end with the literal `empty` token (the string `networksetup` requires to clear the DNS list).

**Validates: Requirements 9.1**

### Property 14: set_dns_servers Idempotency

*For any* DNS-server list already matching `get_dns_servers()`, `set_dns_servers()` should return `SKIPPED`.

**Validates: Requirements 9.2**

### Property 15: get_dns_servers Empty Parsing

*For any* `networksetup -getdnsservers` output matching `There aren't any DNS Servers set on <servicename>.`, `get_dns_servers()` should return `[]`.

**Validates: Requirements 10.2**

### Property 16: set_search_domains Empty Clears

*For any* `set_search_domains([])` call, the dispatched argv should end with the literal `empty` token.

**Validates: Requirements 11.1**

### Property 17: set_search_domains Idempotency

*For any* search-domain list already matching current state, `set_search_domains()` should return `SKIPPED`.

**Validates: Requirements 11.2**

### Property 18: get_hostname Returns ComputerName

*For any* successful `scutil --get ComputerName`, `get_hostname()` should return the stripped stdout.

**Validates: Requirements 12.1, 12.2**

### Property 19: set_hostname Triple Dispatch

*For any* `set_hostname(name)` call, three `scutil --set` commands should be dispatched: `ComputerName`, `LocalHostName` (with `.` → `-` normalisation), `HostName`.

**Validates: Requirements 13.1**

### Property 20: set_hostname Idempotency

*For any* `name` already equal to all three current values (LocalHostName after `.` → `-`), `set_hostname()` should return `SKIPPED`.

**Validates: Requirements 13.2**

### Property 21: set_hostname Short-Circuits On Failure With Message

*For any* `scutil --set` call that fails, `set_hostname()` should stop further dispatch and return `FAILED` with `message` identifying which surface failed.

**Validates: Requirements 13.4**

### Property 22: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked; the result status MUST be `DRY_RUN`.

**Validates: Requirements 3.6, 4.5, 5.5, 6.5, 7.5, 9.5, 11.5, 13.5**

### Property 23: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner MUST be invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 24: No Elevation for Reads

*For any* read (`get_interfaces`, `get_interface`, `get_routes`, `get_dns_servers`, `get_hostname`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 25: Service-Name Map Cached

*For any* instance of the manager, `networksetup -listallhardwareports` is invoked at most once per verb that needs the BSD-to-service mapping (cached on the instance).

**Validates: Requirements 1.1, 3.1**

### Property 26: Error Result Consistency

*For any* non-zero exit from a write command, the returned `MacosNetworkActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 3.5, 4.4, 5.4, 6.4, 7.4, 9.4, 11.4, 13.4**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── networkconfiguration.py     ← MacosNetworkConfigurationManager, config,
                                   models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_networkconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.networkconfiguration import (
    MacosNetworkConfigurationManager,
    MacosNetworkConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = MacosNetworkConfigurationManagerConfig(elevated=True, dry_run=False)
net_mgr = MacosNetworkConfigurationManager(runner=runner, config=config)

net_mgr.configure_interface(
    name="en0", address="10.0.1.50", netmask="255.255.255.0",
    gateway="10.0.1.1",
)

net_mgr.set_dns_servers(servers=["1.1.1.1", "8.8.8.8"])
net_mgr.set_search_domains(domains=["example.com"])
net_mgr.set_hostname(name="mac-worker-01.example.com")

for iface in net_mgr.get_interfaces():
    print(f"{iface.name}\t{iface.state}\t{iface.addresses}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.networkconfiguration import (
    MacosNetworkConfigurationManager, MacosNetworkConfigurationManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

net_mgr = MacosNetworkConfigurationManager(
    runner=runner,
    config=MacosNetworkConfigurationManagerConfig(elevated=True),
)

net_mgr.add_route(dest="172.16.0.0/12", gateway="10.0.1.254", interface="en0")
```

### Dry-Run Mode

```python
config = MacosNetworkConfigurationManagerConfig(elevated=True, dry_run=True)
net_mgr = MacosNetworkConfigurationManager(runner=runner, config=config)

result = net_mgr.set_hostname(name="mac-worker-02.example.com")
assert result.status == MacosNetworkActionStatus.DRY_RUN
print(result.command)
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on argv prefix.
- `networksetup -listallhardwareports` mock drives BSD-to-service-name mapping
  tests and the caching behavior.
- `ifconfig` mock outputs cover IPv4 + IPv6 addresses, the hex-netmask → prefix
  conversion, and loopback (missing `ether`).
- `netstat -rn` mock output includes both the IPv4 `Internet:` and IPv6
  `Internet6:` sections, asserting `default` normalisation and `link#N` →
  `None` gateway handling.
- `networksetup -getinfo` / `-getdnsservers` / `-getsearchdomains` mock outputs
  drive `configure_interface`, `set_dns_servers`, and `set_search_domains`
  idempotency tests.
- `scutil --get` mocks drive `set_hostname` idempotency; a dedicated test
  asserts the `.` → `-` normalisation rule for LocalHostName.
- Dry-run tests prove every write short-circuits to `DRY_RUN` without invoking
  the runner.
- Elevation tests prove writes pass `elevated=True` when configured and reads
  never do.
- No test requires root access or a live macOS System Configuration database.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `get_interfaces` | `networksetup -listallhardwareports` + `ifconfig` |
| `get_interface` | `ifconfig <name>` |
| `configure_interface` | `networksetup -setmanual` |
| `enable_interface` | `networksetup -setnetworkserviceenabled on` |
| `disable_interface` | `networksetup -setnetworkserviceenabled off` |
| `add_route` | `route -n add` (runtime only) |
| `remove_route` | `route -n delete` |
| `get_routes` | `netstat -rn` |
| `set_dns_servers` | `networksetup -setdnsservers` |
| `get_dns_servers` | `networksetup -getdnsservers` |
| `set_search_domains` | `networksetup -setsearchdomains` |
| `get_hostname` | `scutil --get ComputerName` |
| `set_hostname` | three `scutil --set` calls (ComputerName, LocalHostName, HostName) |

---

## Extensions Beyond the Contract

### Persistent-Route Extension

macOS has no `/etc/rc.local` equivalent and `route` writes are runtime-only.  A
future extension will render a launchd plist (for example
`/Library/LaunchDaemons/com.automojo.route.<label>.plist`) that invokes `route
add` at boot, via the same `IFileOperator` + `launchctl bootstrap` pattern as
the filesystem-management spec's persistence extension.  This extension is
deferred; when added it will live behind its own feature flag.
