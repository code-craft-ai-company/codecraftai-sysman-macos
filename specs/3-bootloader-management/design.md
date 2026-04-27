# Design

## Overview

The bootloader-management feature on macOS is implemented as a
single module, `automojo.sysman.macos.bootloadermanagement`, that
exposes a `MacosBootloaderManager` class.  The class is a contract-
conforming implementation of the cross-OS bootloader-management
contract, but the macOS surface diverges from the Linux primaries in
ways that the contract explicitly anticipates: macOS does not expose
a multi-entry boot menu, two of the verbs reduce to SUCCESS no-ops,
and `add_entry` accepts only one entry kind.

The maintenance coordinator instantiates `MacosBootloaderManager`
with a command runner and calls its methods.  All system commands
(`bless`, `nvram`) are executed through the `automojo-interfaces`
command-runner abstraction.  The manager never calls `subprocess`,
`os.path`, or `open()` directly; it only constructs argument lists
and passes them to the injected runner.

---

## Distinction from `boot-management` (UEFI)

This is **not** the UEFI firmware boot-entry feature.  The two
features operate on different layers:

```
+----------------------------------------------------------+
|                      Operator request                    |
+----------------------------------------------------------+
                            |
                +-----------+-----------+
                v                       v
+-------------------------+   +---------------------------+
|     boot-management     |   |  bootloader-management    |
|                         |   |                           |
|  Layer: FIRMWARE        |   |  Layer: OS-LEVEL          |
|  Tool : efibootmgr,     |   |  Tool : bless, nvram      |
|         bcdedit /enum   |   |                           |
|         firmware        |   |                           |
|  Store: NVRAM (UEFI)    |   |  Store: bless'd volumes,  |
|                         |   |         NVRAM boot-args   |
+-------------------------+   +---------------------------+
                |                       |
                +-----------+-----------+
                            v
+----------------------------------------------------------+
|                       Boot sequence                      |
|                                                          |
|  1. iBoot reads NVRAM (boot-management territory)        |
|  2. iBoot launches the blessed loader on the EFI volume  |
|  3. The loader reads boot-args from NVRAM (THIS feature) |
|  4. The loader launches the kernel                       |
+----------------------------------------------------------+
```

On Apple Silicon there is no UEFI surface at all; on Intel Macs the
firmware boot entry is owned by Apple's iBoot rather than by user-
space tools.  This module never invokes `efibootmgr` and never
reads or writes firmware boot variables directly.

---

## Cross-OS Bootloader-Model Mapping

The cross-OS contract abstracts over a wide range of OS-level
bootloader implementations.  macOS maps onto the contract verbs as
follows:

| Cross-OS verb              | macOS native primitive                            | Reduces to NO-OP / FAILED on macOS? |
|----------------------------|---------------------------------------------------|--------------------------------------|
| `list_entries`             | `bless --info --verbose` + `nvram boot-args`      | NO -- returns the active boot volume |
| `get_default_entry`        | `bless --info --getBoot` (with verbose fallback)  | NO                                   |
| `set_default_entry`        | `bless --mount <path> --setBoot`                  | NO                                   |
| `add_entry`                | `bless --mount <path>` (BLESS_VOLUME only)        | FAILED for non-BLESS_VOLUME kinds    |
| `remove_entry`             | `bless --unbless <path>`                          | NO (limited support on sealed FS)    |
| `update_bootloader_config` | (none -- OS-managed)                              | YES -- SUCCESS no-op                 |
| `regenerate_config`        | (none -- OS-managed)                              | YES -- SUCCESS no-op                 |

Comparison with sister implementations:

| OS      | OS-level loader            | Default-entry store                | Apply step                       | Reboot required for change? |
|---------|----------------------------|------------------------------------|----------------------------------|------------------------------|
| RHEL    | GRUB 2                     | `grub2-editenv`                    | `grub2-mkconfig`                 | YES                          |
| Ubuntu  | GRUB 2 / systemd-boot      | `GRUB_DEFAULT` / `bootctl`         | `update-grub` / `bootctl update` | YES                          |
| macOS   | bless / iBoot              | NVRAM `efi-boot-device`            | (firmware-managed; no user step) | YES                          |
| Windows | BCD                        | `bcdedit /default {<guid>}`        | (immediate; no apply step)       | YES                          |

---

## Why SUCCESS no-ops for `update_bootloader_config` /  `regenerate_config`?

The cross-OS contract permits two responses to a verb that does not
map onto the native facility:

