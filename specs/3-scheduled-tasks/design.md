# Design

## Overview

The scheduled-tasks feature is implemented as a single module,
`automojo.sysman.macos.scheduledtasks`, that exposes a
`MacosScheduledTaskManager` class.  This spec implements the
scheduled-tasks contract defined at
`automojo-sysman-common/specs/contracts/scheduled-tasks/contract.md`
for macOS using launchd.  The coordinator instantiates the manager with
a command runner, a file operator, and a config, then calls the
contract's verbs.

All plist authoring goes through the `IFileOperator` (write / read /
delete / exists / list_dir).  All `launchctl` invocations go through
the `ICommandRunner`.  Schedule validation is a pure-Python syntactic
check.  The module has no dependency on third-party libraries.

System-scope tasks are plists in `/Library/LaunchDaemons/`; user-scope
tasks are plists in `~<user>/Library/LaunchAgents/`.  Activation uses
`launchctl bootstrap <domain> <path>`, deactivation uses `launchctl
bootout <domain>/<label>`, and queries use `launchctl print
<domain>/<label>` plus plist reads.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner, file operator (Local or SSH)        │
│  - Configures aspects (optional)                            │
│  - Injects into MacosScheduledTaskManager                   │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          MacosScheduledTaskManager                          │
│  automojo.sysman.macos.scheduledtasks                       │
│                                                             │
│  create_task()   delete_task()                              │
│  enable_task()   disable_task()   run_task_now()            │
│  get_task()      list_tasks()                               │
│  validate_schedule()                                        │
└──────────┬───────────────────────┬──────────────────────────┘
           │ file I/O              │ commands
           ▼                       ▼
┌──────────────────┐   ┌─────────────────────────────────────────┐
│  IFileOperator   │   │   ICommandRunner                        │
│  write_text()    │   │   run(args, elevated, capture_output)   │
│  read_text()     │   └────────────┬────────────────────────────┘
│  delete()        │                │
│  exists()        │     ┌──────────┴──────────────┐
│  list_dir()      │     ▼                         ▼
└──────────────────┘   ┌──────────────────────┐  ┌──────────────────────┐
                       │ LocalCommandRunner   │  │ SSHCommandRunner     │
                       └──────────────────────┘  └──────────────────────┘
                                 │                         │
                                 └──────────┬──────────────┘
                                            │ optional
                                            ▼
                       ┌─────────────────────────────────────────────────┐
                       │            Execution Aspects                    │
                       │  - PersistenceAspect, RetryAspect, TimeoutAspect│
                       └─────────────────────────────────────────────────┘
```

---

## Components

### `MacosScheduledTaskManager`

**Location:** `source/packages/automojo/sysman/macos/scheduledtasks.py`

| Method | Signature | Description |
|---|---|---|
| `create_task` | `(spec: MacosTaskSpec) → MacosScheduledTaskActionResult` | Write plist, `launchctl bootstrap` (or `disable` when `enabled=False`) |
| `delete_task` | `(name, owner) → MacosScheduledTaskActionResult` | `launchctl bootout <domain>/<label>`, delete plist |
| `enable_task` | `(name, owner) → MacosScheduledTaskActionResult` | `launchctl enable <domain>/<label>` |
| `disable_task` | `(name, owner) → MacosScheduledTaskActionResult` | `launchctl disable <domain>/<label>` |
| `run_task_now` | `(name, owner) → MacosScheduledTaskActionResult` | `launchctl kickstart <domain>/<label>` |
| `get_task` | `(name, owner) → MacosTaskInfo \| None` | `launchctl print <domain>/<label>` + plist read |
| `list_tasks` | `(owner) → list[MacosTaskInfo]` | `launchctl print <domain>` + plist dir enumeration |
| `validate_schedule` | `(schedule) → bool` | Pure-Python launchd schedule check |

### `MacosScheduledTaskManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no writes/commands execute |
| `elevated` | `bool` | `False` | When `True`, SYSTEM-owner writes and other-user writes pass `elevated=True` |
| `system_store_dir` | `str` | `"/Library/LaunchDaemons"` | Override for system-owner plist dir |
| `user_store_dir` | `str \| None` | `None` | Override for `~<user>/Library/LaunchAgents` |

---

## Data Models

### Enums

