# Design

## Overview

The filesystem management feature is implemented as a single module,
`automojo.sysman.macos.filesystemmanagement`, that exposes a
`MacosFilesystemManager` class.  This spec implements the filesystem-management
contract defined at
`automojo-sysman-common/specs/contracts/filesystem-management/contract.md` for
macOS using the BSD toolchain (`mount`, `umount`, `stat -f`, `df`, `mkdir`, `rm`,
`ln`, `chmod`, `chown`), macOS's `diskutil` for lazy unmounts, and an
`IFileOperator` for transactional edits to `/etc/fstab`.

All read paths used for idempotency guards go through `mount`, BSD `stat -f`, or the
file operator reading `/etc/fstab`; output is parsed in pure Python into
`MacosMountInfo` / `MacosFileInfo` / `MacosFstabEntry` records.  All system
interaction is delegated to a command runner implementing the `ICommandRunner`
interface and to an `IFileOperator`.  No direct Python `open()` / `subprocess` calls
are made inside the module.

macOS's `/etc/fstab` is *advisory* — the boot-time mount set is controlled by Disk
Arbitration, not by fstab.  The manager therefore writes fstab only for mounts that
should persist across reboots and documents the advisory nature in the
`MacosFilesystemActionResult.detail` on every successful fstab write.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into MacosFilesystemManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosFilesystemManager                           │
│  automojo.sysman.macos.filesystemmanagement                 │
│                                                             │
│  mount()              unmount()                             │
│  add_fstab_entry()    remove_fstab_entry()                  │
│  get_mounts()                                               │
│  create_directory()   remove_directory()                    │
│  create_symlink()     remove_symlink()                      │
│  chown()              chmod()                               │
│  stat()               query_disk_usage()                    │
└─────────┬───────────────────────────────┬───────────────────┘
          │ ICommandRunner                │ IFileOperator
          ▼                               ▼
┌────────────────────────┐    ┌────────────────────────────┐
│ LocalCommandRunner     │    │ LocalFileOperator          │
│ SSHCommandRunner       │    │ SSHFileOperator (SFTP)     │
└────────────────────────┘    └────────────────────────────┘
          │                               │
          └────────────────┬──────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosFilesystemManager`

**Location:** `source/packages/automojo/sysman/macos/filesystemmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `mount` | `(device, mountpoint, fstype, options) → MacosFilesystemActionResult` | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| `unmount` | `(mountpoint, lazy) → MacosFilesystemActionResult` | `umount` or `diskutil unmount force` |
| `add_fstab_entry` | `(entry) → MacosFilesystemActionResult` | read-modify-write `/etc/fstab` via `IFileOperator` |
| `remove_fstab_entry` | `(device_or_mountpoint) → MacosFilesystemActionResult` | read-modify-write `/etc/fstab` |
| `get_mounts` | `() → list[MacosMountInfo]` | BSD `mount` output parser |
| `create_directory` | `(path, mode, owner, group) → MacosFilesystemActionResult` | `mkdir -p` [+ `chmod` + `chown`] |
| `remove_directory` | `(path, recursive) → MacosFilesystemActionResult` | `rmdir` or `rm -rf` |
| `create_symlink` | `(link, target) → MacosFilesystemActionResult` | `ln -s` or `ln -snf` |
| `remove_symlink` | `(link) → MacosFilesystemActionResult` | `rm <link>` after `stat -f` guard |
| `chown` | `(path, user, group) → MacosFilesystemActionResult` | `chown <user>:<group>` |
| `chmod` | `(path, mode) → MacosFilesystemActionResult` | `chmod <octal>` |
| `stat` | `(path) → MacosFileInfo \| None` | `stat -f '%HT\|%Lp\|%Su\|%Sg\|%z\|%N'` |
| `query_disk_usage` | `(path) → MacosDiskUsageInfo` | `df -b <path>` |

### `MacosFilesystemManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands / file writes are dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `fstab_path` | `str` | `/etc/fstab` | Overridable for testing |

---

## Data Models

### Result Types

```python
class MacosFilesystemActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosFilesystemActionResult:
    status: MacosFilesystemActionStatus
    path: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Mount / Fstab / File / DiskUsage Types

```python
@dataclass
class MacosMountInfo:
    device: str
    mountpoint: str
    fstype: str
    options: str

