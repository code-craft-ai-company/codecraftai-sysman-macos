# Design

## Overview

`MacosResourceControlManager` implements the cross-OS
resource-control-management contract using **launchd plists**
for the persistent, per-group representation and **`taskpolicy`**
for best-effort per-PID QoS adjustment.  macOS does **not** ship
a cgroup-equivalent kernel substrate — there is no
`/sys/fs/cgroup`, no `systemctl set-property`, no per-group
CPU-quota knob.  This spec frankly documents the gap and
specifies how the manager surfaces those gaps to cross-OS
callers.

The manager is **stateless beyond config**.  Every public verb
takes its inputs from arguments, dispatches commands through
the injected `ICommandRunner`, and returns a result dataclass.
The plist on disk is the single source of truth for a group's
declared state; the kernel-side resource policy is whatever
`launchd` applies on top of it.

---

## Cross-OS Resource-Control Concept Mapping

| Concept                   | Ubuntu (cgroups v2 + systemd)                | macOS (this package)                              | Windows                                        |
|---------------------------|----------------------------------------------|---------------------------------------------------|------------------------------------------------|
| Group container           | systemd `<name>.slice`                       | launchd label `codecraftai-rc-<name>`                | logical record at `<group_dir>\<name>.json`    |
| On-disk representation    | `/etc/systemd/system/<name>.slice`           | `/Library/LaunchDaemons/codecraftai-rc-<name>.plist` | `<group_dir>\<name>.json`                      |
| Kernel substrate          | cgroups v2 at `/sys/fs/cgroup/<name>.slice/` | **none**                                          | Job Objects (in-memory, kernel)                |
| CPU ceiling tool          | `systemctl set-property CPUQuota=N%`         | **NOT SUPPORTED** (`taskpolicy -c utility -p <pid>` is best-effort QoS only) | per-PID `PriorityClass` + `ProcessorAffinity`  |
| Memory ceiling tool       | `systemctl set-property MemoryMax=N`         | launchd `HardResourceLimits.RLIMIT_AS`            | NOT SUPPORTED via PowerShell                   |
| IO ceiling tool           | `systemctl set-property IOWeight=N`          | **NOT SUPPORTED** (`taskpolicy -d background` is per-PID I/O priority only) | NOT SUPPORTED via PowerShell                   |
| Attach process            | write PID to `cgroup.procs`                  | `taskpolicy -p <pid> -c utility` (best-effort)    | logical record + `Get-Process` apply           |
| Detach process            | write PID to `system.slice/cgroup.procs`     | `taskpolicy -p <pid> -c default`                  | logical record drop + reset PriorityClass      |
| Usage source              | `cpu.stat`, `memory.current`, `io.stat`      | `top -l 1 -pid <pid>` aggregated per attached PID | `Get-Process | Measure-Object` aggregated      |
| Persistent reload         | `systemctl daemon-reload`                    | `launchctl bootout` + `launchctl bootstrap`       | none required                                  |
| Idempotency anchor        | byte-equal slice file + `systemctl show`     | byte-equal plist body                             | byte-equal JSON spec                           |

---

## macOS Limitations — Prominent

The following gaps are intentional and surface as
contract-conformant `FAILED` results with typed
`detail`/`message` tokens:

### Gap 1: Per-group CPU quota — NOT SUPPORTED

macOS has no per-group CPU-quota knob in the kernel.
`taskpolicy -c utility -p <pid>` only adjusts the per-PID QoS
class — it does not enforce a percent ceiling.

`set_cpu_limit(name, percent)` therefore returns:

```python
MacosResourceControlActionResult(
    status=MacosResourceControlActionStatus.FAILED,
    detail="macos_no_per_group_cpu_limit",
    message="macOS launchd has no per-group CPU quota",
    command=["taskpolicy", "-c", "utility"],
    group_name=name,
)
```

The `command` field carries the documented best-effort argv so
callers see what could have been done per-PID.

### Gap 2: Per-group IO bandwidth — NOT SUPPORTED

macOS has no per-group IO-bandwidth knob.  `taskpolicy -d
background` adjusts a per-PID I/O priority class
(foreground/background), not a bandwidth ceiling.

`set_io_limits(name, spec)` returns:

```python
MacosResourceControlActionResult(
    status=MacosResourceControlActionStatus.FAILED,
    detail="macos_no_per_group_io_limit",
    message="macOS has no per-group IO bandwidth limit; per-PID I/O priority via taskpolicy -d only",
    command=["taskpolicy", "-d", "background"],
    group_name=name,
)
```

### Gap 3: `attach_process` / `detach_process` is best-effort

