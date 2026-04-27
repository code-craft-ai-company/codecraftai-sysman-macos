# Design

## Overview

The environment configuration feature is implemented as a single module,
`automojo.sysman.macos.environmentconfiguration`, that exposes a
`MacosEnvironmentConfigurator` class.  This spec implements the
environment-configuration contract defined at
`automojo-sysman-common/specs/contracts/environment-configuration/contract.md`
for macOS.

All runtime operations go through the `ICommandRunner` (via `launchctl setenv`,
`launchctl unsetenv`, `launchctl getenv`, `launchctl export`, `launchctl
asuser`), and all persistent file I/O goes through the `IFileOperator`
(`/etc/launchd.conf`, `/etc/paths.d/automojo`, `~<user>/.zshenv`).  Maintaining
a dual-abstraction dependency (runner + file operator) is an extension over
the contract — the contract explicitly permits `ICommandRunner` alongside
`IFileOperator` where the OS requires a CLI (`launchctl`).

`pwd.getpwnam` is called directly in a single private helper for home+uid
resolution.  No `subprocess`, no direct `open`, and no direct `pathlib` calls
are made elsewhere.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into MacosEnvironmentConfigurator           │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        MacosEnvironmentConfigurator                         │
│  automojo.sysman.macos.environmentconfiguration             │
│                                                             │
│  set_system_env()    unset_system_env()                     │
│  get_system_env()    list_system_env()                      │
│  set_user_env()      unset_user_env()                       │
│  get_user_env()      list_user_env()                        │
└────────────┬────────────────────────────┬───────────────────┘
             │ runtime via                │ persistence via
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   launchctl setenv / │         │   read_text / write  │
│   unsetenv / getenv /│         │   delete / exists    │
│   export / asuser    │         │                      │
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

### `MacosEnvironmentConfigurator`

**Location:** `source/packages/automojo/sysman/macos/environmentconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `set_system_env` | `(name, value) → MacosEnvActionResult` | `launchctl setenv` + `/etc/launchd.conf` + `/etc/paths.d/automojo` when PATH |
| `unset_system_env` | `(name) → MacosEnvActionResult` | `launchctl unsetenv` + rewrite `/etc/launchd.conf` + delete PATH drop-in |
| `get_system_env` | `(name) → MacosEnvValue \| None` | `launchctl getenv` first, fallback to file parse |
| `list_system_env` | `() → dict[str, str]` | `launchctl export` merged with file parse |
| `set_user_env` | `(user, name, value) → MacosEnvActionResult` | `launchctl asuser ... setenv` + `~/.zshenv` |
| `unset_user_env` | `(user, name) → MacosEnvActionResult` | `launchctl asuser ... unsetenv` + `~/.zshenv` rewrite |
| `get_user_env` | `(user, name) → MacosEnvValue \| None` | `launchctl asuser ... getenv` first, fallback to `.zshenv` |
| `list_user_env` | `(user) → dict[str, str]` | Merged runtime + `.zshenv` |

### `MacosEnvironmentConfiguratorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, system-scope writes and other-user writes pass `elevated=True` |

---

## Data Models

```python
class MacosEnvActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class MacosEnvScope(Enum):
    SYSTEM = "system"
    USER   = "user"

@dataclass
class MacosEnvValue:
    name: str
    value: str
    scope: MacosEnvScope
    source: str | None     # "launchctl" / "launchctl-user" / file path

@dataclass
class MacosEnvActionResult:
    status: MacosEnvActionStatus
    scope: MacosEnvScope
    name: str
    value: str | None
    path: str | None
    user: str | None
    message: str | None

class MacosEnvConfigError(Exception):
    """Raised when a user-scope read targets an unknown user or a source is unreadable."""
```

---

## Interfaces

### `ICommandRunner` and `IFileOperator`

Both are imported from `automojo-interfaces`.  The configurator takes one of
each in its constructor.

---

## Scope Mapping

| Abstract | macOS backing storage |
|---|---|
| `SYSTEM` runtime | `launchctl setenv/unsetenv/getenv` (system domain) |
| `SYSTEM` persistent | `/etc/launchd.conf` (`setenv NAME "value"` lines) |
| `SYSTEM` persistent (PATH only) | `/etc/paths.d/automojo` (one entry per line) |
| `USER` runtime | `launchctl asuser <uid> launchctl setenv/unsetenv/getenv` (user domain) |
| `USER` persistent | `~<user>/.zshenv` (`export NAME="value"` lines) |

