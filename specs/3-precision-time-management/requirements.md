# Requirements

## Introduction

The precision-time-management feature provides the
`codecraftai-sysman-macos` package with the ability to query and
configure the host's NTP synchronisation surface on macOS.  This spec
implements the precision-time-management contract defined at
`codecraftai-sysman-common/specs/contracts/precision-time-management/contract.md`
for macOS using `systemsetup` (the canonical persistent surface for
network-time configuration), `sntp` (the user-space SNTP client), and
`defaults read /Library/Preferences/com.apple.timed timeServiceConfig`
(the contemporary `timed` defaults domain on Big Sur and later).

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* time sources should be active or
*whether* clock sync should be on — it provides the capability to
*manipulate the precision-time surface* reliably, idempotently, and
with full auditability.  All system commands flow through the
`codecraftai-interfaces` command-runner abstraction.  No file is opened
directly: legacy `/etc/ntp.conf` (where present) is read via `cat`
through the runner.

This feature is parallel to — and does not duplicate — any other
feature.  Timezone management lives under `localization-management`;
this feature owns clock synchronisation only.

### macOS multi-server limitation

`systemsetup -setnetworktimeserver` accepts a single
whitespace-delimited string and persists it through the System
Configuration framework.  Apple's tooling is single-server-oriented;
multi-server lists are accepted by `systemsetup` but are not natively
distributed across the platform's clock-sync daemons.  This spec
documents the limitation: `add_time_source` and `set_time_servers`
write the full list via `systemsetup`, and operators should treat
the first entry as the effective primary upstream.

---

## Requirements

### Requirement 1: Query Overall Time Status

**User Story:** As a maintenance coordinator, I want to read the
host's overall sync status, so that reporting and idempotency
workflows can branch on whether network time is on, which upstream
is currently selected, and how recently a sync occurred.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_status()` THE SYSTEM SHALL
   dispatch `systemsetup -getusingnetworktime` and
   `systemsetup -getnetworktimeserver` through the command runner
   without elevation.
2. WHEN both commands succeed THE SYSTEM SHALL return a
   `MacosTimeStatus` carrying `enabled` (`Network Time: On`),
   `synchronized` (mirrors `enabled` because macOS does not expose a
   separate "current sync state" reading via these tools),
   `current_server` (parsed from
   ``Network Time Server: <server>``), and `None` for `stratum` /
   `last_sync` / `offset_seconds` (Apple's tools do not expose those
   values).
3. WHEN either `systemsetup` invocation fails THE SYSTEM SHALL raise
   a `MacosPrecisionTimeQueryError` carrying the failing command,
   exit code, and stderr.

---

### Requirement 2: Enumerate Configured Time Sources

**User Story:** As a maintenance coordinator, I want to enumerate
the host's configured upstream time sources so that idempotency
guards on `add_time_source` / `remove_time_source` /
`set_time_servers` can reason about current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_sources()` THE SYSTEM SHALL
   first dispatch `defaults read
   /Library/Preferences/com.apple.timed timeServiceConfig` (no
   elevation) and parse the plist payload's `TimeServers = ( ... );`
   block into one `MacosTimeSourceInfo` per quoted entry, with the
   first entry marked `"selected"` and the rest marked
   `"candidate"`.
2. WHEN the `defaults` command exits non-zero (the domain is absent
   on older builds) THE SYSTEM SHALL fall back to dispatching
   `systemsetup -getnetworktimeserver` and emit a single
   `MacosTimeSourceInfo` for the parsed primary.
3. WHEN both `defaults` and `systemsetup` return no servers THE
   SYSTEM SHALL further fall back to reading `/etc/ntp.conf` via
   `cat` and parsing `server`/`pool` declarations.
4. WHEN no sources are configured at all THE SYSTEM SHALL return
   the empty list rather than raise.

---

### Requirement 3: Add a Time Source

**User Story:** As a maintenance coordinator, I want to add an
upstream NTP source so that the host gains another peer.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_time_source(server)` THE SYSTEM
   SHALL probe the current server list, append `server` to the
   list, and dispatch
   `systemsetup -setnetworktimeserver "<rendered list>"` through
   the command runner with `elevated=True` when configured.
2. WHEN the supplied server is already in the current list THE
   SYSTEM SHALL skip execution and return
   `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `status=DRY_RUN` with the would-have-been
   ``systemsetup`` argv recorded on `command`.
4. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured exit code, stdout, stderr, and
   command.
5. THE SYSTEM SHALL document the macOS multi-server limitation in
   the spec narrative and in the source-code module docstring.

---

### Requirement 4: Remove a Time Source