`taskpolicy -p <pid> -c utility` adjusts the QoS class only.
The PID is NOT placed in any kernel-side container, and the
group's policy ceilings (e.g. memory) are NOT retroactively
applied to the running process.  The plist's `attached_pids`
array is purely a manager-side bookkeeping artefact for the
benefit of `list_groups()` / `get_group_usage()` aggregation.

### Gap 4: Memory ceilings only apply to launchd children

The launchd `HardResourceLimits.RLIMIT_AS` value applies to
processes that launchd itself spawns under the daemon's
`ProgramArguments` — it does NOT retroactively apply to
processes attached via `attach_process`.  Operators using
`set_memory_limit` should treat it as a declaration of intent
for future-spawned processes inside the group's launchd
hierarchy.

---

## Component Diagram

```
                    Maintenance coordinator
                            │
                            │  injects runner, config
                            ▼
              MacosResourceControlManager
                            │
                            │  .run(argv, elevated=…)
                            ▼
                      ICommandRunner
                            │
              ┌─────────────┴──────────────┐
              ▼                            ▼
       LocalCommandRunner            SSHCommandRunner
              │                            │
              └─────────────┬──────────────┘
                            ▼
                launchctl / tee / cat / ls / rm / taskpolicy
                            │
                            ▼
            /Library/LaunchDaemons/codecraftai-rc-<name>.plist
            launchd domain system/codecraftai-rc-<name>
```

---

## Public Surface

### Class

```python
class MacosResourceControlManager(BaseFeatureManager):
    result_class = MacosResourceControlActionResult
    status_class = MacosResourceControlActionStatus
    feature_name = "resource-control-management"

    def create_group(self, spec: MacosResourceGroupSpec) -> MacosResourceControlActionResult: ...
    def delete_group(self, name: str) -> MacosResourceControlActionResult: ...
    def set_cpu_limit(self, name: str, percent: int) -> MacosResourceControlActionResult: ...   # FAILED
    def set_memory_limit(self, name: str, bytes_: int) -> MacosResourceControlActionResult: ...
    def set_io_limits(self, name: str, spec: MacosIoLimitSpec) -> MacosResourceControlActionResult: ...   # FAILED
    def attach_process(self, name: str, pid: int) -> MacosResourceControlActionResult: ...
    def detach_process(self, name: str, pid: int) -> MacosResourceControlActionResult: ...
    def list_groups(self) -> list[MacosResourceGroupInfo]: ...
    def get_group_usage(self, name: str) -> MacosResourceGroupUsage: ...
```

### Data models

```python
MacosResourceControlActionStatus = ActionStatus

@dataclass
class MacosResourceControlManagerConfig(FeatureConfig):
    plist_dir: str = "/Library/LaunchDaemons"
    label_prefix: str = "codecraftai-rc-"

@dataclass
class MacosResourceControlActionResult(FeatureActionResult):
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None

@dataclass
class MacosIoLimitSpec:
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None

@dataclass
class MacosResourceGroupSpec:
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=list)

@dataclass
class MacosResourceGroupInfo:
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]

@dataclass
class MacosResourceGroupUsage:
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int

class MacosResourceControlQueryError(Exception):
    ...
```

The `MacosResourceControlActionResult` extends the canonical
`FeatureActionResult` shape with `group_name` (every verb is
group-scoped) and `pid` (populated by `attach_process` /
`detach_process` only).  `message` carries the SKIPPED /
FAILED reason token; for the unsupported verbs, `detail`
carries the typed token (`macos_no_per_group_cpu_limit`,
`macos_no_per_group_io_limit`).

---

## Verb Implementations

### `create_group(spec)`

```
1. validate spec.name (regex ^[A-Za-z0-9][A-Za-z0-9._\-]*$)
2. plist_path = "<plist_dir>/<label_prefix><name>.plist"
3. plist_body = _render_plist_body(spec)
4. if config.dry_run -> DRY_RUN with the planned `tee` argv
5. existing_body = _read_file(plist_path)
6. if existing_body == plist_body -> SKIPPED
7. tee plist_path  (stdin=plist_body)
8. launchctl bootstrap system <plist_path>
9. SUCCESS / FAILED
```

The rendered plist body has shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" ...>
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>codecraftai-rc-<name></string>
  <key>HardResourceLimits</key>
  <dict>
    <key>RLIMIT_AS</key>
    <integer><memory_bytes></integer>     <!-- only when memory_bytes set -->
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/true</string>
  </array>
  <key>Disabled</key>
  <true/>
  <key>AttachedPIDs</key>
  <array>
    <integer><pid1></integer>
    ...
  </array>
