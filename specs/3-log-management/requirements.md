# Requirements

## Introduction

The log management feature provides the `automojo-sysman-macos` package with the
ability to inspect, tail, rotate, truncate, and configure rotation of logs on macOS
systems.  This spec implements the log-management contract defined at
`automojo-sysman-common/specs/contracts/log-management/contract.md` for macOS
using the unified-logging `log` CLI (`log show`, `log stream`, `log config`),
BSD-style rotation via `newsyslog`, and plain-file truncation via the `IFileOperator`.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
logs to retrieve or rotate — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed exclusively
through the `automojo-interfaces` command-runner abstraction, and all rotation
configuration file I/O is delegated to the `IFileOperator` abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are made inside this
module, enabling dry-run simulation, auditing, and test mocking without requiring a
live macOS host.

The abstract log-source identifier on macOS is either a unified-logging `subsystem`
string (e.g., `com.apple.ssh`), a unified-logging predicate expression
(`predicate:eventMessage CONTAINS 'foo'`), or a filesystem path for BSD syslog files
(e.g., `/var/log/system.log`).

---

## Requirements

### Requirement 1: Retrieve Recent Log Entries

**User Story:** As a maintenance coordinator, I want to retrieve recent entries from a
named log source with optional filters (lines, since, until, severity, pattern), so
that I can inspect recent activity without streaming the full history.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_log(source, lines, since, until, severity, pattern)`
   and `source` resolves to a unified-logging subsystem THE SYSTEM SHALL invoke
   `log show --predicate 'subsystem == "<source>"' --style json` plus flag
   equivalents (`--last <duration>` derived from `since`, `--info`/`--debug` from
   `severity`).
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `tail -n <lines> <source>` (when `lines` is supplied) or `cat <source>`
   otherwise, with subsequent in-process filtering by `since`, `until`, `severity`,
   and `pattern`.
3. WHEN the command succeeds THE SYSTEM SHALL parse stdout into a `MacosLogEntries`
   record whose `entries` list is in chronological order.
4. WHEN `lines` is supplied and the parsed entry count equals `lines` THE SYSTEM
   SHALL set `truncated=True`.
5. WHEN the source does not exist THE SYSTEM SHALL return an empty
   `MacosLogEntries`.
6. WHEN any other failure occurs THE SYSTEM SHALL raise `MacosLogQueryError`.
7. WHEN `get_log` is invoked THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 2: Tail a Log Source

**User Story:** As a maintenance coordinator, I want to follow a log source as it
emits entries, so that I can observe live activity.

#### Acceptance Criteria

1. WHEN the coordinator calls `tail_log(source, follow=True)` and `source` resolves
   to a unified-logging subsystem THE SYSTEM SHALL invoke
   `log stream --predicate 'subsystem == "<source>"' --style json` via the streaming
   runner API and yield parsed `MacosLogEntry` values.
2. WHEN `source` resolves to a filesystem path THE SYSTEM SHALL invoke
   `tail -F <source>` via the streaming runner API and yield one `MacosLogEntry`
   per emitted line.
3. WHEN `follow=False` THE SYSTEM SHALL invoke the non-stream variant
   (`log show` / `tail -n <n>`) and yield the backlog once, then terminate.
4. WHEN the underlying stream is interrupted THE SYSTEM SHALL propagate the
   interrupt so the caller's iteration ends cleanly.
5. WHEN the source does not exist THE SYSTEM SHALL raise `MacosLogQueryError` on
   first iteration.
6. WHEN `tail_log` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 3: List Available Log Sources

**User Story:** As a maintenance coordinator, I want to enumerate the available log
sources, so that I can build tooling that does not hard-code subsystem or path names.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_logs()` THE SYSTEM SHALL invoke
   `log config --list --no-pager` (for known subsystems) and `ls -1 /var/log` via
   the command runner, then compose the results.
2. WHEN a subsystem is listed THE SYSTEM SHALL produce one `MacosLogInfo` with
   `source=<subsystem>`, `path=None`, `size_bytes=None`, and `rotating` reflecting
   whether a `newsyslog` stanza exists for that subsystem-derived file (`False` if
   no associated file).
3. WHEN a plain file is listed THE SYSTEM SHALL produce one `MacosLogInfo` with
   `source="/var/log/<name>"`, `path="/var/log/<name>"`, `size_bytes` populated
   from `stat -f%z <path>`, and `rotating` reflecting `newsyslog` stanza presence.
4. WHEN a command in the composition fails THE SYSTEM SHALL raise
   `MacosLogQueryError`.

---

### Requirement 4: Rotate a Log Source

**User Story:** As a maintenance coordinator, I want to trigger rotation for a named
log source, so that old content is archived and the active file starts fresh.

#### Acceptance Criteria

1. WHEN the coordinator calls `rotate_log(source)` and `source` resolves to a file
   THE SYSTEM SHALL invoke `newsyslog -nvvF -R force-rotate` via the command runner.
