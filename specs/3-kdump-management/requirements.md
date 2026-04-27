# Requirements

## Introduction

The kdump-management feature provides the `automojo-sysman-macos`
package with a contract-conforming surface for the
`kdump-management` cross-OS contract defined at
`automojo-sysman-common/specs/contracts/kdump-management/contract.md`.

**macOS has no kdump equivalent.** The Linux concept of pre-reserving
memory for a capture kernel and arming `kexec` does not map onto
Darwin's panic handler. macOS instead writes panic-and-diagnostic
reports automatically to `/Library/Logs/DiagnosticReports/` from the
panicking kernel context; for deeper system-state captures, operators
run `sysdiagnose` ad hoc. There is no opt-in service to enable, no
runtime-configurable target, and no reservation knob.

This spec is intentionally a contract-conforming **skeleton**:

* The mutating verbs (`enable_kdump`, `disable_kdump`,
  `configure_kdump_target`) MUST return a `FAILED`
  `MacosKdumpActionResult` whose `message` describes the macOS native
  model, rather than pretend to act. The cross-OS contract explicitly
  permits this: "Any verb whose semantics do not map to the native OS
  facility MUST return `FAILED` with `detail='not supported on <os>:
  <reason>'` rather than raise."
* `get_kdump_status()` returns a synthetic always-on status pointing
  at `/Library/Logs/DiagnosticReports`.
* `list_recent_dumps()`, `clear_old_dumps()`, and `get_dump_info()`
  ARE meaningful on macOS and operate over the diagnostic-report
  directory.

A maintenance coordinator running in an external package drives all
operations through the public interface. Every command is dispatched
through the `automojo-interfaces` command-runner abstraction.

---

## Requirements

### Requirement 1: Always-On Native Model

**User Story:** As a maintenance coordinator, I want the kdump-
management feature on macOS to honestly reflect that macOS panic
capture is always-on and not user-configurable, so that compliance
checks do not silently believe a no-op succeeded.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_kdump()` THE SYSTEM SHALL
   return a `MacosKdumpActionResult` with status `FAILED` and `message`
   carrying the advisory "macOS has no kdump equivalent; kernel-panic
   capture is always-on. Use sysdiagnose for system-state captures."
2. WHEN the coordinator calls `disable_kdump()` THE SYSTEM SHALL
   return the same `FAILED` result with the same advisory.
3. WHEN the coordinator calls `configure_kdump_target(spec=...)` THE
   SYSTEM SHALL return the same `FAILED` result with the same
   advisory and SHALL echo the requested target path onto the
   `target` field for audit purposes.
4. THE SYSTEM SHALL NOT execute any command when servicing the three
   unsupported-write verbs.
5. THE SYSTEM SHALL NEVER raise an exception from these verbs;
   failures surface only through the result.

---

### Requirement 2: Synthetic Status

**User Story:** As a maintenance coordinator, I want to query kdump
status on macOS and receive a deterministic synthetic snapshot so
that cross-OS reporting code does not need to special-case macOS.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_kdump_status()` THE SYSTEM SHALL
   return a `MacosKdumpStatus` with `enabled=True`, `running=True`,
   `target_kind=LOCAL_PATH`, `target_path=/Library/Logs/DiagnosticReports`
   (or the configured override), and `message="macOS native kernel-
   panic logging"`.
2. THE SYSTEM SHALL NOT execute any command when servicing this verb.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for status queries.

---

### Requirement 3: List Recent Dumps

**User Story:** As a maintenance coordinator, I want to enumerate
recently captured panic / diagnostic reports so that I can audit how
often a host is panicking.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_recent_dumps()` THE SYSTEM SHALL
   invoke `ls -1t <report_dir>` via the runner without elevation.
2. THE SYSTEM SHALL filter the listing to entries ending in `.panic`
   or `.ips`.
3. THE SYSTEM SHALL return one `MacosKdumpInfo` per surviving entry,
   freshest first, with `id` set to the filename and `path` set to
   `<report_dir>/<id>`.
4. WHEN `<report_dir>` does not exist THE SYSTEM SHALL return an
   empty list.
5. WHEN `ls` fails for any other reason THE SYSTEM SHALL raise
   `MacosKdumpQueryError`.

---

### Requirement 4: Clear Old Dumps

**User Story:** As a maintenance coordinator, I want to purge panic /
diagnostic reports older than a threshold so that the report directory
does not grow without bound.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_old_dumps(age_days=N)` THE
   SYSTEM SHALL invoke `find <report_dir> -maxdepth 1 -type f
   \( -name '*.panic' -o -name '*.ips' \) -mtime +N -exec rm -f {}
   \;` via the runner with elevated privileges.