</dict>
</plist>
```

`Disabled=true` ensures launchd does not attempt to start the
placeholder daemon — the plist is a configuration artefact only.

### `delete_group(name)`

```
1. validate name
2. if config.dry_run -> DRY_RUN
3. if not _path_exists(plist_path) -> SKIPPED
4. launchctl bootout system/<label>     (informational; ignore non-zero)
5. rm -f <plist_path>
6. SUCCESS / FAILED
```

### `set_cpu_limit(name, percent)`  -- FAILED

```
1. validate name
2. if config.dry_run -> DRY_RUN with the documented argv
3. return FAILED(detail="macos_no_per_group_cpu_limit",
                 message="macOS launchd has no per-group CPU quota",
                 command=["taskpolicy", "-c", "utility"])
```

### `set_memory_limit(name, bytes_)`

```
1. validate name
2. if config.dry_run -> DRY_RUN
3. existing_spec = _load_group_spec_from_plist(name)
4. if existing_spec is None -> FAILED("group_not_found")
5. if existing_spec.memory_bytes == bytes_ -> SKIPPED
6. spec = replace(existing_spec, memory_bytes=bytes_)
7. _write_plist(spec)
8. launchctl bootout system/<label>     (informational)
9. launchctl bootstrap system <plist_path>
10. SUCCESS / FAILED
```

### `set_io_limits(name, spec)` -- FAILED

```
1. validate name
2. if config.dry_run -> DRY_RUN with the documented argv
3. return FAILED(detail="macos_no_per_group_io_limit",
                 message="macOS has no per-group IO bandwidth limit; per-PID I/O priority via taskpolicy -d only",
                 command=["taskpolicy", "-d", "background"])
```

### `attach_process(name, pid)`

```
1. validate name; pid > 0
2. if config.dry_run -> DRY_RUN with the planned `taskpolicy` argv
3. existing_spec = _load_group_spec_from_plist(name)
4. if existing_spec is None -> FAILED("group_not_found")
5. if pid in existing_spec.attached_pids -> SKIPPED
6. taskpolicy -p <pid> -c utility
7. spec = replace(existing_spec, attached_pids=existing_spec.attached_pids + [pid])
8. _write_plist(spec) ; launchctl bootout ; launchctl bootstrap
9. SUCCESS / FAILED
```

### `detach_process(name, pid)`

```
1. validate name; pid > 0
2. if config.dry_run -> DRY_RUN with the planned `taskpolicy` argv
3. existing_spec = _load_group_spec_from_plist(name)
4. if existing_spec is None -> FAILED("group_not_found")
5. if pid not in existing_spec.attached_pids -> SKIPPED
6. taskpolicy -p <pid> -c default
7. spec = replace(existing_spec, attached_pids=[p for p in pids if p != pid])
8. _write_plist(spec) ; launchctl bootout ; launchctl bootstrap
9. SUCCESS / FAILED
```

### `list_groups()`

```
1. ls -1 <plist_dir>
2. select entries matching <label_prefix>*.plist
3. for each: cat <plist_path>; parse fields
4. assemble MacosResourceGroupInfo per plist
5. return list
```

### `get_group_usage(name)`

```
1. plist_path = ... ; if not _path_exists(plist_path) ->
   raise MacosResourceControlQueryError(detail="unknown_group")
2. spec = _load_group_spec_from_plist(name)
3. for pid in spec.attached_pids:
       run `top -l 1 -stats pid,cpu,mem -pid <pid>` ; parse cpu%, rss
4. aggregate cpu and memory across PIDs
5. return MacosResourceGroupUsage(
       cpu_usage_seconds=None,         # macOS top reports % not s
       memory_used_bytes=<sum>,
       io_read_bytes=None,             # macOS top has no per-PID I/O bytes
       io_write_bytes=None,
       process_count=<len>,
   )
