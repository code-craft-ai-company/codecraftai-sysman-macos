# Design

## Overview

The kernel parameters feature is implemented as a single module,
`automojo.sysman.macos.kernelparameters`, that exposes a
`MacosKernelParameterManager` class.  This spec implements the kernel-parameters
contract defined at
`automojo-sysman-common/specs/contracts/kernel-parameters/contract.md` for
macOS using `sysctl(8)` at runtime and the single flat `/etc/sysctl.conf` file
for persistence.

The coordinator instantiates `MacosKernelParameterManager` and calls its
methods to perform discrete maintenance actions against the macOS kernel
tunable surface.  All runtime command interaction is delegated to an
`ICommandRunner`; all persistent-file mutations are delegated to an
`IFileOperator`.  No `subprocess` calls or direct filesystem access occur
inside the module.

macOS differs from Linux in persistence shape: there is no `/etc/sysctl.d/`
drop-in directory — only the single `/etc/sysctl.conf` file — so the manager
performs read-modify-write on that file for every persistent operation.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator (Local or SSH)       │
│  - Configures aspects (optional)                            │
│  - Injects into MacosKernelParameterManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           MacosKernelParameterManager                       │
│  automojo.sysman.macos.kernelparameters                     │
│                                                             │
│  get_parameter()             set_parameter()                │
│  list_parameters()           reload_parameters()            │
│  remove_persisted_parameter()                               │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write /     │
│                      │         │   exists             │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
   ┌───────┴────────┐             ┌─────────┴────────┐
   ▼                ▼             ▼                  ▼
┌─────────────┐  ┌──────────────┐ ┌──────────────┐ ┌────────────┐
│LocalCommand │  │SSHCommand    │ │LocalFile     │ │SSHFile     │
│Runner       │  │Runner        │ │Operator      │ │Operator    │
└─────────────┘  └──────────────┘ └──────────────┘ └────────────┘
           │                                │
           └────────────┬───────────────────┘
                        │ optional
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosKernelParameterManager`

**Location:** `source/packages/automojo/sysman/macos/kernelparameters.py`

| Method | Signature | Description |
|---|---|---|
| `get_parameter` | `(name) → MacosParameterValue \| None` | Read runtime value via `sysctl -n` |
| `set_parameter` | `(name, value, persistent) → MacosKernelParameterActionResult` | Runtime / persistent / both |
| `list_parameters` | `() → dict[str, str]` | Parse `sysctl -a` |
| `reload_parameters` | `() → MacosKernelParameterActionResult` | `sysctl -p /etc/sysctl.conf` |
| `remove_persisted_parameter` | `(name) → MacosKernelParameterActionResult` | Rewrite `/etc/sysctl.conf` |

### `MacosKernelParameterManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `persist_dir` | `str` | `"/etc"` | Parent dir of `sysctl.conf` (used to locate the flat file) |

---

## Data Models

### Result Types

