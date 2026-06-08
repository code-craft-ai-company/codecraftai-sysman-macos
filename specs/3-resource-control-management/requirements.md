# Requirements

## Introduction

The resource-control-management feature provides the
`codecraftai-sysman-macos` package with the ability to manage
named, idempotent, auditable resource ceilings for groups of
processes on macOS systems.  The implementation rides on
**launchd** (per-daemon `ResourceLimits` keys + `launchctl
bootstrap`) and **`taskpolicy`** (per-process QoS class
assignment), because macOS has no cgroup-equivalent kernel
substrate for arbitrary per-group resource ceilings.

This package satisfies the cross-OS
[`resource-control-management` contract](../../../codecraftai-sysman-common/specs/contracts/resource-control-management/contract.md)
**with documented limitations** — several verbs return
``status=FAILED`` with a typed `detail`/`message` token when the
underlying tooling does not exist on macOS.  Those gaps are
deliberate and prominent (see Requirement 13 below).

The macOS manager covers:

- Creating and deleting "logical" resource groups represented
  as launchd plists at
  `/Library/LaunchDaemons/codecraftai-rc-<name>.plist`.
- Setting per-group memory ceilings via the launchd
  `HardResourceLimits` dict (specifically `RLIMIT_AS`).
- Best-effort per-PID CPU / IO QoS adjustment via
  `taskpolicy -p <pid>` for processes that have been
  attached to the group.
- Listing all groups by enumerating plists with the
  `codecraftai-rc-` prefix.
- Aggregating per-PID `top` snapshots into a per-group
  usage summary.

All system commands are dispatched through the injected
`ICommandRunner`.  This feature does NOT consume an
`IFileOperator`; plist files are read through `cat` and written
through `tee`.

This feature complements — and does not duplicate — the launchd
service-lifecycle surface owned by `3-service-management`.

---

## Requirements

### Requirement 1: Create a Resource Group

**User Story:** As a maintenance coordinator, I want to declare
a named resource group with optional initial memory ceiling, so
that subsequently attached daemons inherit the ceiling.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_group(spec)` with
   `spec.name == "batch"` THE SYSTEM SHALL render a launchd
   plist body whose `Label` is `"codecraftai-rc-batch"` and whose
   `HardResourceLimits.RLIMIT_AS` is set to `spec.memory_bytes`
   when present.
2. WHEN the on-disk plist does not exist THE SYSTEM SHALL write
   `/Library/LaunchDaemons/codecraftai-rc-batch.plist` via `tee`
   and dispatch
   `launchctl bootstrap system /Library/LaunchDaemons/codecraftai-rc-batch.plist`.
3. WHEN the on-disk plist matches the rendered body
   byte-for-byte THE SYSTEM SHALL return `status=SKIPPED`
   without invoking `tee` or `launchctl bootstrap`.
4. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` carrying the planned `tee` argv.

---

### Requirement 2: Delete a Resource Group

**User Story:** As a maintenance coordinator, I want to remove a
previously created resource group's plist and unload it.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` THE SYSTEM
   SHALL invoke `launchctl bootout system/codecraftai-rc-<name>`
   (best-effort) and `rm -f
   /Library/LaunchDaemons/codecraftai-rc-<name>.plist`.
2. WHEN the plist file does not exist THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the runner.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` with the planned `bootout` argv.

---

### Requirement 3: Set CPU Limit (FAILED with macOS detail)

**User Story:** As a maintenance coordinator using a cross-OS
configuration, I want `set_cpu_limit` to fail loudly on macOS
so my orchestration tooling can detect the gap without silent
data corruption.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_cpu_limit(name, percent)` on
   macOS THE SYSTEM SHALL return `status=FAILED` with
   `detail="macos_no_per_group_cpu_limit"` and
   `message="macOS launchd has no per-group CPU quota"` WITHOUT
   invoking the runner.
2. THE SYSTEM SHALL NOT raise from `set_cpu_limit`; the
   FAILED result is the contract-conformant error surface.
3. THE SYSTEM SHALL include the would-have-been argv on
   `result.command` (the documented best-effort
   `taskpolicy -c utility -p <PID>` fragment) for audit.

---

### Requirement 4: Set Memory Limit

**User Story:** As a maintenance coordinator, I want to set a
group's memory ceiling on macOS, so that daemons spawned under
the group inherit a `RLIMIT_AS` limit.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_memory_limit(name, bytes_)`
   on an existing group THE SYSTEM SHALL re-render the plist
   body with the new `HardResourceLimits.RLIMIT_AS` value, write
   the plist via `tee`, and re-bootstrap the launchd label
   (`launchctl bootout` followed by `launchctl bootstrap`).
2. WHEN the on-disk plist's `RLIMIT_AS` already equals
   `bytes_` THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the group does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="group_not_found"`.
4. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

The macOS `RLIMIT_AS` only applies to children of the
launchd-managed daemon and DOES NOT retroactively limit running
processes.  This limitation is documented in the design note.

---

### Requirement 5: Set IO Limits (FAILED with macOS detail)

**User Story:** As a maintenance coordinator using a cross-OS
configuration, I want `set_io_limits` to fail loudly on macOS
so my orchestration tooling can detect the gap.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_io_limits(name, spec)` on
   macOS THE SYSTEM SHALL return `status=FAILED` with
   `detail="macos_no_per_group_io_limit"` and
   `message="macOS has no per-group IO bandwidth limit; per-PID I/O priority via taskpolicy -d only"`
   WITHOUT invoking the runner.

---

### Requirement 6: Attach Process

**User Story:** As a maintenance coordinator, I want to attach a
running PID to a macOS resource group, applying its policy
ceilings best-effort.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_process(name, pid)` THE
   SYSTEM SHALL invoke `taskpolicy -p <pid> -c utility` (the
   QoS class corresponding to a constrained workload).
