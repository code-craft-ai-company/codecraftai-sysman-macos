# Design

## Overview

The kdump-management feature on macOS is implemented as a single
module, `automojo.sysman.macos.kdumpmanagement`, that exposes a
`MacosKdumpManager` class. The class is a contract-conforming
**skeleton** because the cross-OS kdump-management contract is
**Linux-primary**: macOS has no kdump equivalent and several verbs
reduce to `FAILED` results that carry an explanatory advisory rather
than pretending to act.

The maintenance coordinator instantiates `MacosKdumpManager` with a
command runner and calls its methods. Three of the verbs (the
mutating ones) return `FAILED` unconditionally; the remaining four
operate over the macOS-native panic / diagnostic-report directory.

## Cross-OS Concept Mapping

The kdump-management contract is **Linux-primary**. macOS has no
pre-reserved-memory capture-kernel facility and no runtime-
configurable dump target. The table below documents how each cross-OS
verb maps onto the macOS native primitives.

| Cross-OS verb            | macOS native primitive                               | Native concept                              | Reduces to FAILED on macOS? |
|--------------------------|------------------------------------------------------|---------------------------------------------|-----------------------------|
| `enable_kdump`           | (none -- panic logging is always-on)                 | DiagnosticReports auto-emission             | YES                         |
| `disable_kdump`          | (none -- panic logging is always-on)                 | DiagnosticReports auto-emission             | YES                         |
| `configure_kdump_target` | (none -- target is fixed)                            | DiagnosticReports auto-emission             | YES                         |
| `get_kdump_status`       | (synthetic always-on snapshot)                       | DiagnosticReports auto-emission             | NO (returns synthetic)      |
| `list_recent_dumps`      | `ls -1t /Library/Logs/DiagnosticReports/`            | `*.panic` and `*.ips` files                 | NO                          |
| `clear_old_dumps`        | `find /Library/Logs/DiagnosticReports -mtime +N ...` | `*.panic` and `*.ips` files                 | NO                          |
| `get_dump_info`          | `cat /Library/Logs/DiagnosticReports/<id>` + `stat`  | panic-report dmesg metadata                 | NO                          |

Comparison with sister implementations:

| OS      | Configuration store              | Service unit               | Reboot required for target change? | Pre-reserved memory? |
|---------|----------------------------------|----------------------------|------------------------------------|----------------------|
| RHEL    | `/etc/kdump.conf`                | `kdump.service`            | No                                 | YES (`crashkernel=`) |
| Ubuntu  | `/etc/default/kdump-tools`       | `kdump-tools.service`      | No                                 | YES (`crashkernel=`) |
| macOS   | (none -- always-on)              | (none)                     | N/A                                | NO                   |
| Windows | `HKLM:\...\CrashControl`         | (none -- kernel-resident)  | YES                                | NO (uses pagefile)   |

## Why Not Pretend to Succeed?

The cross-OS contract permits two responses to a verb that does not
map onto the native facility:

1. Return `SKIPPED` and pretend the host is already in the desired
   state.
2. Return `FAILED` with `detail="not supported on <os>: <reason>"`.

This spec deliberately chooses option (2) for `enable_kdump`,
`disable_kdump`, and `configure_kdump_target`. Compliance reporting
should NOT silently believe a no-op succeeded; an operator looking at
the maintenance report needs to see "macOS has no kdump equivalent;
kernel-panic capture is always-on" so that the host's true state
(panic logging is on, but cannot be configured) is communicated
honestly.

The advisory text is the constant `_UNSUPPORTED_MESSAGE`:

```
macOS has no kdump equivalent; kernel-panic capture is always-on.
Use sysdiagnose for system-state captures.
```

It is shared across all three unsupported-write stubs.

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates LocalCommandRunner / SSHCommandRunner       |
|  - Configures aspects (optional)                            |
|  - Injects runner into MacosKdumpManager                    |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|            MacosKdumpManager                                |
|  automojo.sysman.macos.kdumpmanagement                      |
|                                                             |
|  enable_kdump()              -> always FAILED (synthetic)   |
|  disable_kdump()             -> always FAILED (synthetic)   |
|  configure_kdump_target()    -> always FAILED (synthetic)   |
|  clear_old_dumps()           -> find ... -exec rm           |
|  get_kdump_status()          -> synthetic always-on status  |
|  list_recent_dumps()         -> ls -1t                      |
|  get_dump_info()             -> cat + stat                  |
+----------------------+--------------------------------------+
                       | delegates to
                       v
