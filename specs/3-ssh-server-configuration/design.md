# Design

## Overview

The ssh-server-configuration feature is implemented as a single
module, `automojo.sysman.macos.sshserverconfiguration`, that exposes a
`MacosSshServerConfigurationManager` class.  This spec implements the
ssh-server-configuration contract defined at
`automojo-sysman-common/specs/contracts/ssh-server-configuration/contract.md`
for macOS using the stock OpenSSH server (Remote Login) shipped with
the operating system.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` and `tee`, and service interaction
routes through `launchctl`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosSshServerConfigurationManager   │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         MacosSshServerConfigurationManager                  │
│  automojo.sysman.macos.sshserverconfiguration               │
│                                                             │
│  get_sshd_setting()       set_sshd_setting()                │
│  remove_sshd_setting()    list_sshd_settings()              │
│  add_match_block()        remove_match_block()              │
│  list_match_blocks()      reload_sshd()                     │
│  test_sshd_config()       list_authorized_keys()            │
│  add_authorized_key()     remove_authorized_key()           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
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

### `MacosSshServerConfigurationManager`

**Location:**
`source/packages/automojo/sysman/macos/sshserverconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_sshd_setting` | `(keyword) → MacosSshSettingValue \| None` | Read a top-level keyword |
| `set_sshd_setting` | `(keyword, value) → MacosSshActionResult` | Upsert a top-level keyword |
| `remove_sshd_setting` | `(keyword) → MacosSshActionResult` | Drop a top-level keyword |
| `list_sshd_settings` | `() → dict[str, str]` | Enumerate top-level keywords |
| `add_match_block` | `(criteria, settings) → MacosSshActionResult` | Upsert a Match block |
| `remove_match_block` | `(criteria) → MacosSshActionResult` | Drop a Match block |
| `list_match_blocks` | `() → list[MacosSshMatchBlock]` | Enumerate Match blocks |
| `reload_sshd` | `() → MacosSshActionResult` | `launchctl kickstart -k system/com.openssh.sshd` |
| `test_sshd_config` | `() → MacosSshActionResult` | `sshd -t` |
| `list_authorized_keys` | `(user) → list[MacosSshAuthorizedKey]` | Read a user's keys |
| `add_authorized_key` | `(user, key) → MacosSshActionResult` | Append a key |
| `remove_authorized_key` | `(user, fingerprint) → MacosSshActionResult` | Drop a key by fingerprint |

### `MacosSshServerConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `sshd_config_path` | `str` | `"/etc/ssh/sshd_config"` | Primary `sshd_config` |
| `authorized_keys_path_template` | `str` | `"/Users/{user}/.ssh/authorized_keys"` | Per-user key path |
| `service_name` | `str` | `"com.openssh.sshd"` | launchd label |
| `service_domain` | `str` | `"system"` | launchd domain (e.g. `"system"` or `"gui/501"`) |

---

## Data Models

### Result Types

```python
class MacosSshActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class MacosSshActionResult(FeatureActionResult):
    status: MacosSshActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    keyword: str | None
    path: str | None
    user: str | None
    message: str | None
```

### Query Result Types

```python
@dataclass
class MacosSshSettingValue:
    keyword: str
    value: str
    source_file: str

@dataclass
class MacosSshMatchBlock:
    criteria: str
    settings: dict[str, str]

@dataclass
class MacosSshAuthorizedKey:
    key_type: str
    key_data: str
    comment: str | None
    fingerprint: str | None
    options: str | None
```

### Exceptions

