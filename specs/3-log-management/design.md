# Design

## Overview

The log management feature is implemented as a single module,
`automojo.sysman.macos.logmanagement`, that exposes a `MacosLogManager` class.  This
spec implements the log-management contract defined at
`automojo-sysman-common/specs/contracts/log-management/contract.md` for macOS
using Apple's unified-logging CLI (`log show`, `log stream`, `log config`),
BSD-style rotation via `newsyslog` with drop-ins under `/etc/newsyslog.d/`, and
plain-file truncation via the `IFileOperator`.

All system commands go through the `ICommandRunner` interface; all rotation drop-in
file I/O goes through the `IFileOperator` interface — this dual-abstraction
dependency is an extension over the contract, motivated by the need to manage both
the unified log and BSD syslog files on the same host.  No `subprocess`, no direct
`open`, and no direct `pathlib` calls are made inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  When using
SSH, the coordinator can track reliability metrics and apply execution aspects
(persistence, retry, timeout) without any changes to the `MacosLogManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into MacosLogManager                        │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              MacosLogManager                                │
│  automojo.sysman.macos.logmanagement                        │
│                                                             │
│  get_log()              tail_log()                          │
│  list_logs()            rotate_log()                        │
│  truncate_log()         configure_rotation()                │
│  remove_rotation_config() get_rotation_config()             │
│  query_disk_usage()                                         │
└────────────┬────────────────────────────┬───────────────────┘
             │ delegates to               │ delegates to
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run() / stream()   │         │   read_text / write  │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           └────────────────┬───────────────┘
                            ▼
          ┌──────────────────────────────────┐
          │   LocalCommandRunner or          │
          │   SSHCommandRunner               │
          └─────────────┬────────────────────┘
                        │ optional
                        ▼
          ┌──────────────────────────────────┐
          │   PersistenceAspect /            │
          │   RetryAspect / TimeoutAspect    │
          └──────────────────────────────────┘
```

---

## Components

### `MacosLogManager`