---

## File Format Conventions

### `/etc/launchd.conf`

Each managed variable is `setenv NAME "value"` on its own line.  Values
containing `"` are escaped as `\"`.  Empty final newline preserved.

### `/etc/paths.d/automojo`

One absolute path per line.  No shebang, no `export`.  When the `PATH`
variable is unset at system scope this drop-in file is deleted.

### `~<user>/.zshenv`

Each managed variable is `export NAME="value"`.  Values containing `"`
escaped as `\"`.  When the last managed variable is unset the file is
**not** deleted (it may contain non-managed lines; we preserve whole-file
shape).

---

## Parsing

### `_parse_launchd_conf(text)`

1. Split by line; strip; skip blank and `#`-leading lines.
2. Match `r'^setenv\s+([A-Za-z_][A-Za-z0-9_]*)\s+"(.*)"\s*$'`.
3. Unescape `\"` → `"`.
4. Return `dict[str, str]`.

### `_parse_export_env(text)` (shared with Ubuntu shape)

Same as the Ubuntu `_parse_export_env`: `^export NAME="value"$` lines with
quote handling.

### `_parse_launchctl_export(text)` (stdout of `launchctl export`)

`launchctl export` emits lines like:

```
setenv JAVA_HOME "/usr/lib/jvm/java-11";
setenv PATH "/usr/local/bin:/usr/bin";
```

Parser strips the trailing `;`, matches each line as `setenv NAME "value"`,
and returns a dict.

---

## Line Update Strategy

Same as Ubuntu: read, `_apply_change`, write.  When `_apply_change` returns
unchanged lines the caller emits `SKIPPED`.

---

## User Home + UID Resolution

```python
def _resolve_user(username: str) -> tuple[str, int]:
    import pwd
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        raise MacosEnvConfigError(f"User not found: {username}")
    return entry.pw_dir, entry.pw_uid
```

