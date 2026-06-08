# Requirements

## Introduction

The mail-transport-management feature provides the
`codecraftai-sysman-macos` package with the ability to manage the local
mail transport agent (MTA) on macOS hosts.  macOS ships **Postfix**
as part of the base operating system; the configuration files live
at the same paths as Linux (`/etc/postfix/main.cf`, `/etc/aliases`)
and the command suite (`postconf`, `postqueue`, `mailq`,
`newaliases`, `sendmail`) is available out of the box.  This spec
implements the mail-transport-management contract defined at
`codecraftai-sysman-common/specs/contracts/mail-transport-management/contract.md`
for macOS, scoped to the system-shipped Postfix daemon exclusively.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All
system commands (`postconf`, `postqueue`, `mailq`, `newaliases`,
`sendmail`, `cat`, `tee`, `launchctl`) are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are
made inside this module.

The Postfix daemon on macOS is managed by **launchd**, not systemd.
The LaunchDaemon plist ships at
`/System/Library/LaunchDaemons/org.postfix.master.plist` but is **not
loaded by default** on consumer macOS — Postfix is dormant unless an
operator explicitly bootstraps it.  Enabling the daemon is the
responsibility of the service-management feature; this module manages
the *configuration* assuming the daemon-management story exists
elsewhere.  The reload after a successful `set_mta_config` is the
sole exception: it is the canonical "apply directive change" signal
on macOS, idiomatic to this feature rather than to the lifecycle one.

The macOS `launchctl kickstart -k system/org.postfix.master`
incantation is closer to a full restart than a true reload — the
`-k` flag stops the running instance before re-launching it under
the same plist.  When the daemon is not loaded, the kickstart will
fail with a `Could not find service` error; the manager surfaces that
failure on the `set_mta_config` result so callers can react (e.g., by
delegating daemon-load to `service-management` first).

This feature complements — and does not duplicate — the
`postfix.master` lifecycle owned by `3-service-management`, the
SMTP-port firewall surface owned by `3-firewall-management`, and the
TLS-material surface owned by `3-certificate-trust-management` /
`3-certificate-issuance`.

---

## Requirements

### Requirement 1: Read MTA Configuration

**User Story:** As a maintenance coordinator, I want to read the live
Postfix configuration on macOS, so that I can audit declared
directives without parsing the raw `main.cf` file.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mta_config()` THE SYSTEM SHALL
   invoke `postconf -n` via the runner with `elevated=False`.
2. WHEN `postconf -n` exits 0 THE SYSTEM SHALL parse each `key =
   value` line into a `MacosMtaConfig`, mapping every recognised
   directive (`myhostname`, `mydomain`, `myorigin`, `relayhost`,
   `mynetworks`, `inet_interfaces`, `inet_protocols`,
   `mailbox_size_limit`, `message_size_limit`) to its named field.
3. WHEN a directive is integer-typed THE SYSTEM SHALL coerce the
   value with `int()`; non-numeric values fall back to `extras`.
4. WHEN a directive is not in the recognised set THE SYSTEM SHALL
   place it in the `extras` dict.
5. WHEN `postconf -n` exits non-zero AND `stderr` is non-empty THE
   SYSTEM SHALL raise `MacosMtaQueryError`.

---

### Requirement 2: Apply MTA Configuration

**User Story:** As a maintenance coordinator, I want to apply a
declared directive snapshot to live Postfix on macOS, so that the
server's posture matches the spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_mta_config(spec)` THE SYSTEM
   SHALL first call `get_mta_config()` to compare against the live
   directive set.
2. WHEN a named field on `spec` differs from the live value THE
   SYSTEM SHALL dispatch `postconf -e <key>=<value>`.
3. WHEN a named field matches THE SYSTEM SHALL skip the dispatch
   and record `SKIPPED <key>=<value>` in the audit trail.
4. WHEN every named field matches THE SYSTEM SHALL return
   `status=SKIPPED` and SHALL NOT invoke the kickstart.
5. WHEN at least one directive changes successfully AND
   `config.reload_after_set=True` THE SYSTEM SHALL invoke
   `launchctl kickstart -k <domain>/<label>` (default
   `system/org.postfix.master`) once at the end.
6. WHEN any per-directive `postconf -e` exits non-zero THE SYSTEM
   SHALL return `status=FAILED` with that directive's exit code,
   stdout, and stderr; remaining directives SHALL NOT be dispatched.
7. WHEN the trailing `launchctl kickstart` exits non-zero THE
   SYSTEM SHALL return `status=FAILED`.  This is expected on
   consumer macOS where the LaunchDaemon is not loaded.
8. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
9. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for
   every per-directive `postconf -e` and the trailing kickstart.

---

### Requirement 3: Add an Alias

**User Story:** As a maintenance coordinator, I want to add an entry
to `/etc/aliases`, so that local mail addressed to `<local>` is
delivered to `<remote>`.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_alias(local, remote)` THE SYSTEM
   SHALL read `/etc/aliases` via `cat`, parse, mutate, re-render,
   and write the result via `tee /etc/aliases` with the rendered
   content on stdin.
2. WHEN the file already contains an entry for `local` whose remote
   equals `remote` THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the file contains an entry for `local` with a different
   remote THE SYSTEM SHALL replace the line.
4. WHEN the write succeeds THE SYSTEM SHALL invoke `newaliases`.
5. WHEN `newaliases` fails THE SYSTEM SHALL map the verb to
   `status=FAILED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Remove an Alias