**Location:** `source/packages/automojo/sysman/macos/logmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `get_log` | `(source, lines=None, since=None, until=None, severity=None, pattern=None) → MacosLogEntries` | Retrieve recent entries |
| `tail_log` | `(source, follow=True) → Iterator[MacosLogEntry]` | Stream entries |
| `list_logs` | `() → list[MacosLogInfo]` | Enumerate subsystems + `/var/log/*` |
| `rotate_log` | `(source) → MacosLogActionResult` | Force rotation |
| `truncate_log` | `(source) → MacosLogActionResult` | Zero out a plain-file log (subsystems unsupported) |
| `configure_rotation` | `(source, policy) → MacosLogActionResult` | Write drop-in |
| `remove_rotation_config` | `(source) → MacosLogActionResult` | Delete drop-in |
| `get_rotation_config` | `(source) → MacosRotationPolicy \| None` | Parse drop-in |
| `query_disk_usage` | `(source) → MacosLogDiskUsage` | Compute disk usage |

### `MacosLogManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit (see §rotate_log exception) |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

```python
class MacosLogActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class MacosLogEntry:
    timestamp: datetime | None
    severity: str | None
    source: str | None
    message: str

@dataclass
class MacosLogEntries:
    source: str
    entries: list[MacosLogEntry]
    raw: list[str]
    truncated: bool

@dataclass
class MacosLogInfo:
    source: str
    path: str | None
    size_bytes: int | None
    rotating: bool

@dataclass
class MacosLogActionResult:
    status: MacosLogActionStatus
    source: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class MacosRotationPolicy:
    source: str
    keep_count: int
    frequency: str              # "daily" / "weekly" / "size:100M"
    compress: bool
    max_size_bytes: int | None
    post_rotate: str | None

@dataclass
class MacosLogDiskUsage:
    source: str
    size_human: str
    size_bytes: int

class MacosLogQueryError(Exception):
    """Raised when a read fails for a reason other than source-missing."""
```

---

## Interfaces

### `ICommandRunner` and `IFileOperator`

Both are imported from `automojo-interfaces` and used exactly as defined in the
contract.  The manager holds one instance of each and delegates all I/O to them.

---

## Source Mapping

| Shape | Example | Routing |
|---|---|---|
| Unified-log subsystem | `com.apple.ssh`, `com.apple.network` | `log show --predicate 'subsystem == "<name>"'` |
| Unified-log predicate | `predicate:process == "sshd"` | `log show --predicate '<expr>'` |
| Filesystem path | `/var/log/system.log`, `/var/log/install.log` | `tail` / `cat` / `stat` |

The routing helper `_classify_source(source)` returns:

1. If `source.startswith("/")` → `"file"`.
2. If `source.startswith("predicate:")` → `"predicate"`.
3. Otherwise → `"subsystem"` (default).

---

## Command Mappings

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| `get_log` — subsystem | `log show --predicate 'subsystem == "<name>"' --style json [--last <duration>] [--info] [--debug]` |
| `get_log` — predicate | `log show --predicate '<expr>' --style json [--last <duration>]` |
| `get_log` — file | `tail -n <N> <path>` or `cat <path>` |
| `tail_log` — subsystem | `log stream --predicate 'subsystem == "<name>"' --style json` |
| `tail_log` — file | `tail -F <path>` |
| `list_logs` | `log config --list --no-pager`, `ls -1 /var/log`, `stat -f%z <path>` |
| `get_rotation_config` | (file read via `IFileOperator.read_text`) |
| `query_disk_usage` — subsystem | `du -sh /var/db/diagnostics /var/db/uuidtext` |
| `query_disk_usage` — file | `stat -f%z <path>` |

### Write Commands (elevation-aware)

| Operation | Command |
|---|---|
| `rotate_log` — file | `newsyslog -nvvF -R force-rotate` |
| `rotate_log` — subsystem | `launchctl kill SIGHUP <domain>/<label>` |
| `rotate_log` — dry-run | `newsyslog -nvv` |
| `truncate_log` — file | `IFileOperator.write_text(path, "", elevated=True)` |
| `truncate_log` — subsystem | (returns FAILED — unsupported, no command) |
| `configure_rotation` | `IFileOperator.write_text("/etc/newsyslog.d/<basename>.conf", rendered, elevated=True)` |
| `remove_rotation_config` | `IFileOperator.delete("/etc/newsyslog.d/<basename>.conf", elevated=True)` |

---

## `log show --style json` Output Parsing

Unified-logging JSON output is a top-level array of objects:

```json
[{"timestamp":"2024-04-05 12:34:38.123456+0000",
  "messageType":"Default",
  "subsystem":"com.apple.ssh",
  "processImagePath":"/usr/sbin/sshd",
  "eventMessage":"Accepted publickey for admin from 10.0.0.1"},
 ...]
```

Parse rules:

1. `timestamp` — `datetime.strptime(obj["timestamp"], "%Y-%m-%d %H:%M:%S.%f%z")`.
2. `severity` — `_MESSAGE_TYPE_MAP[obj["messageType"]]` where the map is
   `{"Default":"info", "Info":"info", "Debug":"debug", "Error":"err",
    "Fault":"crit"}`.
3. `source` — `obj["subsystem"]` if present, else basename of `processImagePath`.
4. `message` — `obj["eventMessage"]` or empty string.

Missing keys are tolerated (treated as `None`/empty).  A parse failure on the
top-level JSON causes `MacosLogQueryError`.

---

## `du -sh` Output Parsing

Example: `1.2G\t/var/db/diagnostics`.  Parser splits on tab, applies the same
suffix table as Ubuntu (`B`/`K`/`M`/`G`/`T`) and sums across the two paths.

---

## `MacosRotationPolicy` Rendering and Parsing

### Rendered shape (newsyslog format)

```
# path            owner:group   mode   count  size    when   flags  [/pid_file]  [sig_num]
/var/log/<name>   root:wheel    640    <keep>  *       $<freq>  JN
```

Where `$<freq>` encodes cadence as newsyslog tokens (`$D0`=daily at midnight,
`$W0D0`=weekly, `*`=size-triggered when `max_size_bytes` is set).

### Parse rules

Tokenise by whitespace; the first column is the path, columns 2–7 are owner, mode,
count, size, when, flags.  Missing columns yield defaults; unparseable lines raise
`MacosLogQueryError`.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `configure_rotation` | Rendered content matches existing | `read_text` → string compare |
| `remove_rotation_config` | Drop-in absent | `IFileOperator.exists` |
| `rotate_log` | None — operational action | — |
| `truncate_log` | None — operational action | — |

---

## `rotate_log` Dry-Run Exception

Per the contract, `rotate_log` MAY execute a native simulation command even in
dry-run mode.  This implementation invokes `newsyslog -nvv` so that the
coordinator receives the same verbose output the operator would see when running
the command manually.  The returned `MacosLogActionResult` carries
`status=DRY_RUN` and the verbose stdout in the `stdout` field.

All other write verbs follow the strict contract rule: no runner invocation in
dry-run mode.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosLogQueryError` | `get_log`, `tail_log` (first iter), `list_logs`, `get_rotation_config` (unparseable), or `query_disk_usage` sees a failure that is NOT "source unknown" |

All other failures from write operations are surfaced as
`MacosLogActionResult(status=FAILED)` with exit code and stderr populated.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of the system.*

### Property 1: `get_log` Command Construction for Subsystems
*For any* subsystem name and filter set, `get_log` should construct a `log show`
command with `--predicate 'subsystem == "<name>"'`, `--style json`, plus flag
equivalents for each supplied filter.
**Validates: Requirements 1.1**

### Property 2: `get_log` Command Construction for Files
*For any* file path with `lines=N`, `get_log` should construct `tail -n N <path>`.
**Validates: Requirements 1.2**

### Property 3: `get_log` JSON Parsing
*For any* well-formed `log show --style json` stdout, `get_log` should produce a
`MacosLogEntries` whose `entries` list contains one `MacosLogEntry` per array
element, in chronological order.
**Validates: Requirements 1.3**

### Property 4: `get_log` Truncation Flag
*For any* query where the resulting entry count equals `lines`, the result's
`truncated` field should be `True`.
**Validates: Requirements 1.4**

### Property 5: `get_log` Unknown Source
*For any* source that does not exist, `get_log` should return an empty
`MacosLogEntries` without raising.
**Validates: Requirements 1.5**

### Property 6: `get_log` Error Raises
*For any* failure other than "source unknown", `get_log` should raise
`MacosLogQueryError`.
**Validates: Requirements 1.6**

### Property 7: `tail_log` Streaming Construction
*For any* subsystem and `follow=True`, `tail_log` should invoke
`log stream --predicate 'subsystem == "<name>"' --style json` via the streaming
API.
**Validates: Requirements 2.1**

### Property 8: `tail_log` File Construction
*For any* file path and `follow=True`, `tail_log` should invoke `tail -F <path>`.
**Validates: Requirements 2.2**

### Property 9: `tail_log` Non-Follow Termination
*For any* non-follow call, the iterator should yield the backlog and terminate.
**Validates: Requirements 2.3**

### Property 10: `list_logs` Composition
*For any* call, `list_logs` should invoke `log config --list` and `ls -1 /var/log`
and compose the results.
**Validates: Requirements 3.1**

### Property 11: `list_logs` Rotating Flag
*For any* source, `MacosLogInfo.rotating` should be `True` iff a newsyslog stanza
exists for that basename in `/etc/newsyslog.d/` or `/etc/newsyslog.conf`.
**Validates: Requirements 3.2, 3.3**

### Property 12: `list_logs` Error Raises
*For any* failure in either sub-command, `list_logs` should raise
`MacosLogQueryError`.
**Validates: Requirements 3.4**

### Property 13: `rotate_log` File Path
*For any* file source, `rotate_log` should invoke `newsyslog -nvvF -R force-rotate`.
**Validates: Requirements 4.1**

### Property 14: `rotate_log` Subsystem Path
*For any* subsystem source, `rotate_log` should invoke
`launchctl kill SIGHUP <domain>/<label>` where `<label>` comes from
`_subsystem_to_launch_label`.
**Validates: Requirements 4.2**

### Property 15: `rotate_log` Dry-Run Simulation
*For any* call with `dry_run=True`, `rotate_log` should invoke `newsyslog -nvv`
and return `status=DRY_RUN` with output in `stdout`.
**Validates: Requirements 4.5, 11.2**

### Property 16: `truncate_log` File Path
*For any* filesystem source, `truncate_log` should call
`IFileOperator.write_text(path, "", elevated=<cfg>)`.
**Validates: Requirements 5.1**

### Property 17: `truncate_log` Subsystem Rejection
*For any* subsystem source, `truncate_log` should return `status=FAILED` with a
descriptive `detail` and make no runner/file-operator call.
**Validates: Requirements 5.2**

### Property 18: `truncate_log` Dry-Run Short-Circuit
*For any* call with `dry_run=True`, `truncate_log` should not invoke the file
operator and return `status=DRY_RUN`.
**Validates: Requirements 5.5, 11.1**

### Property 19: `configure_rotation` Idempotency
*For any* source where the rendered policy matches existing content,
`configure_rotation` should return `status=SKIPPED`.
**Validates: Requirements 6.2**

### Property 20: `configure_rotation` Write
*For any* source where content differs, `configure_rotation` should call
`write_text(..., elevated=<cfg>)` and return `status=SUCCESS`.
**Validates: Requirements 6.3**

### Property 21: `configure_rotation` Dry-Run
*For any* call with `dry_run=True`, `configure_rotation` should compute the diff,
populate `detail`, and return `status=DRY_RUN`.
**Validates: Requirements 6.5, 11.1**

### Property 22: `remove_rotation_config` Idempotency
*For any* source with no existing drop-in, `remove_rotation_config` should return
`status=SKIPPED`.
**Validates: Requirements 7.1**

### Property 23: `remove_rotation_config` Delete
*For any* source with an existing drop-in, `remove_rotation_config` should call
`delete(..., elevated=<cfg>)` and return `status=SUCCESS`.
**Validates: Requirements 7.2**

### Property 24: `get_rotation_config` Absent
*For any* source with no drop-in, `get_rotation_config` should return `None`.
**Validates: Requirements 8.1**

### Property 25: `get_rotation_config` Parse
*For any* source with a recognised drop-in, `get_rotation_config` should parse the
stanza into a `MacosRotationPolicy`.
**Validates: Requirements 8.2**

### Property 26: `get_rotation_config` Unparseable
*For any* drop-in whose content cannot be parsed, `get_rotation_config` should
raise `MacosLogQueryError`.
**Validates: Requirements 8.3**

### Property 27: `query_disk_usage` Unified Log
*For any* subsystem, `query_disk_usage` should sum `du -sh` outputs for
`/var/db/diagnostics` and `/var/db/uuidtext` into a `MacosLogDiskUsage`.
**Validates: Requirements 9.1**

### Property 28: Elevation for Writes
*For any* write operation with `elevated=True` in the config, the runner or file
operator should be invoked with `elevated=True`.
**Validates: Requirements 10.1**

### Property 29: No Elevation for Reads
*For any* read operation, the runner and file operator should never be invoked
with `elevated=True`.
**Validates: Requirements 10.2**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── logmanagement.py          ← MacosLogManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_logmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.fileoperators import LocalFileOperator
from automojo.sysman.macos.logmanagement import (
    MacosLogManager,
    MacosLogManagerConfig,
    MacosRotationPolicy,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

log_mgr = MacosLogManager(
    runner=runner,
    file_operator=file_operator,
    config=MacosLogManagerConfig(elevated=True, dry_run=False),
)

entries = log_mgr.get_log(source="com.apple.ssh", lines=100, severity="err")
for entry in entries.entries:
    print(f"{entry.timestamp} {entry.severity} {entry.message}")

policy = MacosRotationPolicy(
    source="myapp",
    keep_count=7,
    frequency="daily",
    compress=True,
    max_size_bytes=None,
    post_rotate=None,
)
result = log_mgr.configure_rotation(source="myapp", policy=policy)
print(f"Configure rotation: {result.status}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.aspects import (
    PersistenceAspect, PersistenceAspectConfig,
    RetryAspect, RetryAspectConfig,
    TimeoutAspect, TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.fileoperators import SSHFileOperator
from automojo.sysman.macos.logmanagement import MacosLogManager, MacosLogManagerConfig

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
log_mgr = MacosLogManager(
    runner=runner,
    file_operator=file_operator,
    config=MacosLogManagerConfig(elevated=True),
)

result = log_mgr.rotate_log(source="/var/log/system.log")
print(f"Rotate: {result.status}")
```

### Dry-Run Mode

```python
cfg = MacosLogManagerConfig(elevated=True, dry_run=True)
log_mgr = MacosLogManager(runner=runner, file_operator=file_operator, config=cfg)

rot_result = log_mgr.rotate_log(source="/var/log/system.log")
assert rot_result.status == MacosLogActionStatus.DRY_RUN
print(rot_result.stdout)  # newsyslog -nvv output

trunc_result = log_mgr.truncate_log(source="/var/log/system.log")
assert trunc_result.status == MacosLogActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned `log show
   --style json` arrays for `get_log`; asserts the runner received the correct
   predicate and flag set for each filter combination.
2. `MockFileOperator` backed by an in-memory `dict[str, str]` drives
   `configure_rotation` idempotency tests.
3. `rotate_log` dry-run test asserts the runner was invoked exactly once with
   `["newsyslog", "-nvv"]` and the result is `DRY_RUN`.
4. `list_logs` parser test uses representative `log config --list` and
   `ls -1 /var/log` outputs to verify the composed `MacosLogInfo` list.
5. `query_disk_usage` unit tests cover the `du -sh` output for unified-log paths
   plus the `stat -f%z` path for files.
6. `MacosRotationPolicy` render/parse round-trip test confirms that rendering
   then parsing yields the original policy.
7. Dry-run matrix test iterates every write verb and asserts no mutating runner
   or file-operator call occurs (except the documented `rotate_log` simulation
   path).
8. Elevation matrix test asserts `elevated=True` flows to the runner/file
   operator for writes and is absent for reads.
9. No test requires root access or a live macOS host.

---

## Scope Mapping

| Abstract | macOS |
|---|---|
| Log source (subsystem) | Unified-logging subsystem string like `com.apple.ssh` |
| Log source (predicate) | `predicate:<expr>` prefix |
| Log source (file) | Absolute path, e.g., `/var/log/system.log` |
| Rotation drop-in store | `/etc/newsyslog.d/<basename>.conf` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for rotation drop-in I/O
  in addition to `ICommandRunner`.
- **Unified-log subsystem truncation rejection.**  Returns `FAILED` with a
  descriptive message rather than performing `log erase --all`, which is
  destructive beyond the single-subsystem scope the caller requested.
- **`rotate_log` dry-run simulation.**  Uses `newsyslog -nvv` to surface useful
  verbose output rather than returning a silent `DRY_RUN`.
