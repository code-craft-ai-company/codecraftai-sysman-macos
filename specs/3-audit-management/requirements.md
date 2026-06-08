# Requirements

## Introduction

The audit management feature provides the
`codecraftai-sysman-macos` package with the ability to enable,
disable, query, and rule-manage Apple's BSM-derived audit
subsystem on macOS hosts.  This spec implements the
audit-management contract defined at
`codecraftai-sysman-common/specs/contracts/audit-management/contract.md`
for macOS using `audit` (control), `praudit` (decode), and
`launchctl` (daemon lifecycle).  Rule storage is the
`/etc/security/audit_control` file and (for per-user overrides)
`/etc/security/audit_user`.

A maintenance coordinator running in an external package drives
all operations by calling into this feature's public verbs.  The
feature does not decide *which* audit classes to enable — it
provides the capability to install, remove, and observe them
reliably, idempotently, and with full auditability.  Every system
command is executed exclusively through the
`codecraftai-interfaces` command-runner abstraction; rule-file I/O is
dispatched via `cat` / `tee` against that same runner.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are
made inside the module.

The abstract `rule_id` on macOS maps to a BSM audit-class
abbreviation (`"lo"` = login/logout, `"fa"` = file attribute,
`"pc"` = process, …).  The structured `MacosAuditRuleSpec`
captures the class via the `event_class` field; `name` is a
logical identifier that mirrors `event_class` for the pilot but
may diverge in the future.  This is a **class-based** addressing
scheme — markedly different from Linux's line-based rules — and
the design document explains the cross-OS concept mapping in
detail.

---

## Requirements

### Requirement 1: Add a Rule

**User Story:** As a maintenance coordinator, I want to enable an
audit class on macOS, so that the audit subsystem captures events
from that class.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_rule(spec)` and
   `spec.event_class` is not present in the ``flags`` line of
   `/etc/security/audit_control` THE SYSTEM SHALL rewrite the
   file with the class appended via `tee` and dispatch ``audit -s``
   to reload the kernel policy.
2. WHEN the class is already in the ``flags`` line THE SYSTEM
   SHALL return `MacosAuditActionResult(status=SKIPPED)` without
   invoking `tee` or `audit -s`.
3. WHEN the file does not exist THE SYSTEM SHALL create it with a
   single `flags:<event_class>` line.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. WHEN the writer fails THE SYSTEM SHALL return `status=FAILED`
   with `exit_code` / `stdout` / `stderr` / `command` populated.
6. WHEN `spec.enabled` is False THE SYSTEM SHALL return
   `status=SKIPPED` with a descriptive message; the spec is
   recorded but not applied.
7. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   both the `tee` write and the `audit -s` reload.

---

### Requirement 2: Remove a Rule

**User Story:** As a maintenance coordinator, I want to remove an
audit-class rule, so that the audit subsystem stops capturing
events of that class.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_rule(rule_id)` and
   `rule_id` is present in the ``flags`` line THE SYSTEM SHALL
   rewrite the file with the class dropped, dispatch the rewrite
   via `tee`, and reload the kernel policy via `audit -s`.
2. WHEN `rule_id` is not present in the ``flags`` line THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `audit_control` does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the writer fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 3: List Rules

**User Story:** As a maintenance coordinator, I want to enumerate
the audit rules currently configured, so that I can compare
against my desired baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_rules()` THE SYSTEM SHALL
   read `/etc/security/audit_control` via `cat` and emit one
   `MacosAuditRuleInfo` per audit-class token in the ``flags``
   line, with `subsystem="class"` and `source` set to the
   audit_control path.
2. WHEN `audit_user` exists THE SYSTEM SHALL also read it and
   emit one row per non-comment, non-blank line with
   `subsystem="user"` so that per-user rules are surfaced.
3. WHEN either file is absent THE SYSTEM SHALL omit its rows
   from the result rather than raise.
4. WHEN `list_rules` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 4: Enable Auditing

**User Story:** As a maintenance coordinator, I want to enable
the macOS audit subsystem, so that the daemon begins recording
events.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_auditing()` THE SYSTEM
   SHALL dispatch `audit -i` (initialise) and
   `launchctl bootstrap system <auditd_plist>` so the daemon is
   loaded under launchd.
2. WHEN `audit -s` reports the subsystem as enabled and running
   THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `audit -s` reports `locked:yes` THE SYSTEM SHALL return
   `status=FAILED` with a descriptive `message` and SHALL NOT
   dispatch the enable command.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN either step fails THE SYSTEM SHALL return
   `status=FAILED`.

---

### Requirement 5: Disable Auditing