```python
class MacosSshQueryError(Exception):
    """Raised when a read fails for a reason other than 'not present'."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

The manager consumes only `args`, `elevated`, and `stdin` — reads use
`cat`; writes use `tee` with the rendered content on stdin.  No
`IFileOperator` is consumed.

---

## Configuration Path Mapping

| OS | Path |
|----|------|
| RHEL | `/etc/ssh/sshd_config` |
| Ubuntu | `/etc/ssh/sshd_config` |
| **macOS** | **`/etc/ssh/sshd_config`** |
| Windows | `%ProgramData%\ssh\sshd_config` |

macOS shares the path with the other Unixes.  The reload mechanism,
however, differs:

| OS | Reload mechanism |
|----|-----------------|
| RHEL | `systemctl reload sshd` |
| Ubuntu | `systemctl reload ssh` |
| **macOS** | **`launchctl kickstart -k system/com.openssh.sshd`** |
| Windows | `Restart-Service sshd` |

`launchctl kickstart -k <domain>/<name>` is the canonical macOS
re-execution command.  The ``-k`` flag stops a running instance
before restarting it.  This is heavier than a SIGHUP-style reload —
the listener is briefly torn down and re-created — but it is the
standard mechanism documented in `man launchctl`.  Established sshd
sessions continue running because they are owned by per-connection
child processes that are not bound to the listener's lifetime.

---

## sshd_config Parsing

The grammar, parser shape, and renderer shape are identical to the
RHEL / Ubuntu siblings:

- Each non-comment, non-empty line is `<Keyword> <value>` or `Match
  <criteria>`.
- The keyword is case-insensitive; value text runs to end-of-line.
- A `Match <criteria>` line opens a block; subsequent setting lines
  belong to the block until the next `Match` or EOF.

`_parse_sshd_config(content) → (top_settings, match_blocks)`:

- `top_settings: dict[str, str]` — keyword (original case) to value;
  last occurrence wins on read.
- `match_blocks: list[MacosSshMatchBlock]` — every `Match` block in
  declaration order.

`_render_sshd_config(top_settings, match_blocks) → str`:

1. Top-level settings in dict order.
2. For each `Match` block: a `Match <criteria>` line followed by
   four-space-indented child settings.
3. Trailing newline.

Comments and blank lines are not preserved across mutation; this is
documented in `tasks.md` as a follow-up.

---

## Match Block Idempotency

`add_match_block` and `remove_match_block` use whitespace-collapsed
criteria comparison.  Settings are compared as-rendered: a block
exists if the rendered `sshd_config` after upsert is byte-equal to
the existing file.

---

## authorized_keys Parsing

### Grammar

```
[options ]<key-type> <key-data> [<comment>]
```

Recognised key types: `ssh-rsa`, `ssh-dss`, `ssh-ed25519`,
`ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`,
`sk-ecdsa-sha2-nistp256@openssh.com`, `sk-ssh-ed25519@openssh.com`.

The parser scans tokens for the first recognised key-type token;
everything before it (when non-empty) becomes `options`.

### Fingerprint Computation

```python
digest = hashlib.sha256(base64.b64decode(key_data)).digest()
fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
```

This matches OpenSSH's `ssh-keygen -lf` format exactly.  No external
`ssh-keygen` invocation is required for identity comparisons.

---

## Command Mappings

### Write Commands

| Operation | Command | stdin |
|---|---|---|
| Set keyword | `tee /etc/ssh/sshd_config` | rendered file |
| Remove keyword | `tee /etc/ssh/sshd_config` | rendered file |
| Add Match block | `tee /etc/ssh/sshd_config` | rendered file |
| Remove Match block | `tee /etc/ssh/sshd_config` | rendered file |
| Add authorized key | `tee /Users/<user>/.ssh/authorized_keys` | rendered file |
| Remove authorized key | `tee /Users/<user>/.ssh/authorized_keys` | rendered file |
| Reload | `launchctl kickstart -k system/com.openssh.sshd` | — |
| Test config | `sshd -t` | — |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Read sshd_config | `cat /etc/ssh/sshd_config` |
| Read authorized_keys | `cat /Users/<user>/.ssh/authorized_keys` |

All read commands pass `elevated=False` regardless of
`config.elevated`.

---

## Idempotency Guards

| Method | Guard | Comparison |
|---|---|---|
| `set_sshd_setting` | Rendered content matches existing | Byte-equal |
| `remove_sshd_setting` | Keyword absent OR file absent | Pure-Python |
| `add_match_block` | Rendered content matches existing | Byte-equal |
| `remove_match_block` | No block with matching criteria | Whitespace-collapsed |
| `add_authorized_key` | Fingerprint already present | SHA-256 |
| `remove_authorized_key` | Fingerprint not present | SHA-256 |
| `reload_sshd` | (none — always executes) | — |
| `test_sshd_config` | (none — always executes) | — |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `MacosSshQueryError` | Reserved for future read paths.  The pilot's `cat`-based reads treat any non-zero exit as "absent" |

All other failures from write operations are surfaced as
`MacosSshActionResult` with `status=FAILED`.  Writes do not raise.

---

## Correctness Properties

### Property 1: Read Returns Parsed Value

*For any* `sshd_config` containing a top-level `<keyword> <value>`,
`get_sshd_setting(keyword)` returns a `MacosSshSettingValue` with
matching fields.

**Validates: Requirements 1.1, 1.2**

### Property 2: Read Returns None When Absent

*For any* `sshd_config` not containing the keyword,
`get_sshd_setting(keyword)` returns `None`.

**Validates: Requirements 1.3, 1.4**

### Property 3: Read Never Elevates

*For any* call to a read verb, the runner is invoked with
`elevated=False` regardless of `config.elevated`.

**Validates: Requirements 1.5, 4.3, 7.3, 10.5, 13.2**

### Property 4: Set Idempotency

*For any* `sshd_config` already containing `<keyword> <value>`,
`set_sshd_setting(keyword, value)` returns `SKIPPED` without invoking
`tee`.

**Validates: Requirement 2.2**

### Property 5: Set Replaces Existing Value

*For any* `sshd_config` containing `<keyword> <other-value>`,
`set_sshd_setting(keyword, value)` writes a rendered file in which
the keyword's value is `value`.

**Validates: Requirement 2.3**

### Property 6: Set Creates Missing File

*For any* call to `set_sshd_setting(keyword, value)` when the file
does not exist, the manager writes a single-line file with `<keyword>
<value>`.

**Validates: Requirement 2.4**

### Property 7: Remove Skipped When Absent

*For any* `sshd_config` not containing the keyword,
`remove_sshd_setting(keyword)` returns `SKIPPED`.

**Validates: Requirements 3.1, 3.2**

### Property 8: List Returns Top-Level Only

*For any* `sshd_config` with both top-level settings and a `Match`
block, `list_sshd_settings()` returns only the top-level settings.

**Validates: Requirement 4.2**

### Property 9: Add Match Block Idempotency

*For any* `sshd_config` already containing a `Match` block with
identical criteria and settings, `add_match_block(criteria, settings)`
returns `SKIPPED`.

**Validates: Requirement 5.2**

### Property 10: Add Match Block Replaces Settings

*For any* `sshd_config` containing a `Match` block with the matching
criteria but different settings, `add_match_block(criteria, settings)`
writes a rendered file in which the block's settings equal `settings`.

**Validates: Requirement 5.3**

### Property 11: Remove Match Block Idempotency

*For any* `sshd_config` without a matching `Match` block,
`remove_match_block(criteria)` returns `SKIPPED`.

**Validates: Requirement 6.1**

### Property 12: List Match Blocks Returns Declaration Order

*For any* `sshd_config` with multiple `Match` blocks,
`list_match_blocks()` returns them in declaration order.

**Validates: Requirement 7.2**

### Property 13: Reload Command Construction

*For any* call to `reload_sshd()`, the manager invokes `launchctl
kickstart -k system/com.openssh.sshd` via the runner.

**Validates: Requirement 8.1**

### Property 14: Reload Honours Domain Override

*For any* `service_domain` override (e.g. `"gui/501"`), the manager
substitutes the override into the launchctl target string.

**Validates: Requirement 8.6**

### Property 15: Test Config Command Construction

*For any* call to `test_sshd_config()`, the manager invokes `sshd -t`
via the runner.

**Validates: Requirement 9.1**

### Property 16: Test Config Failure Maps to FAILED

*For any* `sshd -t` invocation returning non-zero, the result has
`status=FAILED` with `stderr` populated.

**Validates: Requirement 9.3**

### Property 17: Authorized-Key Parser Recognises Common Types

*For any* `authorized_keys` line of the form `<key-type> <key-data>
[<comment>]` with `<key-type>` in the recognised set, the parser
populates `key_type`, `key_data`, and `comment` correctly.

**Validates: Requirement 10.2**

### Property 18: Authorized-Key Parser Captures Options

*For any* `authorized_keys` line beginning with options, the parser
captures the options prefix into the `options` field.

**Validates: Requirement 10.3**

### Property 19: Add Authorized Key Idempotency by Fingerprint

*For any* user with an existing entry whose fingerprint matches the
supplied key's fingerprint, `add_authorized_key(user, key)` returns
`SKIPPED`.

**Validates: Requirement 11.3**

### Property 20: Add Authorized Key Format Validation

*For any* `key` that does not parse as an OpenSSH `authorized_keys`
line, `add_authorized_key(user, key)` returns `FAILED` with
`message="key line failed to parse"` without invoking the runner.

**Validates: Requirement 11.2**

### Property 21: Remove Authorized Key By Fingerprint

*For any* user with multiple entries, `remove_authorized_key(user,
fingerprint)` writes a file in which no remaining line's fingerprint
equals the supplied fingerprint.

**Validates: Requirement 12.3**

### Property 22: Remove Authorized Key Idempotency

*For any* user without an entry matching the supplied fingerprint,
`remove_authorized_key(user, fingerprint)` returns `SKIPPED`.

**Validates: Requirement 12.2**

### Property 23: Dry-Run Safety

*For any* write verb with `dry_run=True`, the runner is never invoked
for the write command and the result status is `DRY_RUN`.

**Validates: Requirements 2.5, 3.4, 5.5, 6.4, 8.5, 9.4, 11.5, 12.4**

### Property 24: Elevation for Write Operations

*For any* write verb invoked with `config.elevated=True`, the runner
is invoked with `elevated=True` for the mutating command.

**Validates: Requirement 13.1**

### Property 25: Error Result Consistency

*For any* failed runner command, the returned
`MacosSshActionResult` has `status=FAILED` and contains the
`exit_code`, `stdout`, `stderr`, and `command` from the runner.

**Validates: Requirements 2.6, 3.5, 5.6, 8.4, 9.3, 11.6, 12.5**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── sshserverconfiguration.py   ← MacosSshServerConfigurationManager,
                                   config, models, exceptions, parser
                                   helpers

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_sshserverconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from automojo.sysman.macos.sshserverconfiguration import (
    MacosSshServerConfigurationManager,
    MacosSshServerConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosSshServerConfigurationManagerConfig(elevated=True)
mgr = MacosSshServerConfigurationManager(runner=runner, config=config)

# Disable root login
result = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
print(result.status)

# Validate, then re-execute the daemon
test_result = mgr.test_sshd_config()
if test_result.status.name == "SUCCESS":
    mgr.reload_sshd()
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import (
    SSHCommandRunner,
    SSHRunnerConfig,
)
from automojo.sysman.macos.aspects import (
    PersistenceAspect,
    PersistenceAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.sshserverconfiguration import (
    MacosSshServerConfigurationManager,
    MacosSshServerConfigurationManagerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="remote-mac", username="admin"),
)
runner.add_aspect(
    aspect=PersistenceAspect(
        PersistenceAspectConfig(
            storage_backend=SQLiteStorage(
                path="/var/log/maintenance/commands.db",
            ),
        ),
    ),
)

mgr = MacosSshServerConfigurationManager(
    runner=runner,
    config=MacosSshServerConfigurationManagerConfig(elevated=True),
)

mgr.set_sshd_setting(keyword="PasswordAuthentication", value="no")
mgr.reload_sshd()
```

### Dry-Run Mode

```python
config = MacosSshServerConfigurationManagerConfig(
    elevated=True, dry_run=True,
)
mgr = MacosSshServerConfigurationManager(runner=runner, config=config)

plan = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
assert plan.status.name == "DRY_RUN"
assert plan.command == ["tee", "/etc/ssh/sshd_config"]
```

---

## Testing Strategy

- All tests use a `_MockCommandRunner` keyed on argv tuple.
- `cat /etc/ssh/sshd_config` mock responses drive every read and
  every idempotency guard.
- `tee /etc/ssh/sshd_config` mock responses drive write outcomes.
- Authorized-key tests synthesise OpenSSH-shaped lines using a
  `_build_test_key()` helper that emits a deterministic ed25519
  body and the matching SHA-256 fingerprint, so tests assert
  fingerprint comparisons end-to-end.
- A dedicated test proves `reload_sshd` invokes `launchctl kickstart
  -k system/com.openssh.sshd` exactly — this is the principal
  macOS-vs-Linux divergence.
- A dedicated test proves the manager honours `elevated=True` on
  write paths and `elevated=False` on read paths regardless of
  config.
- A dedicated test proves `add_authorized_key` returns `FAILED` with
  a descriptive `message` when the key text fails to parse.