+-------------------------------------------------------------+
|            ICommandRunner                                   |
|  run(args, elevated, stdin, ...) -> RunResult               |
+-------------------------------------------------------------+
```

The unsupported-write stubs do not invoke the runner at all; their
`FAILED` result is constructed in pure Python.

---

## Components

### `MacosKdumpManager`

**Location:**
`source/packages/automojo/sysman/macos/kdumpmanagement.py`

The primary public class. Constructed with an `ICommandRunner` and a
`MacosKdumpManagerConfig`.

| Method | Description |
|---|---|
| `enable_kdump()` -> `MacosKdumpActionResult` | Synthetic `FAILED` -- macOS unsupported. |
| `disable_kdump()` -> `MacosKdumpActionResult` | Synthetic `FAILED` -- macOS unsupported. |
| `configure_kdump_target(spec)` -> `MacosKdumpActionResult` | Synthetic `FAILED` -- macOS unsupported. |
| `clear_old_dumps(age_days)` -> `MacosKdumpActionResult` | `find /Library/Logs/DiagnosticReports -mtime +N ...`. |
| `get_kdump_status()` -> `MacosKdumpStatus` | Synthetic always-on snapshot. |
| `list_recent_dumps()` -> `list[MacosKdumpInfo]` | `ls -1t` over the report directory, filtered by extension. |
| `get_dump_info(dump_id)` -> `MacosKdumpInfo` | `cat` + `stat` on a single report file. |

### `MacosKdumpManagerConfig`

A dataclass holding runtime configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dry_run` | `bool` | `False` | Inherited. When `True`, no commands execute. |
| `elevated` | `bool` | `False` | Inherited. When `True`, write verbs elevate. |
| `report_dir` | `str` | `/Library/Logs/DiagnosticReports` | Search root for reads and `clear_old_dumps`. |

---

## Data Models

### `MacosKdumpActionStatus`

Aliased to the canonical `ActionStatus` enum from `featurebase.py`.

### `MacosKdumpTargetKind`

```python
class MacosKdumpTargetKind(Enum):
    LOCAL_PATH = "local_path"
```

Only `LOCAL_PATH` is meaningful on macOS. The contract's `SSH`, `NFS`,
and `RAW_DEVICE` values are intentionally absent because macOS panic
captures cannot be redirected without OS-level customisation that is
out of scope here.

### `MacosKdumpTargetSpec`

```python
@dataclass
class MacosKdumpTargetSpec:
    target_kind: MacosKdumpTargetKind
    path: Optional[str] = None
    ssh_user: Optional[str] = None     # reserved
    ssh_host: Optional[str] = None     # reserved
    nfs_export: Optional[str] = None   # reserved
    compression: Optional[str] = None  # reserved
```

The reserved fields exist for cross-OS contract conformance; they are
ignored on macOS and the manager always returns `FAILED` from
`configure_kdump_target` regardless of which fields the spec carries.

### `MacosKdumpActionResult`

```python
@dataclass
class MacosKdumpActionResult(FeatureActionResult):
    target: Optional[str] = None
    requires_reboot: bool = False    # always False on macOS
    message: Optional[str] = None
```

### `MacosKdumpStatus`

```python
@dataclass
class MacosKdumpStatus:
    enabled: bool
    running: bool
    target_kind: Optional[MacosKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None              # always None
    reserved_memory_mb: Optional[int] = None  # always None
    message: Optional[str] = None
```

### `MacosKdumpInfo`

```python
@dataclass
class MacosKdumpInfo:
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None
    panic_message: Optional[str] = None
```

### `MacosKdumpQueryError`

Raised by `list_recent_dumps()` and `get_dump_info()` when a read
fails for a reason other than the target being absent. Carries
`command`, `exit_code`, and `stderr`.

---

## Correctness Properties

### Property 1: Unsupported-Write Honesty

For any invocation of `enable_kdump`, `disable_kdump`, or
`configure_kdump_target`, the result has status `FAILED`, the runner
is NOT invoked, and `message` carries the macOS advisory.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

### Property 2: Synthetic Status Determinism

For any invocation of `get_kdump_status()`, the result has
`enabled=True`, `running=True`,
`target_kind=MacosKdumpTargetKind.LOCAL_PATH`, and `target_path`
matches `config.report_dir`. The runner is NOT invoked.

**Validates: Requirements 2.1, 2.2**

### Property 3: List-Empty-On-Missing

For any host whose `<report_dir>` does not exist,
`list_recent_dumps()` returns an empty list rather than raising.

**Validates: Requirements 3.4**

### Property 4: Extension Filter

For any listing, `list_recent_dumps()` returns only entries whose
filename ends in `.panic` or `.ips`; everything else is filtered out
in pure Python.

**Validates: Requirements 3.2, 3.3**

### Property 5: Dry-Run Safety

For any invocation of `clear_old_dumps` with `config.dry_run=True`,
the runner is NOT invoked and the result has status `DRY_RUN` with
the would-be argv on `command`.

**Validates: Requirements 7.1, 7.2**

### Property 6: Elevation Consistency

For `clear_old_dumps` with `config.elevated=True`, the runner is
invoked with `elevated=True`. For every read verb, the runner is
invoked with `elevated=False` regardless of the config.

**Validates: Requirements 6.1, 6.2, 6.3**

### Property 7: Failed-Command Surface