**User Story:** As a maintenance coordinator, I want to disable
the macOS audit subsystem, so that the daemon stops recording
events.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_auditing()` THE SYSTEM
   SHALL dispatch `launchctl bootout <auditd_label>` to unload
   the daemon.
2. WHEN `audit -s` reports the daemon as not running THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN `audit -s` reports `locked:yes` THE SYSTEM SHALL return
   `status=FAILED` with a descriptive `message`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the bootout step fails THE SYSTEM SHALL return
   `status=FAILED`.

---

### Requirement 6: Query Audit Status

**User Story:** As a maintenance coordinator, I want to inspect
the audit subsystem's status, so that my reports describe the
daemon's current configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_audit_status()` THE SYSTEM
   SHALL dispatch `audit -s` without elevation.
2. WHEN the runner exits zero THE SYSTEM SHALL parse
   `<key>:<value>` and `<key> = <value>` lines into the
   `MacosAuditStatus` fields: `enabled`, `running`, `pid`,
   `failure_mode`, `lost_events`, `locked`, and the raw text.
3. WHEN the runner exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise `MacosAuditQueryError`.
4. WHEN any individual line cannot be parsed THE SYSTEM SHALL
   set the corresponding field to `None`.

---

### Requirement 7: Search Events

**User Story:** As a maintenance coordinator, I want to filter
audit events by a substring or class, so that post-hoc analyses
can target specific incidents.

#### Acceptance Criteria

1. WHEN the coordinator calls `search_events(query)` and
   `query.raw_filter` is empty THE SYSTEM SHALL dispatch
   `praudit -l <audit_log_dir>/current` and apply the query as a
   substring filter against each output line.
2. WHEN `query.raw_filter` is non-empty THE SYSTEM SHALL pass
   its whitespace-split tokens verbatim after `praudit`.
3. WHEN the runner exits zero THE SYSTEM SHALL parse one
   `MacosAuditEvent` per matching line, capping the result at
   `query.limit` when set.
4. WHEN the runner exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise `MacosAuditQueryError`.
5. WHEN `search_events` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 8: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every write
verb to be safely re-runnable.

#### Acceptance Criteria

1. WHEN `add_rule` is called with an `event_class` already in
   the flags line THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN `remove_rule` is called with a `rule_id` not in the
   flags line THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `enable_auditing` is called and the subsystem is
   already enabled and running THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `disable_auditing` is called and the daemon is already
   not running THE SYSTEM SHALL return `status=SKIPPED`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want write
operations to run with administrator privileges, so that
`audit_control` writes, `audit -i`, and `launchctl bootstrap`
all succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   the runner for every write step (`tee` against
   `audit_control`, `audit -i`, `audit -s` reload, `launchctl
   bootstrap`, `launchctl bootout`).
2. WHEN performing reads (`list_rules`, `get_audit_status`,
   `search_events`) THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False`
   on every path.
4. WHEN `praudit` requires elevation to read certain audit log
   files (e.g., when SIP is reduced) the runner is responsible
   for surfacing the failure as a non-zero exit; the manager
   raises `MacosAuditQueryError` rather than silently producing
   an empty result.

---

### Requirement 10: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every
write verb to honour dry-run mode.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   for `add_rule`, `remove_rule`, `enable_auditing`, and
   `disable_auditing` without invoking the runner.
2. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal
   code path regardless of other configuration.
3. WHEN `dry_run=True` THE SYSTEM SHALL still populate
   `command` on the result so callers can audit the
   would-have-been argv.

---

### Requirement 11: Immutable-Mode Surfacing

**User Story:** As a maintenance coordinator, I want a clear
signal when the audit subsystem is in immutable mode, so that
my pipeline does not silently appear to succeed while ignoring
my changes.

#### Acceptance Criteria

1. WHEN `audit -s` reports `locked:yes` and the coordinator
   calls `enable_auditing` or `disable_auditing` THE SYSTEM
   SHALL return `MacosAuditActionResult(status=FAILED,
   message=...)` describing the locked state.
2. WHEN `add_rule` or `remove_rule` is dispatched against a
   subsystem whose `audit -s` reports immutable mode THE SYSTEM
   SHALL still attempt the writer; the kernel-side rejection
   surfaces as a non-zero `audit -s` reload exit, which the
   manager folds into `status=FAILED`.

---

### Requirement 12: Observability and Result Shape

**User Story:** As a maintenance coordinator, I want every
action result to carry the same fields, so that downstream
reporting can treat them uniformly.

#### Acceptance Criteria

1. WHEN any write verb returns THE SYSTEM SHALL populate
   `status`, `command`, and (when applicable) `rule_id` /
   `rule_key` on the result.
2. WHEN any write verb returns `SUCCESS` or `FAILED` THE SYSTEM
   SHALL populate `exit_code`, `stdout`, and `stderr` from the
   runner's `RunResult`.
3. WHEN any write verb returns `SKIPPED` THE SYSTEM SHALL
   populate `message` with a human-readable explanation.
