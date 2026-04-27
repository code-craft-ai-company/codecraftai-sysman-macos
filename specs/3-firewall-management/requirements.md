# Requirements

## Introduction

The firewall management feature provides the `automojo-sysman-macos` package with the
ability to manage `pf` (Packet Filter) on macOS systems via the `pfctl` CLI.  This spec
implements the firewall-management contract defined at
`automojo-sysman-common/specs/contracts/firewall-management/contract.md` for macOS
using `pf` via `pfctl` together with an `IFileOperator` for managing pf anchor rule
files.  It includes adding and removing services, ports, and rich rules within pf
anchors, querying anchor state, and reloading the pf configuration to apply permanent
changes to the runtime configuration.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* firewall
rules to apply — it provides the capability to *do it* reliably, idempotently, and with
full auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, and all anchor file manipulation is
performed through the `automojo-interfaces` file-operator abstraction.  On macOS the
abstract `zone` parameter maps to a pf anchor name; by convention the implementation
maintains files at `/etc/pf.anchors/automojo.<zone>`.

---

## Requirements

### Requirement 1: Add a Service to a Zone

**User Story:** As a maintenance coordinator, I want to add a named service rule to a pf
anchor, so that traffic for that service is permitted through the firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a zone name THE SYSTEM SHALL resolve
   the service name to a `(port, protocol)` pair via a bundled services table, append a
   `pass in quick proto <protocol> from any to any port <port>` line to the anchor file
   `/etc/pf.anchors/automojo.<zone>` via the file operator, and invoke
   `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>` via the command runner.
2. WHEN the service name cannot be resolved via the bundled services table THE SYSTEM
   SHALL return a `MacosFirewallActionResult` with status `FAILED` and a `detail`
   explaining the unknown service.
3. WHEN the resolved rule is already present in the anchor file THE SYSTEM SHALL skip
   execution and return a `MacosFirewallActionResult` with status `SKIPPED`.
4. WHEN the reload succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
5. WHEN either the file write or the reload fails THE SYSTEM SHALL return a
   `MacosFirewallActionResult` with status `FAILED`, the exit code, and the captured
   stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the anchor file and SHALL NOT
   execute any command; it SHALL return a `MacosFirewallActionResult` with status
   `DRY_RUN`.

---

### Requirement 2: Remove a Service from a Zone

**User Story:** As a maintenance coordinator, I want to remove a named service rule
from a pf anchor, so that traffic for that service is no longer permitted through the
firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and a zone name THE SYSTEM SHALL resolve
   the service to `(port, protocol)`, remove the matching `pass in quick proto
   <protocol> ... port <port>` line from `/etc/pf.anchors/automojo.<zone>`, and invoke
   `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>`.
2. WHEN the service name cannot be resolved THE SYSTEM SHALL return a
   `MacosFirewallActionResult` with status `FAILED`.
3. WHEN the matching rule is not present in the anchor file THE SYSTEM SHALL skip
   execution and return a `MacosFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate the anchor file nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 3: Add a Port to a Zone

**User Story:** As a maintenance coordinator, I want to open a specific port and
protocol in a pf anchor, so that traffic on that port is permitted through the firewall
without requiring a named service.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and zone name THE SYSTEM
   SHALL append `pass in quick proto <protocol> from any to any port <port>` to the
   anchor file `/etc/pf.anchors/automojo.<zone>` and invoke
   `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>`.
2. WHEN the corresponding rule is already present in the anchor file THE SYSTEM SHALL
   skip execution and return a `MacosFirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate the anchor file nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 4: Remove a Port from a Zone

**User Story:** As a maintenance coordinator, I want to close a specific port and
protocol in a pf anchor, so that traffic on that port is no longer permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and zone name THE SYSTEM
   SHALL remove the matching `pass in quick proto <protocol> ... port <port>` line from
   `/etc/pf.anchors/automojo.<zone>` and invoke
   `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>`.
2. WHEN the matching rule is not present in the anchor file THE SYSTEM SHALL skip
   execution and return a `MacosFirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate the anchor file nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 5: Add a Rich Rule to a Zone

**User Story:** As a maintenance coordinator, I want to add a raw pf-syntax rule to an
anchor, so that complex traffic filtering logic that cannot be expressed as a simple
service or port can be enforced.

#### Acceptance Criteria

1. WHEN the coordinator provides a raw pf rule string and a zone name THE SYSTEM SHALL
   append the rule verbatim as a new line to `/etc/pf.anchors/automojo.<zone>` and
   invoke `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>`.
2. WHEN an identical rule (after whitespace normalisation) is already present in the
   anchor file THE SYSTEM SHALL skip execution and return a `MacosFirewallActionResult`
   with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate the anchor file nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 6: Remove a Rich Rule from a Zone

**User Story:** As a maintenance coordinator, I want to remove a raw pf-syntax rule
from an anchor, so that a previously applied complex traffic rule is revoked.

#### Acceptance Criteria

1. WHEN the coordinator provides a raw pf rule string and a zone name THE SYSTEM SHALL
   remove the matching line from `/etc/pf.anchors/automojo.<zone>` (whitespace
   normalised) and invoke
   `pfctl -a automojo.<zone> -f /etc/pf.anchors/automojo.<zone>`.
2. WHEN the rule is not present in the anchor file THE SYSTEM SHALL skip execution and
   return a `MacosFirewallActionResult` with status `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate the anchor file nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 7: Set the Default Zone