**User Story:** As a maintenance coordinator, I want to remove an
entry from `/etc/aliases`, so that the alias is no longer delivered.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_alias(local)` and the file
   does not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the file does not contain an entry for `local` THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN an entry for `local` exists THE SYSTEM SHALL drop the line
   and write via `tee`.
4. WHEN the write succeeds THE SYSTEM SHALL invoke `newaliases`.
5. WHEN `newaliases` fails THE SYSTEM SHALL map the verb to
   `status=FAILED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 5: List Aliases

**User Story:** As a maintenance coordinator, I want to enumerate
every entry in `/etc/aliases`, so that I can audit local-mail policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_aliases()` and the file does
   not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse each non-comment,
   non-blank line into a `MacosMtaAliasInfo`.
3. WHEN a line begins with whitespace AND a previous entry exists
   THE SYSTEM SHALL fold it as a continuation onto the previous
   entry's `remote`.
4. WHEN `list_aliases` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 6: Probe Mail Delivery

**User Story:** As a maintenance coordinator, I want to send a probe
message through the local MTA, so that I can verify end-to-end
delivery.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_mail_delivery(to, from_addr,
   subject)` THE SYSTEM SHALL invoke `sendmail -t` via the runner.
2. WHEN invoking `sendmail -t` THE SYSTEM SHALL pass an RFC 5322
   payload on stdin containing `Subject:`, `From:`, and `To:`
   headers, a blank line, and a fixed body.
3. WHEN the runner exits 0 THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner; `stdout` SHALL contain the payload
   that would have been delivered.
6. `test_mail_delivery` MUST NOT skip — it is operational.

---

### Requirement 7: Read Queue Status

**User Story:** As a maintenance coordinator, I want to read the
current Postfix mail-queue status on macOS, so that I can detect
stuck or backed-up messages.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_queue_status()` THE SYSTEM SHALL
   invoke `mailq` via the runner with `elevated=False`.
2. WHEN the output contains `Mail queue is empty` THE SYSTEM SHALL
   return a zero-valued `MacosMtaQueueStatus`.
3. WHEN the output contains queue records THE SYSTEM SHALL parse
   each head line; queue ids ending in `*` count as `pending`,
   queue ids without a marker count as `deferred`.
4. WHEN the output contains a `-- N Kbytes in M Requests.` footer
   THE SYSTEM SHALL set `total_size_bytes` to `N * 1024`.
5. WHEN `mailq` exits non-zero AND `stderr` is non-empty THE SYSTEM
   SHALL raise `MacosMtaQueryError`.

---

### Requirement 8: Flush the Queue

**User Story:** As a maintenance coordinator, I want to attempt
immediate delivery of every queued message, so that delivery is
retried on demand.

#### Acceptance Criteria

1. WHEN the coordinator calls `flush_queue()` THE SYSTEM SHALL
   invoke `postqueue -f` via the runner.
2. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
3. WHEN the runner exits 0 THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED`.
5. `flush_queue` MUST NOT skip — it is operational.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
that mutates the MTA's state to elevate, so that operations succeed
under macOS's `sudo`-driven elevation model.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for
   every write verb (`set_mta_config`, `add_alias`, `remove_alias`,
   `test_mail_delivery`, `flush_queue`) and any chained
   `newaliases` / `launchctl kickstart` dispatches.
2. WHEN performing reads (`get_mta_config`, `list_aliases`,
   `get_queue_status`) THE SYSTEM SHALL pass `elevated=False`
   regardless of the config.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.

---

### Requirement 10: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview every
mutation without dispatching, so that diff-vs-state reports can be
generated before changes go live.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL NOT dispatch any mutating
   command and SHALL NOT rewrite `/etc/aliases`.
2. WHEN `dry_run=True` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for
   every mutating verb with `command` populated.

---

### Requirement 11: Failure Reporting

**User Story:** As a maintenance coordinator, I want every failed
write to surface a structured result, so that downstream automation
can categorise failures without scraping logs.

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated.
2. WHEN a chained command fails (e.g., `newaliases` after `tee`,
   or `launchctl kickstart` after a successful `postconf -e`) THE
   SYSTEM SHALL surface the chained command's `command`,
   `exit_code`, and `stderr` on the returned result.
3. WHEN a read verb's underlying runner call exits non-zero with
   non-empty `stderr` THE SYSTEM SHALL raise `MacosMtaQueryError`.

---

### Requirement 12: Postfix Disabled-By-Default Documentation

**User Story:** As a maintenance coordinator, I want the macOS
Postfix-disabled-by-default behaviour documented, so that I know to
either bootstrap the daemon first or set `reload_after_set=False`.

#### Acceptance Criteria

1. The `MacosMtaManagerConfig` documentation SHALL note that
   `reload_after_set=True` (the default) requires the
   `org.postfix.master` LaunchDaemon to be loaded.
2. The module docstring SHALL document the
   `launchctl kickstart -k system/org.postfix.master` incantation
   and the `Could not find service` failure mode on consumer
   macOS.
3. Tests SHALL include a `test_set_mta_config_failed_when_kickstart_fails`
   case proving the failure surface for that scenario.