```

---

## Idempotency

| Verb              | Probe                                         | SKIPPED When                                    |
|-------------------|-----------------------------------------------|--------------------------------------------------|
| `create_group`    | `cat <plist_path>`                            | byte-equal plist body                            |
| `delete_group`    | `test -e <plist_path>`                        | plist file absent                                |
| `set_cpu_limit`   | n/a — always FAILED                           | (never; macOS limitation)                        |
| `set_memory_limit`| `cat <plist_path>` -> spec.memory_bytes       | parsed memory_bytes equals requested             |
| `set_io_limits`   | n/a — always FAILED                           | (never; macOS limitation)                        |
| `attach_process`  | `cat <plist_path>` -> spec.attached_pids      | PID already in list                              |
| `detach_process`  | `cat <plist_path>` -> spec.attached_pids      | PID NOT in list                                  |

Every probe is dispatched with `elevated=False`.

---

## Elevation

| Method group | `elevated` value |
|--------------|------------------|
| writes (`create_group`, `delete_group`, `set_memory_limit`, `attach_process`, `detach_process`) | `config.elevated` |
| reads (`list_groups`, `get_group_usage`)                                                         | always `False`    |
| internal probes (`cat`, `ls`, `test -e`)                                                         | always `False`    |

---

## Error Reporting

Every write verb funnels failures through the same surface:

```python
MacosResourceControlActionResult(
    status=MacosResourceControlActionStatus.FAILED,
    detail=name_or_macos_token,
    exit_code=run_result.exit_code,
    stdout=run_result.stdout,
    stderr=run_result.stderr,
    command=list(failing_argv),
    group_name=name,
    pid=pid_or_none,
    message="<short_reason_token>",
)
```

The `message` field carries one of:

- `"plist_write_failed"` — `tee` returned non-zero.
- `"bootstrap_failed"` — `launchctl bootstrap` returned non-zero.
- `"plist_delete_failed"` — `rm` returned non-zero.
- `"taskpolicy_failed"` — `taskpolicy` returned non-zero.
- `"group_not_found"` — pre-flight detected the plist was missing.
- `"macOS launchd has no per-group CPU quota"` —
  set_cpu_limit gap.
- `"macOS has no per-group IO bandwidth limit; ..."` —
  set_io_limits gap.

Plus the typed `detail` tokens for the gaps:

- `"macos_no_per_group_cpu_limit"`
- `"macos_no_per_group_io_limit"`

---

## Internal helpers

```python
def _validate_name(name: str) -> None
def _validate_pid(pid: int) -> None
def _plist_path(name: str) -> str
def _label_for(name: str) -> str
def _render_plist_body(spec: MacosResourceGroupSpec) -> str
def _load_group_spec_from_plist(body: str, name: str) -> Optional[MacosResourceGroupSpec]

def _read_file(path: str) -> Optional[str]
def _path_exists(path: str) -> bool

def _parse_plist_attached_pids(body: str) -> list[int]
def _parse_plist_memory_bytes(body: str) -> Optional[int]
def _parse_top_output(stdout: str) -> tuple[Optional[float], Optional[int]]
```

Each helper has a sphinx docstring; `_parse_*` helpers are
stand-alone module-level pure functions.

---

## Test plan

- 14+ pytest functions in
  `source/testroots/testbase/ccai/tests/sysman/macos/test_resourcecontrolmanagement.py`.
- Coverage:

| Test                                                  | Covers                                                  |
|-------------------------------------------------------|---------------------------------------------------------|
| `test_create_group_invokes_launchctl`                 | `tee` + `launchctl bootstrap`                           |
| `test_create_group_skipped_when_present`              | byte-equal plist body                                   |
| `test_create_group_dry_run`                           | DRY_RUN short-circuit                                   |
| `test_delete_group_invokes_launchctl`                 | `bootout` + `rm`                                        |
| `test_set_cpu_limit_returns_failed_with_macos_detail` | typed FAILED + `detail="macos_no_per_group_cpu_limit"`  |
| `test_set_memory_limit_invokes_tee`                   | re-render plist + bootstrap                             |
| `test_set_memory_limit_skipped_when_match`            | RLIMIT_AS already matches                               |
| `test_set_io_limits_returns_failed_with_macos_detail` | typed FAILED + `detail="macos_no_per_group_io_limit"`   |
| `test_attach_process_invokes_taskpolicy`              | `taskpolicy -p <pid> -c utility` argv                   |
| `test_attach_process_skipped_when_present`            | PID already in attached_pids                            |
| `test_detach_process_invokes_taskpolicy`              | `taskpolicy -p <pid> -c default` argv                   |
| `test_list_groups_returns_list`                       | enumerate plists + parse fields                         |
| `test_get_group_usage_parses_top_output`              | aggregate `top` snapshots                               |
| `test_writes_elevate`                                 | `elevated=True` reaches the runner for writes           |
| `test_reads_never_elevate`                            | reads always run with `elevated=False`                  |
| `test_failed_command_surfaces_stderr`                 | non-zero exit -> FAILED with stderr captured            |

---

## Out of Scope

- Translating the abstract IO contract onto macOS's `iopriority`
  / `setiopolicy_np` family — beyond pilot scope.
- Per-process CPU quota emulation via spawn-time wrapper
  scripts — orthogonal to the contract verb set.
- Application-layer ULimit enforcement beyond `RLIMIT_AS` — the
  contract speaks of a single memory cap.
- Watching launchd domain state via streaming `launchctl print`
  — `list_groups` returns a snapshot only.