```python
class MacosScheduledTaskActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"


class MacosTaskOwner(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Dataclasses

```python
@dataclass
class MacosTaskSpec:
    name: str                       # plist Label, e.g. "com.example.backup"
    schedule: str                   # interval seconds, shorthand, or cron-like
    command: str
    arguments: list[str] | None = None
    working_dir: str | None = None
    env: dict[str, str] | None = None
    user: str | None = None
    owner: MacosTaskOwner = MacosTaskOwner.SYSTEM
    enabled: bool = True
    description: str | None = None


@dataclass
class MacosTaskInfo:
    name: str
    owner: MacosTaskOwner
    schedule: str | None = None
    command: str | None = None
    user: str | None = None
    enabled: bool | None = None
    active: bool | None = None
    last_run: datetime | None = None
    next_run: datetime | None = None
    readable: bool = True


@dataclass
class MacosScheduledTaskActionResult:
    status: MacosScheduledTaskActionStatus
    name: str
    owner: MacosTaskOwner
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None


class MacosScheduledTaskQueryError(Exception):
    """Raised when list_tasks fails, or when get_task fails for an unexpected reason."""
```

---

## Owner-Scope Mapping

| `MacosTaskOwner` | Plist directory | `launchctl` domain |
|---|---|---|
| `SYSTEM` | `/Library/LaunchDaemons/` | `system` |
| `USER` (self) | `~/Library/LaunchAgents/` | `gui/<uid>` for the running user |
| `USER` (other, via `spec.user`) | `~<user>/Library/LaunchAgents/` | `gui/<uid>` of that user; `launchctl asuser <uid>` wrapper when required |

Writes into another user's `LaunchAgents/` directory require
`elevated=True` on the file operator.  Reads from another user's GUI
domain require `elevated=True` on the runner.

---

## Plist Template

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{spec.name}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{spec.command}</string>
        <!-- one <string> per argument in spec.arguments -->
    </array>

    <!-- exactly one of StartCalendarInterval or StartInterval -->
    <key>StartCalendarInterval</key>
    <dict>
        <!-- Minute / Hour / Day / Weekday / Month keys as ints -->
    </dict>
    <!-- OR -->
    <key>StartInterval</key>
    <integer>{seconds}</integer>

    <!-- optional -->
    <key>WorkingDirectory</key>
    <string>{spec.working_dir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <!-- one <key><string> pair per entry in spec.env -->
    </dict>

    <key>UserName</key>
    <string>{spec.user}</string>           <!-- system-owner only -->

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

The rendered bytes are the idempotency key: `create_task` reads the
existing plist (when present) and compares it byte-identically to the
new render.  Any difference triggers a rewrite.

---

## `create_task` Sequence

```
1. validate_schedule(spec.schedule); FAILED if invalid.
2. Resolve owner → plist_dir + launchctl domain.
3. Render plist XML.
4. If file exists AND content matches AND activation-state matches
   spec.enabled → SKIPPED.
5. If dry_run → DRY_RUN with the argv captured for the bootstrap
   command.
6. file_operator.write_text(<plist_dir>/<label>.plist, content, elevated=…)
7. If spec.enabled:
       runner.run(["launchctl", "bootstrap", <domain>, <path>],
                  elevated=…)
       If bootstrap fails → FAILED message="bootstrap_failed"
   Else:
       runner.run(["launchctl", "disable", f"{domain}/{spec.name}"],
                  elevated=…)   # best-effort
8. Return SUCCESS.
```

## `delete_task` Sequence

```
1. If plist does not exist → SKIPPED.
2. If dry_run → DRY_RUN.
3. runner.run(["launchctl", "bootout", f"{domain}/{label}"],
               elevated=…)   # non-fatal on exit non-zero
4. file_operator.delete(<plist_dir>/<label>.plist, elevated=…)
5. Return SUCCESS.
   - If bootout failed at step 3 → SUCCESS with message carrying
     bootout stderr.
   - If step 4 failed → FAILED with message="delete_plist_failed".
```

---

## `validate_schedule` Logic

Pure-Python, no external process.

```
LAUNCHD_SHORTHANDS = {"minutely", "hourly", "daily",
                      "weekly", "monthly", "yearly"}

1. Strip whitespace.
2. If string is a positive integer → True (StartInterval seconds).
3. Lowercase-match against LAUNCHD_SHORTHANDS → True.
4. Split on whitespace; if the result has 5 fields, delegate to a
   cron five-field validator (same rules as the RHEL reference).