**User Story:** As a maintenance coordinator, I want to remove an
upstream NTP source so that the host stops considering it.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_time_source(server)` THE
   SYSTEM SHALL recompute the current list with the requested
   server removed and dispatch
   `systemsetup -setnetworktimeserver "<remainder>"` through the
   command runner with `elevated=True` when configured.
2. WHEN the supplied server is not present in the current list THE
   SYSTEM SHALL skip execution and return
   `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `status=DRY_RUN`.

---

### Requirement 5: Replace the Time-Source List

**User Story:** As a maintenance coordinator, I want to replace the
host's full upstream list in a single transaction.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_time_servers(servers)` THE
   SYSTEM SHALL render the supplied list space-joined and dispatch
   `systemsetup -setnetworktimeserver "<rendered>"` through the
   command runner with `elevated=True` when configured.
2. WHEN the existing list (under sorted comparison) already equals
   the desired list THE SYSTEM SHALL skip execution and return
   `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `status=DRY_RUN`.

---

### Requirement 6: Force a Synchronisation

**User Story:** As a maintenance coordinator, I want to force the
host to attempt an immediate sync so that operator-driven workflows
can re-converge after a known clock drift event.

#### Acceptance Criteria

1. WHEN the coordinator calls `force_sync_now()` THE SYSTEM SHALL
   first read the current server via
   `systemsetup -getnetworktimeserver` (no elevation) and then
   dispatch `sntp -sS <current_server>` through the command runner
   with `elevated=True` when configured.
2. WHEN no server is configured THE SYSTEM SHALL return
   `status=FAILED` with a stderr explanation rather than dispatch a
   bogus `sntp` invocation.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `status=DRY_RUN`.
4. `force_sync_now` MUST NOT short-circuit on idempotency; the verb
   is operational.

---

### Requirement 7: Read Drift

**User Story:** As a maintenance coordinator, I want to read the
host's current drift so that reporting workflows can capture
offset / jitter without hand-parsing tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_drift()` THE SYSTEM SHALL read
   the configured server, dispatch `sntp <server>` through the
   command runner without elevation, and parse the
   `±offset +/- error<unit>` payload into a `MacosTimeDrift` with
   `offset_seconds` and `jitter_seconds` populated.
2. WHEN no server is configured THE SYSTEM SHALL return a
   `MacosTimeDrift(offset_seconds=0.0, frequency_ppm=None,
   jitter_seconds=None, stratum=None)` rather than raise.
3. WHEN `sntp` fails THE SYSTEM SHALL raise a
   `MacosPrecisionTimeQueryError`.
4. THE SYSTEM SHALL set `frequency_ppm` and `stratum` to `None` —
   Apple's user-space tooling does not expose those values.

---

### Requirement 8: Enable Sync

**User Story:** As a maintenance coordinator, I want to enable
network time so that the host begins keeping its clock
synchronised.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_sync()` THE SYSTEM SHALL
   dispatch `systemsetup -setusingnetworktime on` through the
   command runner with `elevated=True` when configured.
2. WHEN network time is already on THE SYSTEM SHALL skip execution
   and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.

---

### Requirement 9: Disable Sync

**User Story:** As a maintenance coordinator, I want to disable
network time so that the host stops adjusting its clock from
upstream.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_sync()` THE SYSTEM SHALL
   dispatch `systemsetup -setusingnetworktime off` through the
   command runner with `elevated=True` when configured.
2. WHEN network time is already off THE SYSTEM SHALL skip execution
   and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all
precision-time write operations to run with root privilege via
`sudo`, so that mutations against the System Configuration database
and the system clock succeed on a standard system where the process
does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write
   (`add_time_source`, `remove_time_source`, `set_time_servers`,
   `force_sync_now`, `enable_sync`, `disable_sync`).
2. WHEN performing read-only operations (`get_time_status`,
   `get_time_sources`, `get_drift`, the idempotency probes that
   read the active config) THE SYSTEM SHALL NOT pass
   `elevated=True`, regardless of configuration.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner to surface the
   resulting permission error as a
   `MacosPrecisionTimeActionResult` with `status=FAILED` for
   writes or as a `MacosPrecisionTimeQueryError` for reads.

---

## Out of Scope

- PTP / IEEE 1588 — not natively supported by stock macOS.
- NTP authentication keys.
- Per-source PPS hardware configuration.
- Timezone selection (lives under `localization-management`).
- Hardware clock (`hwclock`) management — macOS does not expose a
  separate hardware-clock surface to user space the way Linux does.
- The legacy `xntpd` / `ntpd` posture on retired macOS releases.