@dataclass
class MacosFstabEntry:
    device: str
    mountpoint: str
    fstype: str
    options: str                        # default "defaults"
    dump: int                           # default 0
    passno: int                         # default 0

@dataclass
class MacosFileInfo:
    path: str
    path_type: str                      # e.g. "Regular File", "Directory", "Symbolic Link"
    mode: str
    owner: str
    group: str
    size_bytes: int

@dataclass
class MacosDiskUsageInfo:
    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int
```

### Exceptions

```python
class MacosFilesystemQueryError(Exception):
    """Raised when a mount / stat / df query fails unexpectedly."""
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
    ) -> RunResult: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(
        self, path: str, content: str, elevated: bool = False, atomic: bool = True,
    ) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

`add_fstab_entry` and `remove_fstab_entry` use `read_text` and
`write_text(atomic=True)` so the rewrite is a write-temp-then-rename sequence.

---

## Mount-Table Mapping

macOS retains `/etc/fstab` support but it is *advisory*.  On modern macOS the
primary mount surface is Disk Arbitration, driven by `diskutil`.  The manager
writes only to `/etc/fstab` because that is the only persistent surface amenable
to programmatic read-modify-write.  Every successful fstab write records a
`detail` string:

> "macOS fstab is advisory; Disk Arbitration may override this entry for boot-time mounts."

Callers that require Disk-Arbitration-controlled persistence are directed to a
future extension that writes a launchd plist to `/Library/LaunchDaemons/` and
invokes `mount` at load time — this extension is documented in the Extensions
section but is not required for contract conformance.

The `MacosFstabEntry` dataclass maps 1:1 to the six whitespace-separated fields in a
fstab line.  The manager preserves comment and blank lines verbatim during the
read-modify-write cycle.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| mount | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| unmount (normal) | `umount <mountpoint>` |
| unmount (lazy) | `diskutil unmount force <mountpoint>` |
| create_directory | `mkdir -p <path>` (+ `chmod` + `chown` when applicable) |
| remove_directory | `rmdir <path>` or `rm -rf <path>` |
| create_symlink | `ln -s <target> <link>` or `ln -snf <target> <link>` on mismatch |
| remove_symlink | `rm <link>` after `stat -f` type check |
| chown | `chown <user>:<group> <path>` |
| chmod | `chmod <octal> <path>` |
| add_fstab_entry | `IFileOperator.read_text(/etc/fstab)` → modify → `write_text(..., atomic=True)` |
| remove_fstab_entry | `IFileOperator.read_text(/etc/fstab)` → filter → `write_text(..., atomic=True)` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_mounts | `mount` |
| stat | `stat -f '%HT\|%Lp\|%Su\|%Sg\|%z\|%N' <path>` |
| query_disk_usage | `df -b <path>` |
| directory / symlink existence checks | `stat -f` (via `stat` verb) |

All writes pass `elevated=True` when the config enables it.  Reads never elevate.

---

## Output Parsing

### `mount` output

```
/dev/disk1s1s1 on / (apfs, local, read-only, journaled)
/dev/disk1s4 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
```

Parser rules:

- Split each line on ` on ` → `(device, rest)`.
- Split `rest` on the last ` (` → `(mountpoint, options_paren)`.
- Strip the trailing `)` from `options_paren` → `"<fstype>, <options>"`.
- The first comma-separated token is `fstype`; everything else is `options`.

### `stat -f` output

```
Directory|755|root|wheel|64|/etc
```

The parser splits on `|`:

- `path_type = "Directory"` (BSD `%HT` form)
- `mode = "755"`
- `owner = "root"`
- `group = "wheel"`
- `size_bytes = 64`
- `path = "/etc"`

Missing paths produce stderr `stat: <path>: stat: No such file or directory`, which
the parser maps to `None`.

### `df -b` output

```
Filesystem   512-blocks     Used  Available Capacity  Mounted on
/dev/disk1s1 1953595392 524288000 1429307392    27%   /
```

The parser skips the header, splits the data line on whitespace, multiplies each
of `512-blocks`, `Used`, and `Available` by `512` to obtain byte counts, strips
the `%` from `Capacity`, and builds a `MacosDiskUsageInfo`.