2. WHEN `age_days < 0` THE SYSTEM SHALL return a `FAILED` result with
   `message="age_days must be >= 0"` and SHALL NOT execute the
   command.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the find
   command.
4. THE SYSTEM SHALL return `SUCCESS` even when no files match the
   filter.

---

### Requirement 5: Get Dump Info

**User Story:** As a maintenance coordinator, I want to surface
metadata about a single panic report (kernel version, panic message,
size, mtime) so that I can attach context to a maintenance ticket.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dump_info(dump_id=ID)` THE SYSTEM
   SHALL `cat <report_dir>/<ID>` via the runner without elevation.
2. THE SYSTEM SHALL parse the first ~30 lines for the
   `Darwin Kernel Version` line and the first `panic(` line.
3. THE SYSTEM SHALL also `stat -f %Sm;%z <report_dir>/<ID>` to
   populate `created` and `size_bytes`.
4. THE SYSTEM SHALL return a `MacosKdumpInfo` carrying `id`, `path`,
   `created`, `size_bytes`, `kernel`, and `panic_message`.
5. WHEN the file is unreadable THE SYSTEM SHALL raise
   `MacosKdumpQueryError`.

---

### Requirement 6: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to
elevate when configured but reads to never elevate, so that the
manager works on hosts that do not run as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is `clear_old_dumps`
   THE SYSTEM SHALL pass `elevated=True` to the runner.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read (`get_kdump_status`, `list_recent_dumps`,
   `get_dump_info`) THE SYSTEM SHALL never pass `elevated=True`,
   regardless of the config.
4. THE SYSTEM SHALL never execute any command when servicing the
   unsupported-write stubs, so elevation is moot for those.

---

### Requirement 7: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that exercises the planning logic without touching the host.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND `clear_old_dumps` is invoked THE
   SYSTEM SHALL NOT call the runner and SHALL return `DRY_RUN` with
   the would-be argv on `command`.
2. THE SYSTEM SHALL NEVER write to the report directory in dry-run
   mode.
3. Read verbs SHALL execute unchanged in dry-run mode -- reads do
   not mutate.
4. The unsupported-write stubs SHALL return `FAILED` regardless of
   dry-run mode -- the macOS rationale is independent of execution.

---

### Requirement 8: Configurable Report Directory

**User Story:** As a maintenance coordinator, I want to override the
report directory in test environments where the standard location is
not available.

#### Acceptance Criteria

1. WHEN `MacosKdumpManagerConfig(report_dir="/tmp/reports")` is
   passed THE SYSTEM SHALL use the override for every read and every
   `clear_old_dumps` invocation.
2. WHEN `report_dir` is omitted THE SYSTEM SHALL default to
   `/Library/Logs/DiagnosticReports`.
3. THE SYSTEM SHALL echo the configured `report_dir` onto the
   `target` field of every `MacosKdumpActionResult` it returns,
   except for the unsupported-write stubs whose `target` reflects the
   caller's spec.

---

### Requirement 9: Typed Errors

**User Story:** As a maintenance coordinator, I want typed exceptions
for unrecoverable read failures so that incident triage tooling can
distinguish them from operational failures.

#### Acceptance Criteria

1. WHEN `list_recent_dumps()` fails for a reason other than the
   directory being absent THE SYSTEM SHALL raise
   `MacosKdumpQueryError` carrying `command`, `exit_code`, and
   `stderr`.
2. WHEN `get_dump_info()` fails to read the report file THE SYSTEM
   SHALL raise `MacosKdumpQueryError` carrying the same fields.
3. THE SYSTEM SHALL never raise exceptions from `get_kdump_status()`,
   `clear_old_dumps()`, or any unsupported-write stub.

---

### Requirement 10: Honest Documentation of Divergence

**User Story:** As a maintenance coordinator reading the spec, I
want the macOS divergence from the cross-OS contract to be documented
in plain English so that I am not surprised by `FAILED` results in
the field.

#### Acceptance Criteria

1. THE design.md SHALL include a cross-OS concept-mapping table
   showing exactly which verbs reduce to `FAILED` on macOS and why.
2. THE design.md SHALL describe the macOS-native model (panic logs
   in `/Library/Logs/DiagnosticReports/`, ad-hoc `sysdiagnose`).
3. THE module docstring of `kdumpmanagement.py` SHALL reproduce the
   essential prose so that `help(MacosKdumpManager)` is informative
   on its own.