```python
class MacosKernelParameterActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class MacosParameterValue:
    name: str
    value: str
    source: str | None          # "runtime" or "/etc/sysctl.conf"

@dataclass
class MacosKernelParameterActionResult:
    status: MacosKernelParameterActionStatus
    name: str
    current_value: str | None
    applied_value: str | None
    persistent: bool
    requires_reboot: bool
    source: str | None          # "/etc/sysctl.conf" when persistent
    diff: str | None
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class MacosKernelParameterQueryError(Exception):
    """Raised when sysctl -a or an unexpected sysctl -n failure occurs."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

Both interfaces are injected at construction time.

---

## Parameter-Namespace Mapping

The contract's abstract parameter name maps directly to a BSD sysctl key.

| Accepted prefix | Example | Notes |
|---|---|---|
| `kern.` | `kern.maxfiles` | Some keys read-only under SIP. |
| `net.` | `net.inet.ip.forwarding` | Includes `net.inet.*`, `net.inet6.*`, `net.link.*`. |
| `hw.` | `hw.ncpu` | Mostly read-only. |
| `vm.` | `vm.swapusage` | Mostly read-only. |
| `debug.` | `debug.iokit` | Requires `kern.bootargs` adjustments for most writes. |
| `machdep.` | `machdep.cpu.brand_string` | Read-only. |
| `security.` | `security.mac.proc_enforce` | Often restricted under SIP. |

Anything else returns `FAILED` with `message="parameter_not_supported: <name>"`.

A parallel `_SIP_READ_ONLY` set inside the module lists parameters that return
`FAILED` with `message="sip_read_only: <name>"` on any runtime write attempt.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Runtime set | `sysctl -w <name>=<value>` |
| Reload persisted | `sysctl -p /etc/sysctl.conf` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Get one parameter | `sysctl -n <name>` |
| List all parameters | `sysctl -a` |

### File Operations (via `IFileOperator`)

| Operation | Path |
|---|---|
| Persist parameter | `/etc/sysctl.conf` (rewritten) |
| Remove persisted parameter | `/etc/sysctl.conf` (rewritten) |

---

## Output Parsing

### `sysctl -n` output

Single-line value.  Trailing whitespace stripped.  On "unknown oid" stderr the
parser returns `None` instead of raising.

### `sysctl -a` output

macOS emits both styles:
- `name: value`
- `name = value`

The parser tries each separator and uses the first match.  Multi-line values
(e.g., `kern.version`) are skipped silently.

### `/etc/sysctl.conf` content

Entries use the strict `name=value` form (no spaces).  `#`-prefixed lines are
comments.  The parser splits on the first `=` and strips both sides for
comparison.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `set_parameter(persistent=False)` | Runtime value equals desired | `sysctl -n <name>` |
| `set_parameter(persistent=True)` | `/etc/sysctl.conf` line equals desired AND runtime value equals desired | File read + `sysctl -n <name>` |
| `remove_persisted_parameter` | File absent or key absent from file | `IFileOperator.exists` + file read |

`reload_parameters` has no idempotency guard other than the file-absent
short-circuit.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosKernelParameterQueryError` | `sysctl -a` fails; `sysctl -n` fails with unexpected (non "unknown oid") error |

All write failures are surfaced as `MacosKernelParameterActionResult` with
`status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Get-Parameter Command Construction

*For any* parameter name, calling `get_parameter()` should construct `sysctl -n <name>` without elevation.

**Validates: Requirements 1.1**

### Property 2: Get-Parameter Missing Value Is None

*For any* parameter name for which `sysctl` emits "unknown oid", `get_parameter()` should return `None`.

**Validates: Requirements 1.3**

### Property 3: Get-Parameter Unexpected Failure Raises

*For any* `sysctl -n` exit code that is non-zero for a non "unknown oid" reason, `get_parameter()` should raise `MacosKernelParameterQueryError`.

**Validates: Requirements 1.4**

### Property 4: Set-Runtime Command Construction

*For any* allowlisted parameter name and value, calling `set_parameter(name, value, persistent=False)` should construct `sysctl -w <name>=<value>`.

**Validates: Requirements 2.2**

### Property 5: Set-Runtime Idempotency

*For any* parameter whose current runtime value already equals the requested value, `set_parameter(persistent=False)` should return `SKIPPED`.

**Validates: Requirements 2.1**

### Property 6: Set-Persistent Rewrites Single File

*For any* allowlisted parameter with `persistent=True`, the manager should rewrite `/etc/sysctl.conf` via the file operator preserving all other lines.

**Validates: Requirements 2.4, 7.2**

### Property 7: Set-Persistent Composition With Runtime Apply

*For any* `set_parameter(name, value, persistent=True)`, the manager should ALSO invoke `sysctl -w <name>=<value>` after the file write.

**Validates: Requirements 2.4**

### Property 8: Set-Persistent Idempotency

*For any* parameter whose `/etc/sysctl.conf` already contains the matching line AND whose runtime value matches, `set_parameter(persistent=True)` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 9: Allowlist Rejection

*For any* parameter name whose dotted prefix is not in the allowlist, `set_parameter()` should return `FAILED` with `message="parameter_not_supported: <name>"` without invoking the runner or file operator.

**Validates: Requirements 2.5, 6.2, 6.3**

### Property 10: SIP-Read-Only Rejection