### `/etc/fstab` line format

Identical to Linux:

```
UUID=abcd-1234  /Volumes/Backup  hfs  ro,noauto  0  0
```

Parsing and re-emission follow the same rules as the Linux implementations: preserve
comments and blank lines verbatim; build one `MacosFstabEntry` per non-comment line;
write back using tab separators.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `mount` | mount already exists for `(device, mountpoint)` | `get_mounts()` |
| `unmount` | mount point not currently mounted | `get_mounts()` |
| `add_fstab_entry` | matching line already present | `read_text(/etc/fstab)` |
| `remove_fstab_entry` | no matching line | `read_text(/etc/fstab)` |
| `create_directory` | directory exists with matching mode / owner / group | `stat(path)` |
| `remove_directory` | path does not exist | `stat(path)` |
| `create_symlink` | existing symlink with matching target | `stat(link)` + `readlink` |
| `remove_symlink` | link does not exist | `stat(link)` |
| `chown` | current owner / group already matches | `stat(path)` |
| `chmod` | current mode already matches | `stat(path)` |

### Type-Conflict Detection

- `create_symlink(link, target)` with `stat(link).path_type != "Symbolic Link"` →
  `FAILED`.
- `remove_symlink(link)` with `stat(link).path_type != "Symbolic Link"` → `FAILED`.
- `create_directory(path)` with `stat(path).path_type != "Directory"` → `FAILED`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosFilesystemQueryError` | `mount`, `df`, or `stat -f` exits non-zero for a reason other than "No such file or directory" |

All write failures are surfaced as
`MacosFilesystemActionResult(status=FAILED, ...)` carrying the runner's exit code,
stdout, and stderr.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: mount Command Construction

*For any* `(device, mountpoint, fstype, options)` tuple, `mount()` should build a `mount` argv with `-t` and `-o` present only when those arguments are not `None`.

**Validates: Requirements 1.1**

### Property 2: mount Idempotency

*For any* `(device, mountpoint)` already present in `get_mounts()`, `mount()` should return `SKIPPED`.

**Validates: Requirements 1.2**

### Property 3: unmount Lazy via diskutil

*For any* `unmount(mountpoint, lazy=True)` call, the dispatched argv should be `diskutil unmount force <mountpoint>`; for `lazy=False`, `umount <mountpoint>`.

**Validates: Requirements 2.1, 2.2**

### Property 4: unmount Idempotency

*For any* mountpoint absent from `get_mounts()`, `unmount()` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 5: add_fstab_entry Idempotency

*For any* `MacosFstabEntry` whose six fields match an existing `/etc/fstab` line, `add_fstab_entry()` should return `SKIPPED` without writing the file.

**Validates: Requirements 3.2**

### Property 6: add_fstab_entry Advisory Detail

*For any* successful `add_fstab_entry()` write, the result's `detail` must state that macOS fstab is advisory.

**Validates: Requirements 3.4**

### Property 7: add_fstab_entry Update-on-Mismatch

*For any* entry whose `device` or `mountpoint` matches an existing line but whose other fields differ, `add_fstab_entry()` should replace the existing line.

**Validates: Requirements 3.3**

### Property 8: remove_fstab_entry Idempotency

*For any* token that matches no line, `remove_fstab_entry()` should return `SKIPPED`.

**Validates: Requirements 4.2**

### Property 9: get_mounts Output Parsing

*For any* BSD `mount` output, `get_mounts()` should split each line on ` on ` and `(…)` and populate `MacosMountInfo` fields correctly.

**Validates: Requirements 5.1, 5.2**

### Property 10: create_directory Orchestration

*For any* `(path, mode, owner, group)` with all three set, `create_directory()` should dispatch `mkdir -p`, `chmod`, `chown` in that order.

**Validates: Requirements 6.1**

### Property 11: create_directory Idempotency

*For any* existing directory whose `stat` already matches `(mode, owner, group)`, `create_directory()` should return `SKIPPED`.

**Validates: Requirements 6.2**

### Property 12: create_directory Type Conflict

*For any* existing non-directory path, `create_directory()` should return `FAILED`.

**Validates: Requirements 6.3**

### Property 13: remove_directory Recursive Flag

*For any* `remove_directory(path, recursive=True)`, the argv should be `rm -rf <path>`; for `recursive=False`, `rmdir <path>`.

**Validates: Requirements 7.1, 7.2**

### Property 14: create_symlink Existing-Match Idempotency

*For any* existing symlink pointing to `target`, `create_symlink()` should return `SKIPPED`.

**Validates: Requirements 8.2**

### Property 15: create_symlink Atomic Replace

*For any* existing symlink pointing to a different target, `create_symlink()` should dispatch `ln -snf <target> <link>`.

**Validates: Requirements 8.3**

### Property 16: create_symlink Non-Symlink Conflict

*For any* existing non-symlink at `link`, `create_symlink()` should return `FAILED`.

**Validates: Requirements 8.4**

### Property 17: remove_symlink Type Check

*For any* existing non-symlink at `link`, `remove_symlink()` should return `FAILED` without dispatching `rm`.

**Validates: Requirements 9.3**

### Property 18: chown Idempotency

*For any* `(path, user, group)` whose current `stat` already matches, `chown()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 19: chmod Idempotency

