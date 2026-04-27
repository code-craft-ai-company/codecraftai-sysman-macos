# Requirements

## Introduction

The network configuration feature provides the `automojo-sysman-macos` package with
the ability to query and configure network services, routes, DNS servers, search
domains, and the system hostname on macOS systems.  This spec implements the
network-configuration contract defined at
`automojo-sysman-common/specs/contracts/network-configuration/contract.md` for
macOS using `networksetup` as the primary persistent surface (it writes to the
System Configuration framework, which `configd` persists), `ifconfig` for runtime
state of BSD interfaces, `route` for the routing table, and `scutil` for
hostname-related state.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
services should be on or which IPs should be assigned — it provides the capability
to *manipulate network state* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction.

---

## Requirements

### Requirement 1: Query All Interfaces

**User Story:** As a maintenance coordinator, I want to list every network
interface so that reporting and idempotency workflows can enumerate interfaces
without parsing raw tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interfaces()` THE SYSTEM SHALL dispatch
   `networksetup -listallhardwareports` through the command runner without
   elevation to obtain the list of `(Hardware Port, Device, Ethernet Address)`
   tuples.
2. WHEN that succeeds THE SYSTEM SHALL loop the resulting BSD device names through
   `ifconfig <device>` (without elevation) and parse each output into a
   `MacosInterfaceInfo` with `name=<device>`, `mac_address` (from the `ether` line),
   `addresses` (from each `inet <addr> netmask <hex_mask>` line, converted to
   `<addr>/<prefix>` CIDR form), `gateway` (looked up via `route -n get default`
   matching on interface), `state` (`"up"` when the flags line contains `<UP>`,
   otherwise `"down"`), and `mtu` (from the flags line).
3. WHEN any underlying command fails THE SYSTEM SHALL raise a
   `MacosNetworkQueryError`.

---

### Requirement 2: Query a Single Interface

**User Story:** As a maintenance coordinator, I want to query a single
interface's detail, so that downstream logic can branch on existence and state
without iterating `get_interfaces()`.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interface(name)` THE SYSTEM SHALL dispatch
   `ifconfig <name>` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the output into a
   `MacosInterfaceInfo` using the same rules as `get_interfaces()`.
3. WHEN the command exits non-zero with stderr matching `interface <name> does
   not exist` THE SYSTEM SHALL return `None`.
4. WHEN the command exits non-zero for any other reason THE SYSTEM SHALL raise a
   `MacosNetworkQueryError`.

---

### Requirement 3: Configure an Interface

**User Story:** As a maintenance coordinator, I want to assign a static IP
address, netmask, and gateway to a network service, so that persistent networking
is in place and the runtime state matches.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure_interface(name, address, netmask,
   gateway)` THE SYSTEM SHALL first map `name` (kernel ifname or BSD device name
   such as `en0`) to its System Configuration service name (e.g. `Wi-Fi`,
   `Ethernet`) by parsing `networksetup -listallhardwareports`, then dispatch
   `networksetup -setmanual <servicename> <address> <netmask> <gateway>` through
   the command runner with `elevated=True` when configured.
2. WHEN `gateway` is `None` THE SYSTEM SHALL dispatch `networksetup -setmanual
   <servicename> <address> <netmask> ""` — `networksetup` requires an empty
   string when no gateway is desired.
3. WHEN `networksetup -getinfo <servicename>` already reports
   `<address>/<netmask>/<gateway>` equal to the requested values THE SYSTEM SHALL
   skip execution and return a `MacosNetworkActionResult` with `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 4: Enable an Interface

**User Story:** As a maintenance coordinator, I want to enable a network service
so that traffic can flow.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_interface(name)` THE SYSTEM SHALL resolve
   `name` to a service name and dispatch `networksetup
   -setnetworkserviceenabled <servicename> on` through the command runner with
   `elevated=True` when configured.
2. WHEN `networksetup -listallnetworkservices` reports the service already
   enabled (not prefixed with `*`) THE SYSTEM SHALL skip execution and return a
   `MacosNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 5: Disable an Interface

**User Story:** As a maintenance coordinator, I want to disable a network
service so that traffic does not flow.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_interface(name)` THE SYSTEM SHALL
   dispatch `networksetup -setnetworkserviceenabled <servicename> off` through
   the command runner with `elevated=True` when configured.
2. WHEN `networksetup -listallnetworkservices` already reports the service
   disabled (prefixed with `*`) THE SYSTEM SHALL skip execution and return a
   `MacosNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 6: Add a Route

**User Story:** As a maintenance coordinator, I want to add a static route so
that traffic destined for a specific prefix flows through a chosen gateway.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_route(dest, gateway, interface)` THE SYSTEM
   SHALL dispatch `route -n add <dest> <gateway>` (with `-ifscope <interface>`
   appended when `interface` is not `None`) through the command runner with
   `elevated=True` when configured.  Note that unlike Linux, macOS `route` adds
   to the *runtime* routing table only — persistent routes require a launchd
   script, which is deferred as an extension.
2. WHEN an equivalent route already exists in `netstat -rn` output THE SYSTEM
   SHALL skip execution and return a `MacosNetworkActionResult` with
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS` and a `message` noting that
   the route is runtime-only; the coordinator should author a launchd script for
   persistence.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Remove a Route