*For any* SIP-read-only parameter attempted at runtime, `set_parameter(persistent=False)` should return `FAILED` with `message="sip_read_only: <name>"`.

**Validates: Requirements 6.4**

### Property 11: Partial-Failure Step Identification

*For any* `set_parameter(persistent=True)` whose persistent-write step fails, the result should be `FAILED` with a `message` identifying the step, and the runtime-apply step should NOT execute.

**Validates: Requirements 2.6**

### Property 12: List-Parameters Command Construction

*For any* call to `list_parameters()`, the manager should invoke `sysctl -a` without elevation.

**Validates: Requirements 3.1**

### Property 13: List-Parameters Dual-Separator Parsing

*For any* `sysctl -a` output containing both `name: value` and `name = value` lines, `list_parameters()` should correctly extract every name/value pair.

**Validates: Requirements 3.2**

### Property 14: List-Parameters Error Raises

*For any* non-zero exit from `sysctl -a`, `list_parameters()` should raise `MacosKernelParameterQueryError`.

**Validates: Requirements 3.3**

### Property 15: List-Parameters Skips Opaque Lines

*For any* stdout line not matching either separator, the parser should silently skip it.

**Validates: Requirements 3.4**

### Property 16: Reload Command Construction

*For any* call to `reload_parameters()`, the manager should invoke `sysctl -p /etc/sysctl.conf`.

**Validates: Requirements 4.1**

### Property 17: Reload Skipped When File Absent

*For any* call to `reload_parameters()` where `/etc/sysctl.conf` does not exist, the result should be `SKIPPED` with `message="no_persistent_file"`.

**Validates: Requirements 4.5**

### Property 18: Remove-Persisted Idempotency When File Absent

*For any* parameter whose `/etc/sysctl.conf` does not exist, `remove_persisted_parameter()` should return `SKIPPED`.

**Validates: Requirements 5.1**

### Property 19: Remove-Persisted Idempotency When Parameter Absent

*For any* parameter not present in the file, `remove_persisted_parameter()` should return `SKIPPED`.

**Validates: Requirements 5.2**

### Property 20: Remove-Persisted Preserves Other Lines

*For any* parameter removal, the written file content should equal the original content with only the target line removed.

**Validates: Requirements 5.3**

### Property 21: Persistent File Format

*For any* persisted parameter, the written line should be exactly `<name>=<value>` with no spaces around the `=`.

**Validates: Requirements 7.1**

### Property 22: New File Header Comment

*For any* newly created `/etc/sysctl.conf`, the first line should be `# managed by automojo-sysman-macos`.

**Validates: Requirements 7.3**

### Property 23: Requires-Reboot Flag

*For any* parameter declared reboot-only in the allowlist metadata, the result's `requires_reboot` should be `True`; for runtime-writable parameters it should be `False`.

**Validates: Requirements 2.8**

### Property 24: Dry-Run Short-Circuit for Writes

*For any* write operation with `dry_run=True`, the runner's write commands and the file operator's write methods should never be invoked, and the result status should be `DRY_RUN`.

**Validates: Requirements 2.7, 4.4, 5.6, 9.1**

### Property 25: Dry-Run Populates Diff For File Writes

*For any* persistent write in dry-run mode, the result's `diff` field should be a non-empty unified-diff string.

**Validates: Requirements 2.7, 9.3**

### Property 26: Dry-Run Still Reads

*For any* write operation in dry-run mode, the idempotency-guard reads should still execute.

**Validates: Requirements 9.4**

### Property 27: Elevation For Writes

*For any* write operation with `elevated=True` in the config, the runner / file operator should be invoked with `elevated=True`.

**Validates: Requirements 8.1**

### Property 28: No Elevation For Reads

*For any* read operation, the runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 8.2**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── kernelparameters.py         ← MacosKernelParameterManager, config,
                                   models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_kernelparameters.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.fileops import LocalFileOperator, LocalFileOperatorConfig