*For any* `(path, mode)` whose current `stat.mode` already matches, `chmod()` should return `SKIPPED`.

**Validates: Requirements 11.2**

### Property 20: stat BSD Format Parsing

*For any* `stat -f '%HT|%Lp|%Su|%Sg|%z|%N'` line, `stat()` should populate a `MacosFileInfo` with the correct fields and use the human-readable BSD type string.

**Validates: Requirements 12.1, 12.2**

### Property 21: stat Missing Returns None

*For any* path whose `stat` invocation fails with "No such file or directory", `stat()` should return `None`.

**Validates: Requirements 12.3**

### Property 22: query_disk_usage 512-Block Conversion

*For any* `df -b` output, `query_disk_usage()` should multiply each block count by `512` to obtain `total_bytes`, `used_bytes`, and `available_bytes`, and should strip the trailing `%` from the capacity field.

**Validates: Requirements 13.1, 13.2**

### Property 23: query_disk_usage Failure

*For any* non-zero `df` exit, `query_disk_usage()` should raise `MacosFilesystemQueryError`.

**Validates: Requirements 13.3**

### Property 24: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked and the file operator MUST NOT perform `write_text`; the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.5, 2.6, 3.6, 4.5, 6.6, 7.7, 8.7, 9.6, 10.6, 11.6**

### Property 25: Dry-Run Diff for add_fstab_entry

*For any* `add_fstab_entry()` dry-run, the result's `detail` MUST contain a unified diff of the intended fstab change.

**Validates: Requirements 3.6**

### Property 26: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner and file operator MUST be invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 27: No Elevation for Reads