5. Otherwise → False.
```

When `validate_schedule` returns `True` for a cron expression, the
renderer maps:

- minute → `Minute`
- hour → `Hour`
- day-of-month → `Day`
- month → `Month`
- day-of-week → `Weekday`

Wildcards (`*`) cause the key to be omitted from
`StartCalendarInterval` (which launchd treats as "every value").

---

## `launchctl print` Parsing

`launchctl print <domain>/<label>` emits a human-readable block like:

```
com.automojo.backup = {
    active count = 1
    path = /Library/LaunchDaemons/com.automojo.backup.plist
    state = running
    last exit code = 0
    nextlaunch = 2026-04-25 02:00:00 UTC
    ...
}
```

The parser extracts:

- `state` → `active=True` when `state == "running"` or `"waiting"`,
  `False` when `state == "disabled"`.
- `last exit code` → `last_run` recorded when the field is present.
- `nextlaunch` → `next_run` (parsed via `datetime.fromisoformat`-style
  handling; best-effort).

`launchctl print-disabled <domain>` emits `"<label>" => disabled` lines
used for the enable/disable idempotency guards.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_task` | plist file exists AND byte-identical AND activation state matches | `file_operator.read_text` + `launchctl print-disabled` |
| `delete_task` | plist file does not exist | `file_operator.exists` |
| `enable_task` | label not listed as disabled | `launchctl print-disabled <domain>` |
| `disable_task` | label listed as disabled | `launchctl print-disabled <domain>` |
| `run_task_now` | never skips | — |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosScheduledTaskQueryError` | `list_tasks` fails, or `get_task` fails for a reason other than the service being unknown |

All write failures surface as
`MacosScheduledTaskActionResult(status=FAILED, ...)` carrying the
runner's (or file-operator's) exit code, stdout, stderr, and a
`message` identifying the failing step.

---

## Correctness Properties

### Property 1: Create Task Schedule Validation

*For any* `create_task(spec)` with an invalid schedule, the result is `FAILED` with `message="invalid_schedule"` and no runner / file operator calls occur.

**Validates: Requirements 1.2**

### Property 2: Create Task Plist Rendering

*For any* `create_task(spec)` with a valid schedule and no pre-existing plist, a plist file is written via the file operator at the owner's directory.

**Validates: Requirements 1.1**

### Property 3: Create Task Idempotency

*For any* `create_task(spec)` where the plist already matches byte-for-byte and the activation state matches, the result is `SKIPPED` with no dispatch.

**Validates: Requirements 1.3**

### Property 4: Create Task Bootstrap Dispatch

*For any* `create_task(spec)` with `spec.enabled=True`, `launchctl bootstrap <domain> <path>` is dispatched after the plist write.

**Validates: Requirements 1.1**

### Property 5: Create Task Disable Instead Of Bootstrap

*For any* `create_task(spec)` with `spec.enabled=False`, `launchctl disable <domain>/<label>` is dispatched instead of `bootstrap`.

**Validates: Requirements 1.4**

### Property 6: Create Task Bootstrap Failure

*For any* `create_task(spec)` where bootstrap fails after a successful write, the result is `FAILED` with `message="bootstrap_failed"`.

**Validates: Requirements 1.5**

### Property 7: Delete Task Bootout Then Delete

*For any* `delete_task(name, owner)` where the plist exists, `launchctl bootout` is dispatched and the plist is then deleted.

**Validates: Requirements 2.1**

### Property 8: Delete Task Idempotency

*For any* `delete_task(name, owner)` where the plist is absent, the result is `SKIPPED`.

**Validates: Requirements 2.2**

### Property 9: Delete Task Soft Bootout Failure

*For any* `delete_task(name, owner)` where bootout fails but the file delete succeeds, the result is `SUCCESS` with the bootout stderr in `message`.

**Validates: Requirements 2.4**

### Property 10: Delete Task Hard File Failure

*For any* `delete_task(name, owner)` where the file delete fails, the result is `FAILED` with `message="delete_plist_failed"`.

**Validates: Requirements 2.3**

### Property 11: Enable Task Dispatch

*For any* `enable_task(name, owner)` where the plist exists and the label is currently disabled, the dispatched argv is `["launchctl", "enable", f"{domain}/{label}"]`.

**Validates: Requirements 3.1**

### Property 12: Enable Task Idempotency

*For any* `enable_task(name, owner)` where the label is not listed in `launchctl print-disabled`, the result is `SKIPPED`.

**Validates: Requirements 3.3**

### Property 13: Enable Task Not Found

*For any* `enable_task(name, owner)` where the plist does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 3.2**

### Property 14: Disable Task Dispatch

*For any* `disable_task(name, owner)` where the plist exists and the label is currently enabled, the dispatched argv is `["launchctl", "disable", f"{domain}/{label}"]`.

**Validates: Requirements 4.1**

### Property 15: Disable Task Idempotency

*For any* `disable_task(name, owner)` where the label is already listed as disabled, the result is `SKIPPED`.

**Validates: Requirements 4.3**

### Property 16: Disable Task Not Found

*For any* `disable_task(name, owner)` where the plist does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 4.2**

### Property 17: Run Task Now Dispatch

*For any* `run_task_now(name, owner)` where the plist exists, the dispatched argv is `["launchctl", "kickstart", f"{domain}/{label}"]`.

**Validates: Requirements 5.1**

### Property 18: Run Task Now Not Found

*For any* `run_task_now(name, owner)` where the plist does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 5.2**

### Property 19: Get Task Populated Info

*For any* `get_task(name, owner)` where both the plist and `launchctl print` succeed, the returned `MacosTaskInfo` contains `schedule`, `command`, `active`, `last_run`, and `next_run`.

**Validates: Requirements 6.1, 6.2**

### Property 20: Get Task Missing Returns None

*For any* `get_task(name, owner)` where the plist does not exist, the result is `None`.

**Validates: Requirements 6.3**

### Property 21: Get Task Unreadable

*For any* `get_task(name, owner)` where the plist exists but cannot be parsed, the returned info has `readable=False`.

**Validates: Requirements 6.4**

### Property 22: List Tasks Merge

*For any* `list_tasks(owner)`, the returned list is the union of `launchctl print <domain>` entries and plist files enumerated in the owner's directory.

**Validates: Requirements 7.1, 7.2**

### Property 23: List Tasks Error

*For any* failure of `launchctl print <domain>`, `list_tasks` raises `MacosScheduledTaskQueryError`.

**Validates: Requirements 7.3**

### Property 24: Validate Schedule Seconds

*For any* positive integer string, `validate_schedule` returns `True`.

**Validates: Requirements 8.1**

### Property 25: Validate Schedule Shorthand

*For any* shorthand in `{minutely, hourly, daily, weekly, monthly, yearly}`, `validate_schedule` returns `True`.

**Validates: Requirements 8.2**

### Property 26: Validate Schedule Cron Fallback

*For any* valid five-field cron expression, `validate_schedule` returns `True` and the renderer maps each field into `StartCalendarInterval`.

**Validates: Requirements 8.3**

### Property 27: Validate Schedule Side-Effect Free

*For any* `validate_schedule` call, the runner and file operator are never invoked.

**Validates: Requirements 8.5**

### Property 28: Dry-Run Safety

*For any* write method with `dry_run=True`, the file operator and runner are never invoked; the result status is `DRY_RUN`.

**Validates: Requirements 1.7, 2.5, 3.4, 4.4, 5.5**

### Property 29: System-Owner Elevation

*For any* write with `spec.owner=SYSTEM` and `config.elevated=True`, both the file operator and the runner are invoked with `elevated=True`.

**Validates: Requirements 9.1**

### Property 30: Other-User-Owner Elevation

*For any* write with `spec.owner=USER` and `spec.user` differing from the running user, the file operator and runner are invoked with `elevated=True` and the runner targets the `gui/<uid>` domain of the target user.

**Validates: Requirements 9.3**

### Property 31: Reads Do Not Elevate (Self)

*For any* read at the running user's scope, the runner is invoked with `elevated=False`.

**Validates: Requirements 9.4**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── scheduledtasks.py    ← MacosScheduledTaskManager, models, config, exception

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_scheduledtasks.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import (
    MacosLocalCommandRunner, MacosLocalRunnerConfig,
    MacosLocalFileOperator, MacosLocalFileOperatorConfig,
)
from automojo.sysman.macos.scheduledtasks import (
    MacosScheduledTaskManager,
    MacosScheduledTaskManagerConfig,
    MacosTaskSpec,
    MacosTaskOwner,
)

runner = MacosLocalCommandRunner(MacosLocalRunnerConfig())
file_operator = MacosLocalFileOperator(MacosLocalFileOperatorConfig())

config = MacosScheduledTaskManagerConfig(elevated=True, dry_run=False)
task_mgr = MacosScheduledTaskManager(
    runner=runner, file_operator=file_operator, config=config,
)

spec = MacosTaskSpec(
    name="com.automojo.nightly-backup",
    schedule="daily",
    command="/usr/local/bin/backup.sh",
    owner=MacosTaskOwner.SYSTEM,
    description="Nightly filesystem backup",
)
result = task_mgr.create_task(spec=spec)
print(f"create_task: {result.status.value}")
```

