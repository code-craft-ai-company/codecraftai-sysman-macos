# Requirements

## Introduction

The scheduled-tasks feature provides the `automojo-sysman-macos` package
with the ability to manage periodic and calendar-triggered task execution
on macOS systems.  This spec implements the scheduled-tasks contract
defined at
`automojo-sysman-common/specs/contracts/scheduled-tasks/contract.md`
for macOS using **launchd** as the scheduling backend.  System-scope
tasks are written as `/Library/LaunchDaemons/<label>.plist`; user-scope
tasks are written as `~<user>/Library/LaunchAgents/<label>.plist`.
Activation and state queries go through `launchctl`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The feature
does not decide *which* tasks to schedule — it provides the capability to
*do it* reliably, idempotently, and with full auditability.  All system
commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction, and all plist file writes/deletes are
executed through the `automojo-interfaces` file-operator abstraction,
enabling dry-run simulation, auditing, and test mocking without requiring
a live system.

Scope mapping for `TaskOwner`:

- `TaskOwner.SYSTEM` → plist in `/Library/LaunchDaemons/`; activation via
  `launchctl bootstrap system <path>` and queries via
  `launchctl print system/<label>`.
- `TaskOwner.USER` → plist in `~<user>/Library/LaunchAgents/`; activation
  via `launchctl bootstrap gui/<uid> <path>` and queries via
  `launchctl print gui/<uid>/<label>`.

---

## Requirements

### Requirement 1: Create a Task

**User Story:** As a maintenance coordinator, I want to create a launchd
plist pair (plist + activation) from a `MacosTaskSpec`, so that a periodic
command runs at the requested schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_task(spec)` AND the schedule string
   is a valid launchd schedule THE SYSTEM SHALL render a plist
   containing `Label`, `ProgramArguments`, `StartCalendarInterval`
   (when the schedule is a cron-like expression) or `StartInterval`
   (when the schedule is an interval-seconds shorthand), and any
   `WorkingDirectory` / `EnvironmentVariables` values, write it via the
   `IFileOperator` to the owner-appropriate path, then dispatch
   `launchctl bootstrap <domain> <path>` through the runner with
   `elevated=True` when `spec.owner=TaskOwner.SYSTEM`.
2. WHEN `spec.schedule` is NOT a valid launchd schedule THE SYSTEM SHALL
   return `MacosScheduledTaskActionResult` with `status=FAILED`,
   `message="invalid_schedule"`, and SHALL NOT invoke the runner or
   file operator.
3. WHEN the plist already exists AND is byte-identical to the new render
   AND the activation state matches `spec.enabled` THE SYSTEM SHALL
   return `status=SKIPPED`.
4. WHEN `spec.enabled=False` THE SYSTEM SHALL write the plist but NOT
   dispatch `launchctl bootstrap`; it SHALL dispatch `launchctl disable
   <domain>/<label>` to record the disabled intent where applicable.
5. WHEN `spec.enabled=True` AND the activation dispatch fails after a
   successful write THE SYSTEM SHALL return `status=FAILED` with
   `message="bootstrap_failed"` carrying exit code / stderr.
6. WHEN the command or file operation succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the file
   operator or runner and SHALL return `status=DRY_RUN`.

---

### Requirement 2: Delete a Task

**User Story:** As a maintenance coordinator, I want to remove a launchd
plist, so that a task no longer exists on disk or in memory.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_task(name, owner)` AND the plist
   file exists THE SYSTEM SHALL dispatch `launchctl bootout
   <domain>/<label>` through the runner, then delete the plist via
   the file operator.
2. WHEN the plist does NOT exist THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN `launchctl bootout` exits non-zero THE SYSTEM SHALL still
   attempt the file delete — a missing live state is acceptable — but
   if the file delete itself fails THE SYSTEM SHALL return
   `status=FAILED` with `message="delete_plist_failed"`.
4. WHEN `launchctl bootout` fails AND the file delete succeeds THE
   SYSTEM SHALL return `status=SUCCESS` but include the bootout
   stderr in `message`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 3: Enable a Task

**User Story:** As a maintenance coordinator, I want to enable an
existing launchd job, so that a previously-disabled task resumes.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_task(name, owner)` AND the plist
   exists THE SYSTEM SHALL dispatch `launchctl enable
   <domain>/<label>`.
2. WHEN the plist does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN the job is already enabled (per `launchctl print-disabled
   <domain>` not listing the label) THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Disable a Task

**User Story:** As a maintenance coordinator, I want to disable an
existing launchd job, so that a task stops firing without deleting its
plist.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_task(name, owner)` AND the plist
   exists THE SYSTEM SHALL dispatch `launchctl disable
   <domain>/<label>`.