For any `clear_old_dumps` invocation whose `find` returns non-zero,
the result has status `FAILED`, with `exit_code`, `stderr`, and
`command` populated.

**Validates: Requirements 4.x**

### Property 8: Age Validation

For any `clear_old_dumps(age_days=N)` with `N < 0`, the runner is NOT
invoked and the result has status `FAILED` with
`message="age_days must be >= 0"`.

**Validates: Requirements 4.2**

### Property 9: Typed Read Errors

For any `list_recent_dumps()` or `get_dump_info()` call whose
underlying read fails for a reason other than the target being
absent, the manager raises `MacosKdumpQueryError`.

**Validates: Requirements 9.1, 9.2**

### Property 10: Reads Never Elevate

For any read verb invocation, every runner call is invoked with
`elevated=False`, regardless of `config.elevated`.

**Validates: Requirements 6.3**

---

## Interfaces

### `ICommandRunner`

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: Optional[str] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult`

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

---

## Command Mappings

| Operation                       | Command                                                                                |
|---------------------------------|----------------------------------------------------------------------------------------|
| List dump directory             | `ls -1t /Library/Logs/DiagnosticReports`                                               |
| Find / clear old dumps          | `find /Library/Logs/DiagnosticReports -maxdepth 1 -type f \( -name '*.panic' -o -name '*.ips' \) -mtime +N -exec rm -f {} \;` |
| Read panic report               | `cat /Library/Logs/DiagnosticReports/<id>`                                             |
| Read report metadata            | `stat -f %Sm;%z /Library/Logs/DiagnosticReports/<id>`                                  |

The `enable_kdump`, `disable_kdump`, `configure_kdump_target`, and
`get_kdump_status` verbs do NOT issue any command -- they are
synthetic.

---

## Error Handling

### Typed Exceptions

| Exception | Raised when |
|-----------|-------------|
| `MacosKdumpQueryError` | A read fails for a reason other than the target being absent. |

All other failures surface as
`MacosKdumpActionResult(status=FAILED)`.

### macOS-Specific Notes

* `clear_old_dumps` against `/Library/Logs/DiagnosticReports` will
  typically require `sudo` even though the entries are technically
  user-readable. Set `config.elevated=True` for production runs.
* The `stat` invocation uses BSD `stat` flags (`-f %Sm;%z`), not GNU
  flags. Running this manager against a Linux host (e.g. via SSH)
  will produce empty mtime / size fields gracefully.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
+-- __init__.py
+-- kdumpmanagement.py            <- MacosKdumpManager + models

source/testroots/testbase/
+-- automojo/tests/sysman/macos/
    +-- test_kdumpmanagement.py   <- unit tests using a MockCommandRunner
```

---

## Testing Strategy

### Unit Tests

Every test uses a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values keyed by argv tuple. Tests
verify:

* the exact argv passed to the runner,
* the elevation flag on every call,
* the result `status`, `command`, `exit_code`, `stderr`, and
  `message` fields,
* the synthetic `FAILED` advisory text on the unsupported-write
  stubs,
* the synthetic always-on `MacosKdumpStatus` shape.

### Test Coverage

Required tests:

* `test_enable_kdump_returns_failed_with_macos_message`
* `test_disable_kdump_returns_failed_with_macos_message`
* `test_configure_kdump_target_returns_failed_with_macos_message`
* `test_get_kdump_status_returns_synthetic_status`
* `test_list_recent_dumps_parses_directory_listing`
* `test_list_recent_dumps_empty_when_directory_missing`
* `test_clear_old_dumps_invokes_find_with_mtime`
* `test_clear_old_dumps_dry_run_short_circuits`
* `test_get_dump_info_parses_panic_output`
* `test_writes_elevate`
* `test_reads_never_elevate`
* `test_failed_command_surfaces_stderr`
* `test_get_dump_info_raises_on_missing_file`
* `test_clear_old_dumps_rejects_negative_age`

Total: 14 tests.

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.macos.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.macos.kdumpmanagement import (
    MacosKdumpManager, MacosKdumpManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
manager = MacosKdumpManager(
    runner=runner,
    config=MacosKdumpManagerConfig(elevated=True),
)

# Enumerate recent panics.
for info in manager.list_recent_dumps():
    print(f"{info.id} -> {info.path}")

# Synthetic status -- never executes a command.
status = manager.get_kdump_status()
print(f"enabled={status.enabled} message={status.message}")

# Unsupported writes are honest about the macOS native model.
result = manager.enable_kdump()
assert result.status.value == "failed"
print(result.message)
```

### Dry-Run Mode

```python
config = MacosKdumpManagerConfig(elevated=True, dry_run=True)
manager = MacosKdumpManager(runner=runner, config=config)
result = manager.clear_old_dumps(age_days=30)
assert result.status.value == "dry_run"
print(result.command)  # ['find', '/Library/Logs/DiagnosticReports', ...]
```