2. WHEN the group's plist file does not exist THE SYSTEM SHALL
   return `status=FAILED` with `message="group_not_found"`.
3. WHEN the group's `attached_pids` list (recorded inside the
   plist body) already includes `<pid>` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. THE SYSTEM SHALL re-render the plist's `attached_pids`
   array, re-write the plist body, and re-bootstrap on success.
5. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

`taskpolicy` only adjusts QoS class — it is NOT a true
group-attachment mechanism.  The plist's `attached_pids`
array is purely a manager-side bookkeeping artefact;
processes are not actually members of any kernel-side
container after attachment.

---

### Requirement 7: Detach Process

**User Story:** As a maintenance coordinator, I want to detach a
PID from a macOS resource group, restoring its default QoS.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_process(name, pid)` THE
   SYSTEM SHALL invoke `taskpolicy -p <pid> -c default`.
2. WHEN the group's `attached_pids` list does NOT include the
   PID THE SYSTEM SHALL return `status=SKIPPED`.
3. THE SYSTEM SHALL re-render the plist with the PID removed
   from `attached_pids` and re-bootstrap on success.

---

### Requirement 8: List Resource Groups

**User Story:** As a maintenance coordinator, I want to
enumerate every resource group the manager knows about.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL
   list `/Library/LaunchDaemons/` via `ls -1` and select every
   entry matching `codecraftai-rc-*.plist`.
2. FOR each entry THE SYSTEM SHALL `cat` the plist, parse the
   embedded fields, and assemble a
   `MacosResourceGroupInfo` record.
3. THE SYSTEM SHALL return an empty list (not raise) when no
   matching plist is present.
4. WHEN any underlying `ls` exits non-zero AND its stderr is
   non-empty THE SYSTEM SHALL raise
   `MacosResourceControlQueryError`.

---

### Requirement 9: Get Group Usage

**User Story:** As a maintenance coordinator, I want a per-group
usage snapshot on macOS.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group_usage(name)` THE
   SYSTEM SHALL read the group's plist for the
   `attached_pids` array and invoke
   `top -l 1 -stats pid,cpu,mem -pid <pid>` for each attached
   PID.
2. THE SYSTEM SHALL aggregate the parsed CPU percent and
   memory bytes across every PID into a single
   `MacosResourceGroupUsage` record.
3. THE SYSTEM SHALL set `cpu_usage_seconds` to `None` (macOS
   `top` reports cumulative CPU percent, not seconds).
4. THE SYSTEM SHALL set `io_read_bytes` and `io_write_bytes`
   to `None` (macOS `top` does not report per-PID IO byte
   counters).
5. WHEN the group's plist file does not exist THE SYSTEM SHALL
   raise `MacosResourceControlQueryError` with
   `detail="unknown_group"`.

---

### Requirement 10: Idempotency

**User Story:** As a maintenance coordinator, I want every
write verb to short-circuit when the requested state already
holds.

#### Acceptance Criteria

1. THE SYSTEM SHALL implement the per-verb idempotency rules
   listed in Requirements 1-7 above.
2. THE SYSTEM SHALL NOT invoke `tee` or `launchctl bootstrap`
   when the idempotency guard fires.
3. THE SYSTEM SHALL still record the would-have-been argv on
   the returned `MacosResourceControlActionResult.command`
   field for audit.

---

### Requirement 11: Elevation

**User Story:** As a maintenance coordinator, I want every
write verb to elevate (`sudo`) when the manager is configured
for elevation, and every read verb to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated == True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for write-verb mutating
   commands (`tee`, `rm`, `launchctl`, `taskpolicy`).
2. THE SYSTEM SHALL NOT pass `elevated=True` for the
   idempotency-guard `cat`, `ls`, or `test -e` probes.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs
   (`list_groups`, `get_group_usage`).

---

### Requirement 12: Dry-Run, and Error Reporting

**User Story:** As a maintenance coordinator, I want every
mutating operation to respect dry-run and surface failures
structurally.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   `tee`, `launchctl`, `taskpolicy`, or `rm`.  A
   `MacosResourceControlActionResult(status=DRY_RUN)` is
   returned with the intended argv on `command`.
2. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `MacosResourceControlActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.
3. THE SYSTEM SHALL NOT raise from any write verb on a
   dispatched-command failure.

---

### Requirement 13: Documented macOS Limitations

**User Story:** As a maintenance coordinator running cross-OS
configurations, I want the manager's gaps relative to the
contract documented in one place.

#### Acceptance Criteria

1. THE SYSTEM SHALL surface the following limitations
   prominently in the design note and in the affected verb
   docstrings:
   - Per-group CPU quota: NOT SUPPORTED.  `set_cpu_limit`
     returns `FAILED` with
     `detail="macos_no_per_group_cpu_limit"`.
   - Per-group IO bandwidth: NOT SUPPORTED.  `set_io_limits`
     returns `FAILED` with
     `detail="macos_no_per_group_io_limit"`.
   - `attach_process` / `detach_process`: best-effort QoS
     class adjustment via `taskpolicy`; NOT a true kernel-side
     container.  Documented in the verb's docstring.
   - Memory ceilings: apply only to launchd daemon's children
     via `RLIMIT_AS`; cannot be retroactively applied to
     already-running processes.
2. THE SYSTEM SHALL include the gap details in
   `result.message` so callers can branch on them
   programmatically.