2. WHEN `source` resolves to a unified-logging subsystem THE SYSTEM SHALL compute
   the associated emitter service (via `_subsystem_to_launch_label`) and invoke
   `launchctl kill SIGHUP gui/<uid>/<label>` OR `launchctl kill SIGHUP system/<label>`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `MacosLogActionResult(status=SUCCESS, ...)`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with exit code,
   stdout, stderr, and command populated.
5. WHEN `dry_run=True` THE SYSTEM SHALL invoke `newsyslog -nvv` (documented
   debug/simulation exception) and return `status=DRY_RUN` with stdout preserved.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for all non-debug
   invocations.

---

### Requirement 5: Truncate a Log Source

**User Story:** As a maintenance coordinator, I want to truncate a plain-file log
source to zero bytes, so that disk usage is reclaimed without restarting the emitter.

#### Acceptance Criteria

1. WHEN the coordinator calls `truncate_log(source)` and `source` resolves to a
   filesystem path THE SYSTEM SHALL invoke
   `IFileOperator.write_text(path=source, content="", elevated=elevated)`.
2. WHEN `source` resolves to a unified-logging subsystem THE SYSTEM SHALL return
   `status=FAILED` with `detail="unified-log subsystems cannot be truncated; use
   log erase --all if you truly want that"` and SHALL NOT invoke the runner.
3. WHEN the operation succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the operation fails THE SYSTEM SHALL return `status=FAILED` with `message`
   set from the file operator exception.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the file operator and SHALL
   return `status=DRY_RUN`.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the file operator.

---

### Requirement 6: Configure Log Rotation

**User Story:** As a maintenance coordinator, I want to persist a rotation policy
for a named source, so that `newsyslog` manages retention automatically.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure_rotation(source, policy)` THE SYSTEM
   SHALL render `policy` into a `newsyslog.conf`-compatible stanza and prepare the
   write target `/etc/newsyslog.d/<source-basename>.conf`.
2. WHEN the target file already exists with identical rendered content THE SYSTEM
   SHALL return `status=SKIPPED` without calling `write_text`.
3. WHEN the content differs or the file does not exist THE SYSTEM SHALL call
   `IFileOperator.write_text(path=target, content=rendered, elevated=elevated)` and
   return `status=SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL compute a unified diff, populate `detail`,
   and return `status=DRY_RUN` without calling `write_text`.

---

### Requirement 7: Remove a Rotation Policy

**User Story:** As a maintenance coordinator, I want to remove a rotation policy for
a source, so that `newsyslog` no longer rotates it according to our stanza.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_rotation_config(source)` and the target file
   does not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the target exists THE SYSTEM SHALL invoke
   `IFileOperator.delete(path=target, elevated=elevated)` and return
   `status=SUCCESS`.
3. WHEN `dry_run=True` THE SYSTEM SHALL NOT call `delete` and SHALL return
   `status=DRY_RUN`.
4. WHEN the delete fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 8: Query a Rotation Policy

**User Story:** As a maintenance coordinator, I want to inspect the current rotation
policy for a source, so that I can decide whether to update or remove it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_rotation_config(source)` and the target does not
   exist THE SYSTEM SHALL return `None`.
2. WHEN the target exists THE SYSTEM SHALL read it via `IFileOperator.read_text`
   and parse the stanza into a `MacosRotationPolicy`.
3. WHEN the parse fails THE SYSTEM SHALL raise `MacosLogQueryError`.
4. WHEN `get_rotation_config` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 9: Query Disk Usage

**User Story:** As a maintenance coordinator, I want to report disk usage for a
named source, so that retention tuning is driven by real figures.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_disk_usage(source)` and `source` resolves to
   a unified-logging subsystem THE SYSTEM SHALL invoke
   `du -sh /var/db/diagnostics` and `du -sh /var/db/uuidtext` via the runner, sum
   the sizes, and return a `MacosLogDiskUsage`.
2. WHEN `source` resolves to a file THE SYSTEM SHALL invoke `stat -f%z <path>` and
   convert bytes into a human-readable size.
3. WHEN the command succeeds THE SYSTEM SHALL return `MacosLogDiskUsage(source,
   size_human, size_bytes)`.
4. WHEN the command fails THE SYSTEM SHALL raise `MacosLogQueryError`.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all write operations to run
with root privileges, so that `newsyslog`, `launchctl kill`, and drop-in writes
under `/etc/newsyslog.d/` succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner and file
   operator for `rotate_log`, `truncate_log`, `configure_rotation`, and
   `remove_rotation_config`.
2. WHEN performing reads THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on every path.
4. WHEN a non-root runner hits a permission denial during a read THE SYSTEM SHALL
   raise `MacosLogQueryError` with captured stderr.

---

### Requirement 11: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write verb to honour
dry-run mode, so that plans can be validated without system change.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for
   `truncate_log`, `configure_rotation`, and `remove_rotation_config` without
   invoking any mutating runner or file-operator method.
2. WHEN `dry_run=True` AND the verb is `rotate_log` THE SYSTEM SHALL invoke
   `newsyslog -nvv` (no rotation occurs), capture stdout, and return
   `status=DRY_RUN` with output preserved.
3. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code path regardless
   of other configuration.