*For any* read operation (`get_mounts`, `stat`, `query_disk_usage`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 28: Error Result Consistency

*For any* non-zero exit from a write command, the returned `MacosFilesystemActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 1.4, 2.5, 3.5, 4.4, 6.5, 7.6, 8.6, 9.5, 10.5, 11.5**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── filesystemmanagement.py      ← MacosFilesystemManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_filesystemmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.fileoperators import LocalFileOperator
from automojo.sysman.macos.filesystemmanagement import (
    MacosFilesystemManager,
    MacosFilesystemManagerConfig,
    MacosFstabEntry,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator()

config = MacosFilesystemManagerConfig(elevated=True, dry_run=False)
fs_mgr = MacosFilesystemManager(runner=runner, file_operator=file_op, config=config)

# Mount an SMB share
fs_mgr.mount(
    device="//server/share",
    mountpoint="/Volumes/share",
    fstype="smbfs",
    options="nosuid,nodev",
)

entry = MacosFstabEntry(
    device="UUID=ABCD1234-...",
    mountpoint="/Volumes/Backup",
    fstype="hfs",
    options="ro,noauto",
    dump=0,
    passno=0,
)
fs_mgr.add_fstab_entry(entry=entry)

fs_mgr.create_directory(path="/Library/Application Support/Myapp",
                        mode=0o755, owner="root", group="wheel")

fs_mgr.create_symlink(link="/usr/local/bin/myapp",
                      target="/Library/Application Support/Myapp/bin/myapp")

info = fs_mgr.stat(path="/Library/Application Support/Myapp")
print(f"{info.path_type} mode={info.mode}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.fileoperators import SSHFileOperator
from automojo.sysman.macos.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.filesystemmanagement import (
    MacosFilesystemManager, MacosFilesystemManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))
file_op = SSHFileOperator(host="remote-mac", username="admin")

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

fs_mgr = MacosFilesystemManager(
    runner=runner, file_operator=file_op,
    config=MacosFilesystemManagerConfig(elevated=True),
)

for m in fs_mgr.get_mounts():
    print(f"{m.device}\t{m.mountpoint}\t{m.fstype}")
```

### Dry-Run Mode With Diff

```python
config = MacosFilesystemManagerConfig(elevated=True, dry_run=True)
fs_mgr = MacosFilesystemManager(runner=runner, file_operator=file_op, config=config)

entry = MacosFstabEntry(
    device="LABEL=Backup", mountpoint="/Volumes/Backup", fstype="hfs",
    options="ro,noauto", dump=0, passno=0,
)
result = fs_mgr.add_fstab_entry(entry=entry)
assert result.status == MacosFilesystemActionStatus.DRY_RUN
print(result.detail)  # unified diff
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on argv prefix and a `MockFileOperator`.
- `mount` mock output drives `mount`/`unmount` idempotency guards; tests cover the
  BSD-format parser including autofs and disk-image entries.
- `stat -f` mock output drives `create_directory`, `create_symlink`,
  `remove_symlink`, `chown`, `chmod` idempotency and type-conflict tests; the
  parser is tested against BSD `%HT` strings such as `Regular File`, `Directory`,
  and `Symbolic Link`.
- `/etc/fstab` read-modify-write semantics are tested against representative
  multi-line macOS fstab files (commonly empty or with only comment lines on
  stock macOS), asserting that unrelated content survives the rewrite.
- Dry-run tests prove every write short-circuits to `DRY_RUN` without invoking the
  runner or performing `write_text`, and that `add_fstab_entry` in dry-run emits a
  unified diff.
- Elevation tests prove writes pass `elevated=True` when configured and reads never
  do.
- Symlink conflict tests cover: non-symlink at `link` (both create and remove),
  symlink with matching target (create idempotent), symlink with different target
  (atomic replace with `ln -snf`).
- A dedicated test verifies that `unmount(..., lazy=True)` dispatches
  `diskutil unmount force` rather than `umount -l`, which macOS does not support.
- Parser tests for `df -b` cover both small and large filesystems, ensuring
  512-byte block conversion is correct across ≥ 1 TiB datasets.
- No test requires root access or a live macOS filesystem.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `mount` | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| `unmount` | `umount` (normal) / `diskutil unmount force` (lazy) |
| `add_fstab_entry` | `IFileOperator` read-modify-write of `/etc/fstab` |
| `remove_fstab_entry` | `IFileOperator` read-modify-write of `/etc/fstab` |
| `get_mounts` | BSD `mount` |
| `create_directory` | `mkdir -p` + `chmod` + `chown` |
| `remove_directory` | `rmdir` or `rm -rf` |
| `create_symlink` | `ln -s` or `ln -snf` |
| `remove_symlink` | `rm` (after `stat -f` type guard) |
| `chown` | `chown <user>:<group>` |
| `chmod` | `chmod <octal>` |
| `stat` | `stat -f` |
| `query_disk_usage` | `df -b` |

---

## Extensions Beyond the Contract

### `IFileOperator` Extension for Disk-Arbitration-Controlled Persistence

macOS's boot-time mount surface is Disk Arbitration, not `/etc/fstab`.  An
OS-specific extension (not required for contract conformance) provides a
`persist_via_launchd(entry: MacosFstabEntry)` method that:

1. Renders a launchd plist with a `ProgramArguments` block invoking `mount` with
   the entry's parameters.
2. Writes the plist to `/Library/LaunchDaemons/com.automojo.mount.<label>.plist`
   via the `IFileOperator` with `elevated=True`.
3. Dispatches `launchctl bootstrap system <path>` through the command runner.

This extension is deferred; when added it will live behind its own feature flag so
a coordinator writing against the contract does not have to special-case macOS.