1. Return `SUCCESS` with `detail="not applicable"` and pretend the
   step ran.
2. Return `FAILED` with an explanatory `detail`.

For these two verbs this spec deliberately chooses option (1) because
the *intent* of "regenerate / apply the bootloader config" is
actually achieved on macOS without any explicit user step:

* When `bless` writes a new boot device, the OS rebuilds the relevant
  loader caches automatically.
* There is no separate apply phase; the moment `bless --setBoot`
  succeeds the change is in effect for the next boot.

Returning `FAILED` from these verbs would mislead compliance
reporting into believing the host's bootloader configuration was
broken, when in fact macOS already handled the rebuild as part of
the preceding write.  The `message` field carries the no-op
rationale so that the operator sees the honest explanation in the
result payload.

For `add_entry` with non-BLESS_VOLUME kinds the choice flips to
option (2) because pretending those kinds succeeded would silently
drop a configuration request.

---

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates LocalCommandRunner / SSHCommandRunner       |
|  - Configures aspects (optional)                            |
|  - Injects runner into MacosBootloaderManager               |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|                MacosBootloaderManager                       |
|  automojo.sysman.macos.bootloadermanagement                 |
|                                                             |
|  list_entries()           get_default_entry()               |
|  set_default_entry(id)    add_entry(spec)                   |
|  remove_entry(id)         update_bootloader_config()        |
|  regenerate_config()                                        |
+-------------------+-----------------------------------------+
                    | delegates commands to
                    v
+-------------------------------------------------------------+
|                     ICommandRunner                          |
|     run(args, elevated, capture_output)                     |
|     -> RunResult                                            |
+-------------------+-----------------------------------------+
                    |
       +------------+------------+
       v                         v
+----------------+      +-------------------+
| LocalCommand   |      | SSHCommand        |
| Runner         |      | Runner            |
+----------------+      +-------------------+
```

---

## Components

### `MacosBootloaderManager`

**Location:** `source/packages/automojo/sysman/macos/bootloadermanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and
a `MacosBootloaderManagerConfig`.

| Method                                       | Description                                                              |
|----------------------------------------------|--------------------------------------------------------------------------|
| `list_entries()` -> `list[MacosBootEntryInfo]` | Enumerate the currently blessed boot volumes                            |
| `get_default_entry()` -> `MacosBootEntryInfo`  | Return the active boot volume                                           |
| `set_default_entry(entry_id)` -> `Result`      | Persist `entry_id` (volume path) as the active boot device              |
| `add_entry(spec)` -> `Result`                  | Bless a volume so the firmware treats it as bootable                    |
| `remove_entry(entry_id)` -> `Result`           | Unbless a volume                                                        |
| `update_bootloader_config()` -> `Result`       | SUCCESS no-op (macOS rebuilds caches automatically)                     |
| `regenerate_config()` -> `Result`              | SUCCESS no-op (macOS rebuilds caches automatically)                     |

### `MacosBootloaderManagerConfig`

A dataclass holding runtime configuration for
`MacosBootloaderManager`.

| Field             | Type             | Default | Description                                                       |
|-------------------|------------------|---------|-------------------------------------------------------------------|
| `dry_run`         | `bool`           | `False` | When `True`, no mutating commands are executed                    |
| `elevated`        | `bool`           | `False` | When `True`, mutating commands are dispatched with elevation      |
| `default_volume`  | `Optional[str]`  | `None`  | Fallback volume path used when `bless --info` cannot resolve one  |

---

## Data Models

### `MacosBootloaderActionStatus`

```python
MacosBootloaderActionStatus = ActionStatus  # alias

class ActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### `MacosBootloaderActionResult`

```python
@dataclass
class MacosBootloaderActionResult(FeatureActionResult):
    status: ActionStatus
    detail: Optional[str]
    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    command: Optional[list[str]]
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None
```

### `MacosBootEntryKind`

```python
class MacosBootEntryKind(Enum):
    BLESS_VOLUME  = "bless_volume"
    KERNEL_ENTRY  = "kernel_entry"
    LOADER_ENTRY  = "loader_entry"
```

### `MacosBootEntryInfo`

```python
@dataclass
class MacosBootEntryInfo:
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None
```

### `MacosBootEntrySpec`

```python
@dataclass
class MacosBootEntrySpec:
    entry_kind: MacosBootEntryKind
    volume_path: Optional[str] = None
    name: Optional[str] = None