from automojo.sysman.macos.kernelparameters import (
    MacosKernelParameterManager,
    MacosKernelParameterManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = MacosKernelParameterManagerConfig(elevated=True, dry_run=False)
kp_mgr = MacosKernelParameterManager(runner=runner, file_operator=file_op, config=config)

# Runtime only
result = kp_mgr.set_parameter(name="net.inet.ip.forwarding", value="1", persistent=False)
print(f"Runtime: {result.status}")

# Persistent + runtime
result = kp_mgr.set_parameter(name="kern.maxfiles", value="65536", persistent=True)
print(f"Persistent: {result.status}")

# Reload
kp_mgr.reload_parameters()
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.fileops import SSHFileOperator, SSHFileOperatorConfig
from automojo.sysman.macos.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.kernelparameters import (
    MacosKernelParameterManager,
    MacosKernelParameterManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="mac-node-01", username="admin"))
file_op = SSHFileOperator(SSHFileOperatorConfig(host="mac-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

kp_mgr = MacosKernelParameterManager(
    runner=runner,
    file_operator=file_op,
    config=MacosKernelParameterManagerConfig(elevated=True),
)

kp_mgr.set_parameter(name="net.inet.tcp.mssdflt", value="1460", persistent=True)
kp_mgr.reload_parameters()
```

### Dry-Run Mode

```python
config = MacosKernelParameterManagerConfig(elevated=True, dry_run=True)
kp_mgr = MacosKernelParameterManager(runner=runner, file_operator=file_op, config=config)

result = kp_mgr.set_parameter(name="kern.maxfiles", value="65536", persistent=True)
assert result.status == MacosKernelParameterActionStatus.DRY_RUN
print(result.diff)         # unified diff of /etc/sysctl.conf
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` and `MockFileOperator` keyed on command
  arg-prefix and file path respectively.
- `sysctl -n <name>` mock drives runtime idempotency.
- `sysctl -a` mock drives list-parameters parser tests including both
  separator styles and opaque multi-line content.
- File read mocks drive `set_parameter(persistent=True)` and
  `remove_persisted_parameter` idempotency.
- `persist_dir` is overridden in tests to avoid touching the real
  `/etc/sysctl.conf`.
- Dry-run tests assert zero calls to mutating runner methods and zero calls
  to `file_operator.write_text`.
- Allowlist rejection tests assert zero runner and file-operator
  invocations.
- A dedicated test covers the SIP-read-only rejection path.
- A dedicated test covers the new-file-creation path with the header
  comment.
- No test requires root access or a live `sysctl`.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (allowlisted parameter names, values).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: kernel-parameters (macos), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | macOS implementation | Notes |
|---|---|---|
| `get_parameter` | `sysctl -n <name>` | `None` on unknown-oid. |
| `set_parameter(persistent=False)` | `sysctl -w <name>=<value>` | Idempotency via `sysctl -n`. |
| `set_parameter(persistent=True)` | File rewrite + `sysctl -w` | Both steps MUST succeed. |
| `list_parameters` | `sysctl -a` | Dual-separator parser. |
| `reload_parameters` | `sysctl -p /etc/sysctl.conf` | `SKIPPED` if file absent. |
| `remove_persisted_parameter` | File rewrite (line removal) | Preserves other lines. |

---

## Extensions beyond the contract

- **Parameter-prefix allowlist.**  The manager rejects parameters outside
  `kern./net./hw./vm./debug./machdep./security.` with `FAILED
  message="parameter_not_supported"`.  This is permitted by the contract's
  "Unsupported parameters" clause.
- **SIP-read-only set.**  A bundled set of parameter names that are
  read-only under System Integrity Protection.  Runtime writes against
  these return `FAILED` with `message="sip_read_only"` without invoking
  the runner — a fast-fail for misconfigurations.
- **Header comment on new files.**  A newly created `/etc/sysctl.conf`
  gets a `# managed by automojo-sysman-macos` header for auditing.
- **IFileOperator usage.**  macOS uses `IFileOperator` for persistent-file
  rewrites (contract-permitted extension).

## Out of Scope

- `launchd` plist-based persistence (not a sysctl concept).
- NVRAM / EFI-variable tunables (`nvram` command) — separate feature.
- `csrutil` / SIP configuration (handled by security-compliance).
- Per-cgroup or per-namespace tunables (macOS has no equivalent).
- Parameter discovery metadata (type, range, default).
- Mass-apply from a structured policy document (YAML / TOML).