User-scope writes wrap this call to convert `MacosEnvConfigError` into a
`FAILED` result, per contract.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `set_system_env` | Runtime + persistent already match | `launchctl getenv` + parse `/etc/launchd.conf` |
| `unset_system_env` | Variable absent from both | Same |
| `set_user_env` | Runtime + `.zshenv` already match | `launchctl asuser ... getenv` + parse `.zshenv` |
| `unset_user_env` | Variable absent from both | Same |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosEnvConfigError` | `get_user_env` / `list_user_env` called with unknown user or unreadable source |

For user-scope writes, unknown-user is surfaced as
`MacosEnvActionResult(status=FAILED, message="user not found: <user>")` —
not raised — per contract.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of the system.*

### Property 1: `set_system_env` Runtime + Persistent
*For any* `(name, value)`, `set_system_env` should invoke
`launchctl setenv <name> <value>` AND write a `setenv` line in
`/etc/launchd.conf`.
**Validates: Requirements 1.1**

### Property 2: `set_system_env` PATH Drop-In
*For any* `(name="PATH", value)`, `set_system_env` should additionally write
the value (one path per line) to `/etc/paths.d/automojo`.
**Validates: Requirements 1.2**

### Property 3: `set_system_env` Idempotency
*For any* `(name, value)` where runtime + persistent both match,
`set_system_env` should return `status=SKIPPED`.
**Validates: Requirements 1.3**

### Property 4: `set_system_env` Creates Missing Conf
*For any* system write where `/etc/launchd.conf` does not exist,
`set_system_env` should create it with the single `setenv` line.
**Validates: Requirements 1.4**

### Property 5: `set_system_env` Dry-Run
*For any* call with `dry_run=True`, `set_system_env` should return
`status=DRY_RUN` without invoking the runner or file operator's mutating
methods.
**Validates: Requirements 1.5, 10.1**

### Property 6: `set_system_env` Elevation
*For any* call with `elevated=True`, `set_system_env` should pass
`elevated=True` to the runner and every `write_text` call.
**Validates: Requirements 1.6, 9.1**

### Property 7: `unset_system_env` Idempotency
*For any* variable absent from both runtime and persistent, `unset_system_env`
should return `status=SKIPPED`.
**Validates: Requirements 2.1**

### Property 8: `unset_system_env` Runtime Remove
*For any* variable present at runtime, `unset_system_env` should invoke
`launchctl unsetenv <name>`.
**Validates: Requirements 2.2**

### Property 9: `unset_system_env` Conf Rewrite
*For any* variable present in `/etc/launchd.conf`, `unset_system_env` should
rewrite the file with the matching line removed.
**Validates: Requirements 2.3**

### Property 10: `unset_system_env` PATH Drop-In Delete
*For any* unset of `PATH` where `/etc/paths.d/automojo` exists,
`unset_system_env` should delete the drop-in via
`IFileOperator.delete`.
**Validates: Requirements 2.4**

### Property 11: `get_system_env` Runtime First
*For any* variable present at runtime, `get_system_env` should return the
runtime value with `source="launchctl"`.
**Validates: Requirements 3.1**

### Property 12: `get_system_env` Persistent Fallback
*For any* variable absent from runtime but present in `/etc/launchd.conf`,
`get_system_env` should return the persistent value.
**Validates: Requirements 3.2**

### Property 13: `get_system_env` Absent
*For any* variable absent from both sources, `get_system_env` should return
`None`.
**Validates: Requirements 3.3**

### Property 14: `list_system_env` Merge
*For any* call, `list_system_env` should merge `launchctl export` with
`/etc/launchd.conf` using runtime precedence on conflict.
**Validates: Requirements 4.1, 4.3**

### Property 15: `list_system_env` Empty Sources
*For any* call where both sources are empty, `list_system_env` should return
an empty dict.
**Validates: Requirements 4.4**

### Property 16: `set_user_env` Unknown User
*For any* unknown user, `set_user_env` should return `status=FAILED` with
`message="user not found: <user>"` and not invoke runner or file operator.
**Validates: Requirements 5.1**

### Property 17: `set_user_env` Runtime + Persistent
*For any* known user, `set_user_env` should invoke
`launchctl asuser <uid> launchctl setenv <name> <value>` AND write an
`export` line in `<home>/.zshenv`.
**Validates: Requirements 5.3**

### Property 18: `set_user_env` Creates Missing zshenv
*For any* known user where `.zshenv` does not exist, `set_user_env` should
create it with the export line.
**Validates: Requirements 5.4**

### Property 19: `set_user_env` Idempotency
*For any* known user where both runtime and persistent match, `set_user_env`
should return `status=SKIPPED`.
**Validates: Requirements 5.5**

### Property 20: `set_user_env` Same-User Elevation Suppression
*For any* call where the target user equals the current user, `set_user_env`
should pass `elevated=False` even when config has `elevated=True`.
**Validates: Requirements 5.7, 9.3**

### Property 21: `unset_user_env` Unknown User
*For any* unknown user, `unset_user_env` should return `status=FAILED` with
a descriptive message.
**Validates: Requirements 6.1**

### Property 22: `unset_user_env` Idempotency
*For any* known user where neither source has the variable,
`unset_user_env` should return `status=SKIPPED`.
**Validates: Requirements 6.2**

### Property 23: `unset_user_env` Runtime Remove
*For any* known user with the variable at runtime, `unset_user_env` should
invoke `launchctl asuser <uid> launchctl unsetenv <name>`.
**Validates: Requirements 6.3**

### Property 24: `unset_user_env` zshenv Rewrite
*For any* known user with the variable in `.zshenv`, `unset_user_env` should
rewrite the file with the line removed.
**Validates: Requirements 6.4**

### Property 25: `get_user_env` Unknown User
*For any* unknown user, `get_user_env` should raise `MacosEnvConfigError`.
**Validates: Requirements 7.1**

### Property 26: `get_user_env` Runtime First
*For any* known user with the variable at runtime, `get_user_env` should
return `source="launchctl-user"`.
**Validates: Requirements 7.2**

### Property 27: `get_user_env` zshenv Fallback
*For any* known user where runtime is empty but `.zshenv` has the variable,
`get_user_env` should return the parsed `.zshenv` value.
**Validates: Requirements 7.3**

### Property 28: `list_user_env` Unknown User
*For any* unknown user, `list_user_env` should raise `MacosEnvConfigError`.
**Validates: Requirements 8.1**

### Property 29: `list_user_env` Empty
*For any* known user with no runtime export and no `.zshenv`, `list_user_env`
should return an empty dict.
**Validates: Requirements 8.2**

### Property 30: No Elevation for Reads
*For any* read operation, neither the runner nor the file operator should be
invoked with `elevated=True`.
**Validates: Requirements 9.5**

### Property 31: Dry-Run Reads Still Run
*For any* call with `dry_run=True`, the runner's `getenv` and the file
operator's `read_text` should still be invoked so that guard logic can
distinguish `SKIPPED` from `DRY_RUN`.
**Validates: Requirements 10.3**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── environmentconfiguration.py   ← MacosEnvironmentConfigurator, config,
                                     models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_environmentconfiguration.py
```

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.fileoperators import LocalFileOperator
from automojo.sysman.macos.environmentconfiguration import (
    MacosEnvironmentConfigurator,
    MacosEnvironmentConfiguratorConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

env_cfg = MacosEnvironmentConfigurator(
    runner=runner,
    file_operator=file_operator,
    config=MacosEnvironmentConfiguratorConfig(elevated=True, dry_run=False),
)

result = env_cfg.set_system_env(name="JAVA_HOME", value="/usr/libexec/java_home-21")
print(f"Set system env: {result.status}")

result = env_cfg.set_user_env(user="myron", name="EDITOR", value="vim")
print(f"Set user env: {result.status}")
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
from automojo.sysman.macos.environmentconfiguration import (
    MacosEnvironmentConfigurator, MacosEnvironmentConfiguratorConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))
storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
env_cfg = MacosEnvironmentConfigurator(
    runner=runner,
    file_operator=file_operator,
    config=MacosEnvironmentConfiguratorConfig(elevated=True),
)

env_cfg.set_system_env(name="LANG", value="en_US.UTF-8")
```

### Dry-Run Mode

```python
cfg = MacosEnvironmentConfiguratorConfig(elevated=True, dry_run=True)
env_cfg = MacosEnvironmentConfigurator(runner=runner, file_operator=file_operator, config=cfg)

result = env_cfg.set_system_env(name="DANGEROUS_VAR", value="value")
assert result.status == MacosEnvActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockFileOperator` backed by an in-memory `dict[str, str]` drives every
   file read/write/delete path.  `MockCommandRunner` keyed on command args
   prefix returns canned `launchctl getenv`, `launchctl export`,
   `launchctl setenv`, `launchctl unsetenv`, and `launchctl asuser` responses.
2. `_parse_launchd_conf` and `_parse_launchctl_export` parser tests cover
   representative multi-line outputs including blank lines, comments, and
   embedded quotes.
3. `_resolve_user` is tested by monkey-patching `pwd.getpwnam`.
4. PATH drop-in tests assert that `/etc/paths.d/automojo` is written when
   setting `PATH` and deleted when unsetting.
5. Runtime-first read tests prove that `launchctl getenv` is consulted before
   the persistent file; persistent-fallback tests use a non-zero `getenv`
   response.
6. Same-user elevation suppression test monkey-patches the current-user
   helper and asserts `elevated=False` regardless of config.
7. Dry-run matrix test iterates every write verb and asserts zero mutating
   runner or file-operator calls while reads still occur.
8. Elevation matrix test asserts `elevated=True` flows to system-scope writes
   and to other-user writes, and is absent from same-user writes and all
   reads.
9. Unknown-user tests for each of `set_user_env`, `unset_user_env`,
   `get_user_env`, `list_user_env` assert the correct contract-defined
   behaviour.
10. No test requires root access or a live macOS host.

---

## Scope Mapping

| Abstract | macOS |
|---|---|
| `SYSTEM` runtime | `launchctl setenv/unsetenv/getenv` (system domain) |
| `SYSTEM` persistent | `/etc/launchd.conf` + `/etc/paths.d/automojo` for `PATH` |
| `USER` runtime | `launchctl asuser <uid> launchctl setenv/unsetenv/getenv` |
| `USER` persistent | `~<user>/.zshenv` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `ICommandRunner` for runtime values
  and `IFileOperator` for persistent files.  Contract explicitly permits this
  for OSes whose native surface requires a CLI.
- **Dual-source SYSTEM scope.**  Writes to both `launchctl` (runtime) and
  `/etc/launchd.conf` (persistence) so the value survives a reboot even on
  systems where `/etc/launchd.conf` is no longer consulted at every boot.
- **PATH-specific drop-in.**  Maintains `/etc/paths.d/automojo` in lock-step
  with `launchctl setenv PATH`.
- **Same-user elevation suppression.**  Writes targeting the current user
  never pass `elevated=True`, even when the config says so.