**User Story:** As a maintenance coordinator, I want to designate a pf anchor as the
default (first-loaded) automojo anchor, so that its rules take precedence over other
automojo anchors.

#### Acceptance Criteria

1. WHEN the coordinator provides a zone name THE SYSTEM SHALL read `/etc/pf.conf` via
   the file operator and ensure `anchor "automojo.<zone>"` and
   `load anchor "automojo.<zone>" from "/etc/pf.anchors/automojo.<zone>"` appear first
   among the `automojo.*` anchors, rewriting `/etc/pf.conf` if needed, then invoke
   `pfctl -f /etc/pf.conf`.
2. WHEN the specified anchor is already the first `automojo.*` anchor in `/etc/pf.conf`
   THE SYSTEM SHALL skip execution and return a `MacosFirewallActionResult` with status
   `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT mutate `/etc/pf.conf` nor execute
   any command and SHALL return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 8: Reload the Firewall

**User Story:** As a maintenance coordinator, I want to reload pf so that
`/etc/pf.conf` and all referenced anchors are re-read and applied to the packet filter.

#### Acceptance Criteria

1. WHEN the coordinator requests a firewall reload THE SYSTEM SHALL invoke
   `pfctl -f /etc/pf.conf` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `MacosFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `MacosFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Zone State

**User Story:** As a maintenance coordinator, I want to query the current state of a pf
anchor including its services, ports, and rich rules, so that I can make conditional
decisions about what changes are needed without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator provides a zone name THE SYSTEM SHALL invoke
   `pfctl -a automojo.<zone> -sr` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each `pass` / `block` / `match`
   rule line into `MacosZoneInfo.ports`, `MacosZoneInfo.services` (reverse-looked-up
   from the bundled services table), and `MacosZoneInfo.rich_rules`, populating
   `interfaces` and `sources` only when a rule narrows them.
3. WHEN the anchor does not exist THE SYSTEM SHALL raise a
   `MacosFirewallQueryError` carrying the exit code and stderr.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise a
   `MacosFirewallQueryError` with the exit code and captured stderr.

---

### Requirement 10: Query All Zones

**User Story:** As a maintenance coordinator, I want to retrieve a summary of all
automojo-managed pf anchors, so that I can audit the overall firewall configuration.

#### Acceptance Criteria

1. WHEN the coordinator requests all zones THE SYSTEM SHALL invoke `pfctl -sA` via the
   command runner, filter the output to anchors whose name starts with `automojo.`,
   and invoke `query_zone` for each.
2. WHEN every per-anchor query succeeds THE SYSTEM SHALL return a list of
   `MacosZoneInfo` records, one per automojo anchor, each populated with services,
   ports, and rich_rules.
3. WHEN any per-anchor query fails THE SYSTEM SHALL raise a `MacosFirewallQueryError`
   with the exit code and captured stderr.
4. WHEN `pfctl -sA` fails THE SYSTEM SHALL raise a `MacosFirewallQueryError` with the
   exit code and captured stderr.

---

### Requirement 11: Query PF State

**User Story:** As a maintenance coordinator, I want to query whether pf is enabled
and what the current default (first automojo) anchor is, so that I can verify the
firewall is active before applying rule changes.

#### Acceptance Criteria

1. WHEN the coordinator requests the firewall state THE SYSTEM SHALL invoke `pfctl -si`
   and `pfctl -sA` via the command runner without elevation.
2. WHEN `pfctl -si` output contains `Status: Enabled` THE SYSTEM SHALL set `running=True`;
   otherwise `running=False`.
3. WHEN `running=True` THE SYSTEM SHALL set `default_zone` to the first anchor name from
   `pfctl -sA` that starts with `automojo.`, stripped of that prefix; or `""` if none
   exist.
4. WHEN `pfctl -si` exits non-zero THE SYSTEM SHALL return a `MacosFirewallState` with
   `running=False` and `default_zone=""` per the contract's firewall-not-running rule.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all firewall write commands and
anchor-file writes to run with the privileges required by `pfctl`, so that rule changes
succeed on a standard macOS system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner and the file operator for all write operations
   (add/remove service, port, rich rule, set default zone, reload).
2. WHEN performing read-only operations (query zone, list all zones, get state) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner and file operator to surface the resulting
   permission error as a `MacosFirewallActionResult` with `status=FAILED` for writes
   or as a `MacosFirewallQueryError` for reads.