2. WHEN the plist does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN the job is already disabled (per `launchctl print-disabled
   <domain>` listing the label) THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 5: Run a Task Now

**User Story:** As a maintenance coordinator, I want to trigger a
launchd job immediately, so that an on-demand run happens outside its
schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_task_now(name, owner)` AND the plist
   exists THE SYSTEM SHALL dispatch `launchctl kickstart
   <domain>/<label>`.
2. WHEN the plist does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with
   captured `exit_code` / `stderr`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 6: Get a Task

**User Story:** As a maintenance coordinator, I want to resolve a task
by name, so that downstream logic can branch on its existence and state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_task(name, owner)` AND the plist
   file exists THE SYSTEM SHALL dispatch `launchctl print
   <domain>/<label>` (without elevation where the owner is the
   running user; with `elevated=True` for SYSTEM or another user's
   GUI domain) AND read the plist content via the `IFileOperator`.
2. WHEN both sources succeed THE SYSTEM SHALL return a `MacosTaskInfo`
   populated from the plist (`Label`, `ProgramArguments`,
   `StartCalendarInterval` / `StartInterval`) and the `launchctl print`
   output (`state`, `last exit code`, `next launch`).
3. WHEN the plist does not exist THE SYSTEM SHALL return `None`.
4. WHEN the plist exists but cannot be parsed THE SYSTEM SHALL return
   a `MacosTaskInfo` with `readable=False` and `name` / `owner`
   populated.
5. WHEN `launchctl print` fails for an unknown-service reason AND the
   plist exists THE SYSTEM SHALL return the plist-derived info with
   `active=None`; no exception is raised.
6. WHEN `launchctl print` fails for any other reason THE SYSTEM SHALL
   raise `MacosScheduledTaskQueryError`.

---

### Requirement 7: List Tasks

**User Story:** As a maintenance coordinator, I want to enumerate every
launchd job at a given owner scope, so that audit workflows can
inventory scheduled tasks.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_tasks(owner)` THE SYSTEM SHALL
   dispatch `launchctl print <domain>` through the runner, then
   enumerate plist files in the owner's directory via the
   `IFileOperator` to pick up plists that have not been bootstrapped.
2. WHEN both sources succeed THE SYSTEM SHALL return a merged list of
   `MacosTaskInfo` keyed on `Label`, with `active` populated from the
   `launchctl print` output when available.
3. WHEN `launchctl print <domain>` exits non-zero THE SYSTEM SHALL
   raise `MacosScheduledTaskQueryError`.
4. WHEN the owner directory is empty and `launchctl print` is empty
   THE SYSTEM SHALL return an empty list.

---

### Requirement 8: Validate a Schedule

**User Story:** As a maintenance coordinator, I want to validate a
schedule expression without dispatching any command.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate_schedule(schedule)` AND the
   string is a positive integer THE SYSTEM SHALL return `True` (treated
   as a `StartInterval` seconds value).
2. WHEN the string is a shorthand in `{minutely, hourly, daily,
   weekly, monthly, yearly}` THE SYSTEM SHALL return `True` and the
   manager SHALL render it as a `StartCalendarInterval` dict.
3. WHEN the string is a five-field cron expression (supported for
   coordinators that share schedules across OSes) THE SYSTEM SHALL
   return `True` and the manager SHALL render it by translating to a
   `StartCalendarInterval` dict (wildcards → field omitted).
4. WHEN the string matches none of the above THE SYSTEM SHALL return
   `False`.
5. THE SYSTEM SHALL NOT invoke the runner or the file operator for
   `validate_schedule`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want system-scope
writes and other-user writes to run with `sudo`, so that file writes
into `/Library/LaunchDaemons/` and `launchctl bootstrap system`
succeed on a standard macOS system where the process does not run as
root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.SYSTEM` THE
   SYSTEM SHALL pass `elevated=True` to both the file operator (for
   plist writes/deletes) and the runner (for `launchctl` invocations
   in the `system` domain).
2. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.USER` AND
   `spec.user` equals the running user THE SYSTEM SHALL NOT pass
   `elevated=True`.
3. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.USER` AND
   `spec.user` is NOT the running user THE SYSTEM SHALL pass
   `elevated=True` for every file operation and `launchctl` call
   targeting `gui/<uid>` of that user.
4. WHEN performing any read (`get_task`, `list_tasks`) for the
   running user THE SYSTEM SHALL NOT pass `elevated=True`.
5. WHEN reading another user's scope (`list_tasks(owner=USER)` for
   a target user other than the running user) THE SYSTEM SHALL pass
   `elevated=True` so the `launchctl print gui/<uid>` invocation
   succeeds.
6. WHEN `validate_schedule` is called THE SYSTEM SHALL NOT invoke the
   runner or file operator.