### SSH Execution With Aspects

```python
from automojo.sysman.macos.runners import (
    MacosSSHCommandRunner, MacosSSHRunnerConfig,
)
from automojo.sysman.macos.aspects import (
    RetryAspect, RetryAspectConfig, TimeoutAspect, TimeoutAspectConfig,
)

runner = MacosSSHCommandRunner(
    MacosSSHRunnerConfig(host="mac-fleet-01", username="ops"),
)
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

task_mgr = MacosScheduledTaskManager(
    runner=runner, file_operator=file_operator,
    config=MacosScheduledTaskManagerConfig(elevated=True),
)
task_mgr.disable_task(
    name="com.automojo.stale-probe",
    owner=MacosTaskOwner.SYSTEM,
)
```

### Dry-Run Mode

```python
config = MacosScheduledTaskManagerConfig(elevated=True, dry_run=True)
task_mgr = MacosScheduledTaskManager(
    runner=runner, file_operator=file_operator, config=config,
)
result = task_mgr.delete_task(
    name="com.automojo.nightly-backup",
    owner=MacosTaskOwner.SYSTEM,
)
assert result.status.value == "dry_run"
print(result.command)
```

---

## Testing Strategy

1. `MockFileOperator` records every `write_text`, `delete`,
   `read_text`, `exists`, and `list_dir` call with its arguments.
