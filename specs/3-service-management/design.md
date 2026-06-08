# Design

## Overview

The service-management feature is implemented as a single module,
`ccai.sysman.macos.servicemanagement`, that exposes a `MacosServiceManager`
class.  This spec implements the service-management contract defined at
`codecraftai-sysman-common/specs/contracts/service-management/contract.md` for macOS
using `launchctl`.  The coordinator instantiates `MacosServiceManager` with a
command runner, an optional file operator (used only by the mask fallback), and a
config, then calls its verbs.

macOS has no direct `mask`/`unmask` semantics, so the manager emulates masking via
disable + an override plist that sets `Disabled=true`.  The override plist is
written through `IFileOperator.write_text` so dry-run and aspect composition apply.
Callers can turn the fallback off, in which case `mask_service` aliases
`disable_service` and `unmask_service` aliases `enable_service`.

The feature works identically whether commands execute locally (via
`MacosLocalCommandRunner`) or remotely over SSH (via `MacosSSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + optional file operator             │
│  - Configures aspects (optional)                            │
│  - Injects into MacosServiceManager                         │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosServiceManager                              │
│  ccai.sysman.macos.servicemanagement                    │
│                                                             │
│  start_service()  stop_service()  restart_service()         │
│  reload_service() enable_service()  disable_service()       │
│  mask_service()   unmask_service()                          │
│  get_service_status()  list_services()  reload_daemon()     │
└───────┬─────────────────────────────────────────┬───────────┘
        │ runner                                  │ fileop (mask fallback)
        ▼                                         ▼
   ICommandRunner                           IFileOperator
```

---

## Components

### `MacosServiceManager`

**Location:** `source/packages/ccai/sysman/macos/servicemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `start_service` | `(name, scope) → MacosServiceActionResult` | `launchctl kickstart -k <domain>/<name>` |
| `stop_service` | `(name, scope) → MacosServiceActionResult` | `launchctl bootout <domain>/<name>` |
| `restart_service` | `(name, scope) → MacosServiceActionResult` | `launchctl kickstart -k <domain>/<name>` |
| `reload_service` | `(name, scope) → MacosServiceActionResult` | `launchctl kill SIGHUP <domain>/<name>` |
| `enable_service` | `(name, scope) → MacosServiceActionResult` | `launchctl enable <domain>/<name>` |
| `disable_service` | `(name, scope) → MacosServiceActionResult` | `launchctl disable <domain>/<name>` |
| `mask_service` | `(name, scope) → MacosServiceActionResult` | `disable` + override-plist write (or alias to disable) |
| `unmask_service` | `(name, scope) → MacosServiceActionResult` | override-plist removal + `enable` (or alias to enable) |
| `get_service_status` | `(name, scope) → MacosServiceStatus` | `launchctl print <domain>/<name>` |
| `list_services` | `(scope) → list[MacosServiceInfo]` | `launchctl list` ∪ `launchctl print-disabled <domain>` |
| `reload_daemon` | `(scope) → MacosServiceActionResult` | no-op |

### `MacosServiceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write dispatches |
| `elevated` | `bool` | `False` | When `True`, system-scope writes pass `elevated=True` |
| `target_uid` | `int \| None` | `None` | UID used for `gui/<uid>` USER-scope domain |
| `mask_fallback_enabled` | `bool` | `True` | When `True`, mask/unmask use the two-step disable+override-plist strategy |
| `system_override_plist_dir` | `str` | `"/Library/LaunchDaemons"` | Directory for system-scope override plists |
| `user_override_plist_dir` | `str` | `"~/Library/LaunchAgents"` | Directory (tilde-expanded against the target user) for user-scope override plists |

---

## Data Models

### Enums

```python
class MacosServiceActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class MacosServiceScope(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Result / Info Types

```python
@dataclass
class MacosServiceActionResult:
    status: MacosServiceActionStatus
    service_name: str
    scope: MacosServiceScope
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None

@dataclass
class MacosServiceStatus:
    service_name: str
    scope: MacosServiceScope
    active_state: str      # "active" | "inactive" | "not-found"
    enabled_state: str     # "enabled" | "disabled" | "masked" | "not-found"
    main_pid: int | None = None
    description: str | None = None

@dataclass
class MacosServiceInfo:
    service_name: str
    scope: MacosServiceScope
    active_state: str
    enabled_state: str
    description: str | None

class MacosServiceQueryError(Exception):
    """Raised when a read fails for an unexpected reason."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (mask fallback only)

```python
class IFileOperator(Protocol):
    def write_text(self, path, content, *, elevated, encoding="utf-8") -> FileResult: ...
    def remove(self, path, *, elevated) -> FileResult: ...
```

---

## Scope Mapping

| `MacosServiceScope` | Domain string |
|---|---|
| `SYSTEM` | `"system"` |
| `USER` | `f"gui/{config.target_uid}"` |

The domain is used to build `<domain>/<name>` arguments for most verbs and
`<domain>` for `print-disabled` / `list`.  When `scope=USER` and `target_uid` is
`None`, the manager raises `ValueError` before dispatching any command.

---

## Command Mappings

| Operation | `SYSTEM` command | `USER` command (`<uid>` from config) |
|---|---|---|
| Start / Restart | `launchctl kickstart -k system/<name>` | `launchctl kickstart -k gui/<uid>/<name>` |
| Stop | `launchctl bootout system/<name>` | `launchctl bootout gui/<uid>/<name>` |
| Reload (SIGHUP) | `launchctl kill SIGHUP system/<name>` | `launchctl kill SIGHUP gui/<uid>/<name>` |
| Enable | `launchctl enable system/<name>` | `launchctl enable gui/<uid>/<name>` |
| Disable | `launchctl disable system/<name>` | `launchctl disable gui/<uid>/<name>` |
| Print status | `launchctl print system/<name>` | `launchctl print gui/<uid>/<name>` |
| Print disabled | `launchctl print-disabled system` | `launchctl print-disabled gui/<uid>` |
| List | `launchctl list` | `launchctl list` (scoped via `launchctl asuser <uid>` for SSH) |

---

## Output Parsing

### `launchctl print <domain>/<name>` Output

```
com.example.service = {
  active count = 1
  path = /Library/LaunchDaemons/com.example.service.plist
  type = LaunchDaemon
  state = running
  program = /usr/local/bin/example
  pid = 4321
  disabled = false
  ...
}
```

| Field in output | Maps to |
|---|---|
| `state = running` | `active_state = "active"` |
| `state = not running` (or absent) | `active_state = "inactive"` |
| `pid = <N>` with N != 0 | `main_pid = N` |
| `disabled = true` | `enabled_state = "disabled"` |
| `disabled = false` | `enabled_state = "enabled"` |
| Override plist present | `enabled_state = "masked"` (detected by a side probe) |
| `program = <path>` | `description = path` |
| `print` exits non-zero with "service not found" stderr | Return a synthetic `not-found` status |

### `launchctl list` Output

```
PID	Status	Label
1234	0	com.apple.cfprefsd.xpc.daemon
-	0	com.apple.bluetoothd
-	-	com.example.disabled.service
```

Tab-separated; the PID column is `-` for inactive services, the Status column is
`0` for healthy, the Label column is the service name.  `active_state = "active"`
iff PID is numeric.

### `launchctl print-disabled <domain>` Output

```
disabled services = {
  "com.example.disabled.service" => true
  "com.example.enabled.service" => false
}
```

Services mapped to `true` are disabled; mapped to `false` are enabled.

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `start_service` | `active_state == "active"` | already running |
| `stop_service` | `active_state == "inactive"` | already stopped |
| `restart_service` | never skips | — |
| `reload_service` | `active_state != "active"` → `FAILED` | must be active |
| `enable_service` | `enabled_state == "enabled"` | already enabled |
| `disable_service` | `enabled_state in {"disabled", "masked"}` | already disabled or masked |
| `mask_service` | `enabled_state == "masked"` | already masked |
| `unmask_service` | `enabled_state != "masked"` | not masked |
| `reload_daemon` | never skips (but is a no-op) | — |

All idempotency probes go through `get_service_status` which does NOT pass
`elevated=True`.

---

## Extensions beyond the contract

### Mask Fallback (two-step disable + override plist)

launchd has no dedicated mask primitive.  The contract's §Service-Name and Scope
Mapping row for macOS explicitly permits "Masking is emulated via disable +
permission-based refuse-to-run".  This spec's fallback is:

1. `launchctl disable <domain>/<name>` — prevents future loads.
2. Write `<system_override_plist_dir or user_override_plist_dir>/<name>.override.plist`
   containing:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
     <dict>
       <key>Label</key><string><name></string>
       <key>Disabled</key><true/>
     </dict>
   </plist>
   ```

Unmask reverses both steps.

When `mask_fallback_enabled=False` the verbs alias to `disable_service` /
`enable_service` respectively; this is the path that matches the Windows service
manager's mask-alias-disable semantics for cross-OS consistency.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosServiceQueryError` | `list_services` fails unexpectedly; `get_service_status`'s `print` command fails with a non-"service not found" stderr; USER scope with no target_uid |

All write failures surface as `MacosServiceActionResult(status=FAILED, …)`.  TCC /
SIP denials surface verbatim in `stderr`.

---

## Correctness Properties

### Property 1: Start Command Construction (SYSTEM)

*For any* `start_service(name, SYSTEM)` whose guard does not skip, the runner is
invoked with `["launchctl", "kickstart", "-k", f"system/{name}"]`.

**Validates: Requirements 1.1**

### Property 2: Start Command Construction (USER)

*For any* `start_service(name, USER)` with `target_uid=501`, the runner is invoked
with `["launchctl", "kickstart", "-k", f"gui/501/{name}"]`.

**Validates: Requirements 1.1, 12.2**

### Property 3: Start Idempotency

*For any* service already `active`, `start_service` returns `SKIPPED` without
dispatching the start command.

**Validates: Requirements 1.2**

### Property 4: Stop Command

*For any* `stop_service`, the runner is invoked with `bootout <domain>/<name>`
unless the service is already inactive.

**Validates: Requirements 2.1**

### Property 5: Stop Idempotency

*For any* service already `inactive`, `stop_service` returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 6: Stop TCC Surface

*For any* `bootout` failure with stderr containing `Operation not permitted`, the
result has `status=FAILED` and the stderr is preserved verbatim.

**Validates: Requirements 2.6**

### Property 7: Restart Unconditional

*For any* `restart_service`, the runner is invoked regardless of current state
(unless dry-run).

**Validates: Requirements 3.1, 3.5**

### Property 8: Reload Precondition

*For any* `reload_service(name, scope)` when the service is not active, the result
is `FAILED` with `message="service is not active; cannot reload"` and the `kill`
command is NOT invoked.

**Validates: Requirements 4.2**

### Property 9: Reload Happy Path

*For any* `reload_service` when active, the runner is invoked with
`["launchctl", "kill", "SIGHUP", f"{domain}/{name}"]`.

**Validates: Requirements 4.1**

### Property 10: Enable Idempotency

*For any* service whose `enabled_state == "enabled"`, `enable_service` returns
`SKIPPED`.

**Validates: Requirements 5.2**

### Property 11: Disable Idempotency

*For any* service whose `enabled_state in {"disabled", "masked"}`,
`disable_service` returns `SKIPPED`.

**Validates: Requirements 6.2**

### Property 12: Mask Fallback Two-Step Sequence

*For any* `mask_service` call with `mask_fallback_enabled=True` whose guard does
not skip, the runner is invoked with `disable` AND the file operator is invoked
with `write_text` to the override-plist path.

**Validates: Requirements 7.1**

### Property 13: Mask Alias When Fallback Disabled

*For any* `mask_service` call with `mask_fallback_enabled=False`, behaviour is
identical to `disable_service`; no file operator call occurs.

**Validates: Requirements 7.2**

### Property 14: Mask Idempotency

*For any* service whose `enabled_state == "masked"`, `mask_service` returns
`SKIPPED` without dispatching either step.

**Validates: Requirements 7.3**

### Property 15: Unmask Fallback Two-Step Sequence

*For any* `unmask_service` call with `mask_fallback_enabled=True` whose guard does
not skip, the file operator's `remove` is invoked AND the runner is invoked with
`enable`.

**Validates: Requirements 8.1**

### Property 16: Unmask Alias When Fallback Disabled

*For any* `unmask_service` call with `mask_fallback_enabled=False`, behaviour is
identical to `enable_service`.

**Validates: Requirements 8.2**

### Property 17: Status Query Command

*For any* `get_service_status(name, scope)`, the runner is invoked with
`["launchctl", "print", f"{domain}/{name}"]` without elevation.

**Validates: Requirements 9.1**

### Property 18: Status Not Found

*For any* `print` failure matching the "service not found" pattern,
`get_service_status` returns a status with `active_state="not-found"` and
`enabled_state="not-found"` without raising.

**Validates: Requirements 9.2**

### Property 19: Status Unexpected Error

*For any* `print` failure with a non-recognised stderr, a `MacosServiceQueryError`
is raised.

**Validates: Requirements 9.3**

### Property 20: List Services Parse

*For any* successful `list` + `print-disabled` output, `list_services` returns the
union by label with merged active/enabled state.

**Validates: Requirements 10.2**

### Property 21: List Services Error

*For any* failure from `list` or `print-disabled`, `MacosServiceQueryError` is
raised.

**Validates: Requirements 10.3**

### Property 22: Reload Daemon No-Op

*For any* `reload_daemon` call that is not dry-run, the runner is NOT invoked and
the result has `status=SUCCESS` with a descriptive `message`.

**Validates: Requirements 11.1**

### Property 23: Invalid Scope

*For any* call with a scope other than `SYSTEM` or `USER`, `ValueError` is raised.

**Validates: Requirements 12.4**

### Property 24: User Scope Without target_uid

*For any* `USER` scope call with `target_uid=None`, `ValueError` is raised before
the runner is invoked.

**Validates: Requirements 12.3**

### Property 25: Scope Echo On Result

*For any* write result, `MacosServiceActionResult.scope` equals the scope used.

**Validates: Requirements 12.5**

### Property 26: System Writes Elevate

*For any* write with `config.elevated=True` and `scope=SYSTEM`, the runner is
invoked with `elevated=True`.

**Validates: Requirements 13.1**

### Property 27: User Writes Elevate for Other Users

*For any* write with `config.elevated=True`, `scope=USER`, `target_uid !=
os.getuid()`, the runner is invoked with `elevated=True`.

**Validates: Requirements 13.2**

### Property 28: User Writes Do Not Elevate for Self

*For any* write with `config.elevated=True`, `scope=USER`, `target_uid ==
os.getuid()`, the runner is NOT invoked with `elevated=True`.

**Validates: Requirements 13.3**

### Property 29: Reads Do Not Elevate

*For any* read or idempotency-probe call, the runner is NEVER invoked with
`elevated=True`.

**Validates: Requirements 13.4**

### Property 30: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner (and file operator) are
NOT invoked and the result has `status=DRY_RUN`.

**Validates: Requirements 1.5, 2.5, 3.4, 4.5, 5.5, 6.5, 7.5, 8.5, 11.2**

### Property 31: Status Mapping

*For any* write command result from the runner, `status=SUCCESS` iff
`exit_code==0`; otherwise `status=FAILED` with captured fields.

**Validates: Requirements 1.3, 1.4, 2.3, 2.4, 3.2, 3.3, 4.3, 4.4, 5.3, 5.4, 6.3,
6.4**

---

## Module Layout

```
source/packages/ccai/sysman/macos/
├── __init__.py
└── servicemanagement.py       ← MacosServiceManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/macos/
    └── test_servicemanagement.py
```

---

## Usage Examples

### Local Execution (System Scope)

```python
import os
from ccai.sysman.macos.runners import (
    MacosLocalCommandRunner, MacosLocalRunnerConfig,
)
from ccai.sysman.macos.fileoperators import (
    MacosLocalFileOperator, MacosLocalFileOperatorConfig,
)
from ccai.sysman.macos.servicemanagement import (
    MacosServiceManager, MacosServiceManagerConfig, MacosServiceScope,
)

runner = MacosLocalCommandRunner(MacosLocalRunnerConfig())
fileop = MacosLocalFileOperator(MacosLocalFileOperatorConfig())
config = MacosServiceManagerConfig(elevated=True, dry_run=False,
                                   target_uid=os.getuid(),
                                   mask_fallback_enabled=True)
svc_mgr = MacosServiceManager(runner=runner, file_operator=fileop, config=config)

result = svc_mgr.restart_service(name="com.apple.coreaudiod",
                                 scope=MacosServiceScope.SYSTEM)
print(f"restart: {result.status.value}")
```

### User Scope via SSH with Aspects

```python
from ccai.sysman.macos.runners import (
    MacosSSHCommandRunner, MacosSSHRunnerConfig,
)
from ccai.sysman.macos.aspects import (
    RetryAspect, RetryAspectConfig,
)

runner = MacosSSHCommandRunner(MacosSSHRunnerConfig(host="mac-ci", username="ci"))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3)))

svc_mgr = MacosServiceManager(
    runner=runner,
    config=MacosServiceManagerConfig(elevated=True, target_uid=501,
                                     mask_fallback_enabled=False),
)
svc_mgr.enable_service(name="com.example.agent", scope=MacosServiceScope.USER)
```

### Dry-Run Preview

```python
config = MacosServiceManagerConfig(elevated=True, dry_run=True, target_uid=501)
svc_mgr = MacosServiceManager(runner=runner, config=config)
result = svc_mgr.mask_service(name="com.example.unwanted",
                              scope=MacosServiceScope.SYSTEM)
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** and **MockFileOperator** record invocation order and
   arguments per test.
2. One command-construction test per write verb per scope asserts the exact argv.
3. Idempotency tests cover `start`, `stop`, `enable`, `disable`, `mask`, `unmask`
   based on canned `print` outputs.
4. `reload_service` precondition is tested against an inactive probe.
5. `mask_service` in fallback mode is tested end-to-end: runner is called with
   `disable`, file operator is called with the override plist content.
6. `mask_service` in alias mode is tested to ensure no file operator invocation
   occurs.
7. `list_services` parsing is tested with a representative `list` + `print-disabled`
   merge.
8. `reload_daemon` is tested to NOT invoke the runner and to return a descriptive
   message.
9. Scope tests cover `USER` scope without `target_uid` raising `ValueError`.
10. Elevation tests assert `elevated=True` is passed for SYSTEM writes, for
    cross-user USER writes, and never for self-user USER writes or reads.
11. No test requires root access or a live `launchctl`.