```

### Exceptions

| Exception                    | Raised When                                                    |
|------------------------------|----------------------------------------------------------------|
| `MacosBootloaderQueryError`  | A read fails unexpectedly, or no boot device can be resolved.  |

---

## Correctness Properties

### Property 1: Idempotent `set_default_entry`

For any volume path, calling `set_default_entry()` when the supplied
volume is already the active boot device should return a result with
status `SKIPPED` and must not invoke `bless --setBoot`.

**Validates: Requirements 3.2**

### Property 2: Idempotent `add_entry`

For any volume path, calling `add_entry()` with a `BLESS_VOLUME`
spec when the supplied volume is already the active boot device
should return a result with status `SKIPPED` and must not invoke
`bless --mount`.

**Validates: Requirements 4.4, 8.2**

### Property 3: Unsupported Entry Kind Rejection

For any `add_entry` call whose `spec.entry_kind` is not
`BLESS_VOLUME`, the result must be `FAILED` with `stderr` token
`unsupported_entry_kind` and the runner must not be invoked.

**Validates: Requirements 4.2**

### Property 4: Invalid Spec Rejection

For any `add_entry` call with `entry_kind=BLESS_VOLUME` but no
`volume_path`, the result must be `FAILED` with `stderr` token
`invalid_entry_spec` and the runner must not be invoked.

**Validates: Requirements 4.3**

### Property 5: No-Op Behaviour for OS-Managed Verbs

For any call to `update_bootloader_config()` or
`regenerate_config()`, the runner must not be invoked, the result
must be `SUCCESS`, `requires_reboot` must be `False`, and the
`message` must explain the no-op rationale.

**Validates: Requirements 6.1, 6.2, 6.3, 7.1, 7.2, 7.3**

### Property 6: Dry-Run Safety

For any mutating operation with `dry_run=True`, the command runner
must never be invoked with a mutating command, and the returned
result's `status` must be `DRY_RUN`.

**Validates: Requirements 10.1, 10.2**

### Property 7: Elevation Consistency

For any mutating operation with `elevated=True` in the config, the
command runner must be invoked with `elevated=True`; for any
read-only query, elevation must never be requested.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 8: Status Mapping Correctness

For any operation result from the command runner, the
`MacosBootloaderActionResult.status` must correctly reflect the
runner's exit code: `0` maps to `SUCCESS`, non-zero maps to
`FAILED` (unless the operation was skipped or in dry-run mode), with
the exit code and captured stderr preserved on `FAILED` results.

**Validates: Requirements 3.3, 5.3, 12.2**

### Property 9: No UEFI NVRAM Touch

For any verb invocation, no command argv may equal or start with
`efibootmgr` and no path argument may resolve under the firmware
boot-variable surface.

**Validates: Requirements 11.1, 11.2**

### Property 10: Reboot Signalling

For any successful write (`set_default_entry`, `add_entry`,
`remove_entry`), `result.requires_reboot` must be `True` because
the firmware boot device hint is consulted only at boot.

**Validates: Requirements 12.1**

### Property 11: Active-Boot-Device-Only Idempotency

The macOS bless surface does not expose a multi-bless inventory.
The only blessed-volume signal available is whether the firmware
boot device matches a given path.  Idempotency probes for
`add_entry` and `remove_entry` therefore consult only the active
boot device; non-active blessed volumes are not detected.  This
is documented on the manager class.

**Validates: Requirements 5.2, 8.2, 8.3**

### Property 12: Boot-Args Passthrough

For any `list_entries` or `get_default_entry` call, the active
entry's `cmdline` field must equal the value returned by
`nvram boot-args`, or `None` when `nvram` returns a non-zero exit
code.

**Validates: Requirements 1.2, 2.2**

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

`MacosBootloaderManager` depends on this interface, injected at
construction time.

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `automojo-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

When `MacosBootloaderManagerConfig.elevated` is `True`,
`MacosBootloaderManager` passes `elevated=True` to
`ICommandRunner.run()` for every mutating command.  The runner is
responsible for prepending `sudo` or using whatever privilege-
escalation mechanism is appropriate for the environment.  The
manager has no knowledge of how elevation is implemented.  Read-only
queries never request elevation.

---

## Command Mappings

