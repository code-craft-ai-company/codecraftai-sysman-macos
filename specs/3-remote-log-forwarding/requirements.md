# Requirements

## Introduction

The remote-log-forwarding feature provides the
`automojo-sysman-macos` package with the ability to ship syslog
traffic off-host through the legacy `/etc/syslog.conf` mechanism
that `syslogd(8)` still honours on modern macOS releases.  It is
the cross-OS counterpart to the local log-shaping surface owned
by `3-log-management` and the RHEL-canonical
`3-remote-log-forwarding` feature.

**Important platform limitation.**  macOS unified logging — the
``log show`` / ``log stream`` surface — does NOT have a native
remote-syslog forwarder.  The only mechanism the operating system
exposes is `/etc/syslog.conf` plus `syslogd`, and that only
forwards messages routed through the legacy syslog facility (a
strict subset of unified-logging messages).  Operators who need
full unified-logging coverage must run a third-party shipper
(Filebeat, NXLog, Fluent Bit) or a custom `os_log` consumer; the
design document discusses this gap explicitly.

All system commands (`launchctl kickstart -k system/com.apple.syslogd`,
`logger`, `tee`, `cat`, `test -e`, `mv`, `rm`) are dispatched
through the `automojo-interfaces` `ICommandRunner` abstraction.
All mutating operations honour dry-run mode and config-driven
elevation.

The reference implementation for shape, idempotency, dry-run, and
result mapping lives in
`automojo-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

---

## Requirements

### Requirement 1: Add a Forwarder

**User Story:** As a maintenance coordinator, I want to declare a
syslog forwarder so that legacy syslog facility messages ship to
a remote collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_forwarder(spec)` THE SYSTEM
   SHALL upsert a sentinel-bracketed block in
   `/etc/syslog.conf` containing the canonical
   `<selector> @<host>:<port>` action line.
2. WHEN `protocol` is `tcp` or `tls` THE SYSTEM SHALL still emit
   the single-`@` action line because Apple's `syslogd` does
   not honour the rsyslog double-`@` convention; the result's
   `message` field SHALL note that legacy macOS syslog only
   speaks UDP and that operators requesting TCP / TLS should
   route through a third-party shipper.
3. WHEN the existing block matches the rendered desired block
   byte-for-byte THE SYSTEM SHALL return `SKIPPED`.
4. WHEN the dispatch succeeds THE SYSTEM SHALL kickstart
   `system/com.apple.syslogd` via `launchctl kickstart -k`;
   restart failures MUST surface as `FAILED`.

---

### Requirement 2: Remove a Forwarder

**User Story:** As a maintenance coordinator, I want to remove a
syslog forwarder block from `/etc/syslog.conf`.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_forwarder(id)` THE SYSTEM
   SHALL strip the matching sentinel-bracketed block from
   `/etc/syslog.conf` and write the resulting body via `tee`.
2. WHEN no matching block is found THE SYSTEM SHALL return
   `SKIPPED` without modifying the file.
3. WHEN the dispatch succeeds THE SYSTEM SHALL kickstart
   `system/com.apple.syslogd`; restart failures MUST surface as
   `FAILED`.

---

### Requirement 3: List Forwarders

**User Story:** As a maintenance coordinator, I want to enumerate
the forwarder blocks this manager owns.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_forwarders()` THE SYSTEM
   SHALL `cat /etc/syslog.conf` without elevation and parse the
   sentinel-bracketed blocks via pure Python.
2. THE SYSTEM SHALL include only blocks whose sentinel header
   matches the configured prefix so that legacy operator-edited
   lines outside a managed block are NOT falsely claimed.