**User Story:** As a maintenance coordinator, I want to remove a static route so
that a previous indirection is revoked.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_route(dest)` THE SYSTEM SHALL dispatch
   `route -n delete <dest>` through the command runner with `elevated=True` when
   configured.
2. WHEN no route for `dest` exists THE SYSTEM SHALL skip execution and return a
   `MacosNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Query All Routes

**User Story:** As a maintenance coordinator, I want to list every route so
that reporting workflows can enumerate routes without parsing raw `route` output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_routes()` THE SYSTEM SHALL dispatch
   `netstat -rn` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line of the IPv4 and
   IPv6 tables into `MacosRouteInfo` records with `destination` (from
   `Destination`, normalising `default` → `0.0.0.0/0` or `::/0`), `gateway`
   (from `Gateway`, `None` if link-local), `interface` (from `Netif`), and
   `metric` (always `None` — macOS's `netstat -rn` does not emit metric).
3. WHEN the command fails THE SYSTEM SHALL raise a `MacosNetworkQueryError`.

---

### Requirement 9: Set DNS Servers

**User Story:** As a maintenance coordinator, I want to set the list of DNS
servers so that name resolution uses the operator-supplied nameservers.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_dns_servers(servers)` THE SYSTEM SHALL resolve
   the default-route interface's service name and dispatch `networksetup
   -setdnsservers <servicename> <server1> <server2> ...` through the command
   runner with `elevated=True` when configured.  When `servers` is empty, the
   manager SHALL dispatch `networksetup -setdnsservers <servicename> empty`
   (the literal word required to clear the list).
2. WHEN `get_dns_servers()` already reports the supplied list (order-preserved)
   THE SYSTEM SHALL skip execution and return a `MacosNetworkActionResult` with
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS`.  `networksetup` persists
   the change through `configd`, so no additional persistence step is required.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Query DNS Servers

**User Story:** As a maintenance coordinator, I want to retrieve the current
DNS server list, so that idempotency guards and reporting workflows can reason
about current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dns_servers()` THE SYSTEM SHALL resolve the
   default-route interface's service name and dispatch `networksetup
   -getdnsservers <servicename>` through the command runner without elevation.
2. WHEN the command stdout is the literal line `There aren't any DNS Servers
   set on <servicename>.` THE SYSTEM SHALL return `[]`.
3. WHEN the command stdout is one IP per line THE SYSTEM SHALL return those IPs
   as a list preserving the order returned by the tool.
4. WHEN the command fails THE SYSTEM SHALL raise a `MacosNetworkQueryError`.

---

### Requirement 11: Set Search Domains

**User Story:** As a maintenance coordinator, I want to set the DNS search
domains so that short-name lookups resolve under the operator-supplied domains.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_search_domains(domains)` THE SYSTEM SHALL
   dispatch `networksetup -setsearchdomains <servicename> <domain1> <domain2>
   ...` through the command runner with `elevated=True` when configured.  An
   empty `domains` list dispatches `... empty` to clear the list.
2. WHEN `networksetup -getsearchdomains <servicename>` already reports the
   supplied list THE SYSTEM SHALL skip execution and return a
   `MacosNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 12: Get Hostname

**User Story:** As a maintenance coordinator, I want to query the current
system hostname so that downstream logic can branch on it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_hostname()` THE SYSTEM SHALL dispatch
   `scutil --get ComputerName` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return the stripped stdout as
   the hostname string.
3. WHEN the command fails THE SYSTEM SHALL raise a `MacosNetworkQueryError`.

---

### Requirement 13: Set Hostname

**User Story:** As a maintenance coordinator, I want to set the system
hostname, covering the three macOS name surfaces, so that the machine reports
the operator-supplied name consistently.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_hostname(name)` THE SYSTEM SHALL dispatch
   three separate commands — `scutil --set ComputerName <name>`, `scutil --set
   LocalHostName <name>` (with any `.` replaced by `-` to meet the Bonjour
   constraint), and `scutil --set HostName <name>` — through the command runner
   with `elevated=True` when configured.
2. WHEN every `scutil` call reports that its current value already equals the
   requested value THE SYSTEM SHALL skip execution and return a
   `MacosNetworkActionResult` with `status=SKIPPED`.
3. WHEN all three commands succeed THE SYSTEM SHALL return a
   `MacosNetworkActionResult` with `status=SUCCESS`.
4. WHEN any one command fails THE SYSTEM SHALL stop dispatching further
   commands and return a `MacosNetworkActionResult` with `status=FAILED` and a
   `message` identifying the failing surface (ComputerName / LocalHostName /
   HostName).
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all network write
operations to run with root privilege via `sudo`, so that write operations
succeed on a standard macOS system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for every write (configure_interface,
   enable_interface, disable_interface, add_route, remove_route,
   set_dns_servers, set_search_domains, set_hostname).
2. WHEN performing read-only operations (get_interfaces, get_interface,
   get_routes, get_dns_servers, get_hostname) THE SYSTEM SHALL NOT pass
   `elevated=True`, regardless of configuration.
3. WHEN the process is already running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner to surface the resulting permission
   error as a `MacosNetworkActionResult` with `status=FAILED` for writes or as
   a `MacosNetworkQueryError` for reads.