| Operation                  | Command                                                  |
|----------------------------|----------------------------------------------------------|
| `list_entries`             | `bless --info --verbose`, then `nvram boot-args`         |
| `get_default_entry`        | `bless --info --getBoot` (terse), verbose fallback       |
| `set_default_entry`        | `bless --mount <path> --setBoot`                         |
| `add_entry` (BLESS_VOLUME) | `bless --mount <path>`                                   |
| `remove_entry`             | `bless --unbless <path>`                                 |
| `update_bootloader_config` | (none -- SUCCESS no-op)                                  |
| `regenerate_config`        | (none -- SUCCESS no-op)                                  |
| Idempotency probes         | `bless --info --getBoot` (verbose fallback)              |

---

## Error Handling

### Typed Exceptions

| Exception                    | Raised When                                                                                |
|------------------------------|--------------------------------------------------------------------------------------------|
| `MacosBootloaderQueryError`  | `bless --info` fails unexpectedly, or no boot device can be resolved by either bless form. |

All other failures are surfaced as a `MacosBootloaderActionResult`
with `status=FAILED` carrying the underlying `exit_code`, `stdout`,
and `stderr`.  This includes:

- `bless --setBoot` permission errors on sealed system volumes.
- `bless --unbless` failures when the volume is currently mounted as
  the boot device.
- Spec-validation rejections for `add_entry` (typed via the `stderr`
  token field).

### Idempotency Guards

Before executing mutating commands, `MacosBootloaderManager` consults
the current boot device via `bless --info --getBoot`.  When the
host is already in the desired state the method returns `SKIPPED`
without invoking the apply step.  The macOS bless surface does not
expose a multi-bless inventory, so non-active blessed volumes are
not detected; this is documented as a known limitation on the
manager class.

### Destructive-Change Safety

`bless --setBoot` writes affect the firmware boot device hint and a
misconfiguration can cause the next boot to land on an unsigned,
sealed, or absent volume.  This module:

1. Sets `requires_reboot=True` on every successful write so the
   coordinator can surface the change to the operator.
2. Surfaces the underlying `bless` stderr verbatim on `FAILED`
   results.
3. Documents in the module docstring that callers SHOULD verify the
   resulting state via `get_default_entry` before rebooting and
   SHOULD keep a known-good fallback volume blessed before any
   change.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
+-- __init__.py
+-- bootloadermanagement.py          (this feature)

source/testroots/testbase/automojo/tests/sysman/macos/
+-- test_bootloadermanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` injected into
  `MacosBootloaderManager` at construction time.
- Tests verify the *arguments* passed to the runner, not real
  system state.
- Each public verb has at minimum: a success path test, a skip /
  idempotency test where applicable, a failure path test, and a
  dry-run test.
- The unsupported-entry-kind rejection is exercised by an
  explicit test that asserts the `stderr` token is
  `unsupported_entry_kind`.
- The no-op verbs are exercised by tests that assert the runner
  was not invoked.
- No test requires root access, a live `bless`, or a real macOS
  host.

### Coverage Targets

At least 14 unit tests covering every public verb, every
idempotency path, the unsupported-kind rejection, the elevation
policy on writes, the no-elevation policy on reads, and the
failed-command stderr passthrough.

---

## Usage Examples

### Local Execution (Set Default Boot Volume)

```python
from automojo.sysman.macos.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.macos.bootloadermanagement import (
    MacosBootloaderManager,
    MacosBootloaderManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosBootloaderManagerConfig(elevated=True)
manager = MacosBootloaderManager(runner=runner, config=config)

result = manager.set_default_entry(
    entry_id="/Volumes/Macintosh HD",
)
print("set boot device: {} (reboot: {})".format(
    result.status, result.requires_reboot,
))
```

### Bless an Alternate Volume

```python
from automojo.sysman.macos.bootloadermanagement import (
    MacosBootEntryKind,
    MacosBootEntrySpec,
    MacosBootloaderManager,
    MacosBootloaderManagerConfig,
)

manager = MacosBootloaderManager(
    runner=runner,
    config=MacosBootloaderManagerConfig(elevated=True),
)
spec = MacosBootEntrySpec(
    entry_kind=MacosBootEntryKind.BLESS_VOLUME,
    volume_path="/Volumes/Recovery",
    name="Recovery Volume",
)
result = manager.add_entry(spec=spec)
print("add: {}".format(result.status))
```

### Dry-Run Across Transports

```python
manager = MacosBootloaderManager(
    runner=runner,
    config=MacosBootloaderManagerConfig(elevated=True, dry_run=True),
)
result = manager.set_default_entry(
    entry_id="/Volumes/Macintosh HD",
)
assert result.status.name == "DRY_RUN"
```