2. `MockCommandRunner` keyed on argv prefix queues responses for
   `launchctl bootstrap`, `bootout`, `enable`, `disable`, `kickstart`,
   `print`, and `print-disabled` independently.
3. `create_task` tests cover: valid schedule, invalid schedule (no
   dispatch), byte-identical idempotency, render mismatch triggers
   rewrite, `enabled=False` substitutes `launchctl disable` for
   bootstrap, bootstrap failure after successful write.
4. `delete_task` tests cover: plist exists + bootout succeeds + file
   delete succeeds, bootout fails + file delete succeeds (SUCCESS
   with message), bootout succeeds + file delete fails (FAILED).
5. `enable_task` / `disable_task` tests cover: already in requested
   state, plist not found, successful dispatch.
6. `run_task_now` tests cover: plist exists (SUCCESS), plist missing
   (FAILED with message), runner failure (FAILED with captured exit
   code).
7. `get_task` tests cover: plist + launchctl both succeed (populated
   info), plist exists + launchctl unknown-service (active=None, no
   raise), plist absent (None), unreadable plist (readable=False).
8. `list_tasks` tests cover: `launchctl print <domain>` succeeds +
   directory enumeration merges without duplicates; failure raises.
9. `validate_schedule` tests cover: seconds, shorthands, cron-field
   fallback, and invalid strings.  A dedicated test proves the
   cron-to-`StartCalendarInterval` translation omits wildcard fields.
10. Dry-run tests prove every write short-circuits without invoking
    the file operator or runner.
11. Elevation tests prove SYSTEM-owner writes elevate, USER-self
    writes do not, and USER-other writes elevate both file-op and
    runner.
12. No test requires root access or a live macOS launchd instance.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `create_task` | file-op plist write + `launchctl bootstrap` (or `disable`) |
| `delete_task` | `launchctl bootout` + file-op plist delete |
| `enable_task` | `launchctl enable <domain>/<label>` |
| `disable_task` | `launchctl disable <domain>/<label>` |
| `run_task_now` | `launchctl kickstart <domain>/<label>` |
| `get_task` | `launchctl print <domain>/<label>` + plist read |
| `list_tasks` | `launchctl print <domain>` + plist directory enumeration |
| `validate_schedule` | pure-Python seconds / shorthand / cron check |

---

## Extensions Beyond the Contract

### KeepAlive / RunAtLoad Semantics

launchd supports `KeepAlive` (keep the process running, restart on
exit) and `RunAtLoad` (run immediately when the plist is loaded).
These are not part of the base contract surface but a future extension
will allow a coordinator to flip them via extra fields on a subclass
of `MacosTaskSpec`.

### LoginWindow / Per-Session Agents

Per-session user agents (`~/Library/LaunchAgents/` with
`LoginWindow`-domain activation) are intentionally out of scope.  If
a caller needs them, an extension will expose a
`MacosLoginWindowScheduledTaskManager` that honors the same contract
but bootstraps into the `loginwindow` domain.