3. THE SYSTEM SHALL classify a block as ``enabled=False`` when
   every action line within the block is commented out (leading
   ``#``).
4. WHEN ``cat`` fails for any reason other than the file simply
   being missing THE SYSTEM SHALL raise
   `MacosForwarderQueryError`.

---

### Requirement 4: Enable / Disable a Forwarder

**User Story:** As a maintenance coordinator, I want to suspend
and resume a forwarder block without rewriting it.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_forwarder(id)` THE SYSTEM
   SHALL prefix every action line within the block with ``# ``
   (a sentinel comment).
2. WHEN the coordinator calls `enable_forwarder(id)` THE SYSTEM
   SHALL strip the leading ``# `` from every action line in the
   block.
3. WHEN the requested state already matches the on-disk state
   THE SYSTEM SHALL return `SKIPPED` without writing the file.
4. WHEN the dispatch succeeds THE SYSTEM SHALL kickstart
   `system/com.apple.syslogd`; failures MUST surface as
   `FAILED`.

---

### Requirement 5: Test a Forwarder

**User Story:** As a maintenance coordinator, I want to send a
known message into the local syslog pipeline so that the
configured forwarder can be observed end-to-end on the
collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_forwarder(id)` THE SYSTEM
   SHALL dispatch
   `logger -t automojo-test 'forwarder-test-<timestamp>'`
   through the runner.
2. THE SYSTEM SHALL return a `MacosForwarderTestResult` with
   `forwarder_id` populated and `test_message_id` set to the
   timestamp portion of the dispatched message.
3. THE SYSTEM SHALL NOT raise on dispatch failure; the result's
   `status` reports `FAILED` and `stderr` carries the runner's
   stderr.
4. THE SYSTEM SHALL run the verb unconditionally — there is no
   idempotency guard for `test_forwarder`.

---

### Requirement 6: Get Forwarder Status

**User Story:** As a maintenance coordinator, I want to query a
forwarder's current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_forwarder_status(id)` THE
   SYSTEM SHALL `cat /etc/syslog.conf`, locate the matching
   block, and return a `MacosForwarderStatus` describing the
   current `enabled` flag.
2. WHEN no block is found THE SYSTEM SHALL return a status
   record whose `enabled` is `False` and whose
   `last_event_time`, `error_count`, and `bytes_sent` are
   `None`.
3. THE SYSTEM SHALL NOT elevate the status probe.

---

### Requirement 7: Documented Limitations

**User Story:** As a maintenance coordinator, I want the
documentation to enumerate the gaps in macOS native log
forwarding so that I do not file false bugs.

#### Acceptance Criteria

1. THE `design.md` SHALL prominently document that macOS
   unified logging (the ``log show`` / ``log stream`` surface)
   does NOT have a native remote-syslog forwarder.
2. THE `design.md` SHALL list TLS, TCP, queue tuning, and the
   `os_log` predicate-filter surface as out of scope.
3. THE `design.md` SHALL recommend Filebeat, NXLog, or Fluent
   Bit for full coverage.
4. THE module docstring SHALL repeat the unified-logging
   limitation so that callers see it in `help()` /
   IDE-tooltip output.

---

### Requirement 8: Dry-Run, Elevation, and Error Reporting

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run and to elevate predictably.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT mutate the
   syslog config file and SHALL NOT dispatch
   `launchctl kickstart`.
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner on every write dispatch.
3. WHEN the runner returns non-zero on a write THE SYSTEM SHALL
   return `MacosForwarderActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)` without raising.
4. THE SYSTEM SHALL never elevate read verbs (`list_forwarders`,
   `get_forwarder_status`, `test_forwarder`).

---

### Requirement 9: Idempotency

**User Story:** As a maintenance coordinator, I want every write
verb to be safe to invoke repeatedly.

#### Acceptance Criteria

1. `add_forwarder` MUST `SKIP` when the on-disk block matches
   the rendered desired block byte-for-byte.
2. `remove_forwarder` MUST `SKIP` when no matching block is
   present.
3. `enable_forwarder` MUST `SKIP` when the block's action lines
   are already uncommented.
4. `disable_forwarder` MUST `SKIP` when the block's action
   lines are already commented out.
5. `test_forwarder` MUST run unconditionally — there is no
   idempotency guard.

---

### Requirement 10: Service Reload

**User Story:** As a maintenance coordinator, I want the manager
to reload syslogd after every successful mutation so that the
new configuration takes effect immediately.

#### Acceptance Criteria

1. WHEN any write verb succeeds THE SYSTEM SHALL dispatch
   `launchctl kickstart -k system/com.apple.syslogd`
   through the same runner that handled the mutation, with
   `elevated=config.elevated`.
2. WHEN the kickstart returns non-zero THE SYSTEM SHALL surface
   the failure on the mutating verb's result with `status=FAILED`,
   the kickstart's `exit_code`, and the kickstart's `stderr`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   the kickstart.
