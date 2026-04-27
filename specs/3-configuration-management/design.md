# Design

## Overview

The configuration management feature is implemented as a single module,
`automojo.sysman.macos.configurationmanagement`, that exposes a
`MacosConfigurationManager` class.  This spec implements the
configuration-management contract defined at
`automojo-sysman-common/specs/contracts/configuration-management/contract.md`
for macOS using Apple's `defaults` and `plutil` CLIs for plist-backed settings
and the `IFileOperator` abstraction for plain-text files under `/etc/`.

All content mutation is delegated to either the `ICommandRunner` (for plist
domains) or the `IFileOperator` (for plain text files).  Maintaining a
dual-abstraction dependency (runner + file operator) is an extension over the
contract — the contract allows but does not require `IFileOperator`.  This
package uses both because the native macOS configuration surface spans two
fundamentally different storage backends.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  When using
SSH, the coordinator can track reliability metrics and apply execution aspects
(persistence, retry, timeout) without any changes to the
`MacosConfigurationManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into MacosConfigurationManager              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        MacosConfigurationManager                            │
│  automojo.sysman.macos.configurationmanagement              │
│                                                             │
│  read_setting()     write_setting()                         │
│  remove_setting()   list_settings()                         │
│  write()            diff()                                  │
│  validate()         backup_configuration()                  │
│  restore_configuration()                                    │
│  set_permissions()  set_ownership()                         │
│  reload_configuration()                                     │
└────────────┬────────────────────────────┬───────────────────┘
             │ delegates to               │ delegates to
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   defaults / plutil /│         │   read_text / write  │
│   chmod / chown /    │         │   delete / exists    │
│   cp / launchctl /   │         │                      │
│   validator argv     │         │                      │
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

### `MacosConfigurationManager`

**Location:** `source/packages/automojo/sysman/macos/configurationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `read_setting` | `(source, key) → MacosSettingValue` | Parse source, return value |
| `write_setting` | `(source, key, value) → MacosConfigActionResult` | Mutate key |
| `remove_setting` | `(source, key) → MacosConfigActionResult` | Delete key |
| `list_settings` | `(source) → list[MacosSettingValue]` | Parse all keys |
| `write` | `(source, content, mode=None, owner=None, group=None) → MacosConfigActionResult` | Whole-source write |
| `diff` | `(source, desired_content) → MacosConfigDiffResult` | Unified diff |
| `validate` | `(source, validator_id) → MacosConfigValidationResult` | Run validator argv |
| `backup_configuration` | `(source) → MacosConfigActionResult` | Export plist / copy file |
| `restore_configuration` | `(backup_path) → MacosConfigActionResult` | Import plist / copy file |
| `set_permissions` | `(source, mode) → MacosConfigActionResult` | `chmod` (files only) |
| `set_ownership` | `(source, user=None, group=None) → MacosConfigActionResult` | `chown` (files only) |
| `reload_configuration` | `(service) → MacosConfigActionResult` | `launchctl kickstart -k` |

### `MacosConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

### `ValidatorRegistry`

```python
MACOS_VALIDATORS: dict[str, Callable[[str], list[str]]] = {
    "plist":      lambda path: ["plutil", "-lint", path],
    "sshd":       lambda path: ["sshd", "-t", "-f", path],
    "nginx":      lambda path: ["nginx", "-t", "-c", path],
    "apache":     lambda path: ["apachectl", "-t", "-f", path],
    "named":      lambda path: ["named-checkconf", path],
    "sudoers":    lambda path: ["visudo", "-c", "-f", path],
    "launchd":    lambda path: ["plutil", "-lint", path],
}
```

---

## Data Models

```python
class MacosConfigActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class MacosConfigActionResult:
    status: MacosConfigActionStatus
    source: str
    key: str | None
    backup_path: str | None
    diff: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class MacosSettingValue:
    source: str
    key: str
    value: str
    value_type: str | None    # "plist-string" / "plist-bool" / "plist-int" / "ini" / "yaml" / "shell"

@dataclass
class MacosConfigDiffResult:
    source: str
    has_diff: bool
    diff: str

@dataclass
class MacosConfigValidationResult:
    source: str
    validator_id: str
    valid: bool
    exit_code: int
    stdout: str
    stderr: str

@dataclass
class MacosConfigFileInfo:
    source: str
    exists: bool
    mode: str | None
    owner: str | None
    group: str | None
    size_bytes: int | None

class MacosConfigFileNotFoundError(Exception): ...
class MacosConfigTemplateError(Exception): ...
class MacosConfigValidatorNotFoundError(Exception): ...
class MacosConfigSettingNotFoundError(Exception): ...
class MacosConfigQueryError(Exception): ...
```

---

## Source Classification

`_classify_source(source)` returns one of `"plist-domain"`, `"plist-path"`,
`"file"`:

1. If `source.startswith("defaults:")` → `"plist-domain"` (strip prefix for
   actual domain).
2. If `source.endswith(".plist")` → `"plist-path"` (derive domain by stripping
   suffix and directory).
3. Otherwise → `"file"`.

---

## Command Mappings

### Read Commands (no elevation)

| Operation | Command / Call |
|---|---|
| `read_setting` — plist | `defaults read <domain> <key>` |
| `read_setting` — file | `IFileOperator.read_text(path=source)` + in-process parse |
| `list_settings` — plist | `defaults read <domain>` |
| `list_settings` — file | Same as `read_setting` + parse |
| `diff` — plist | `defaults export <domain> -` (stdout XML) |
| `diff` — file | `IFileOperator.read_text(path=source)` |
| `validate` | validator-specific argv |
| `stat` | `stat -f "%A %Su %Sg" <source>` |

### Write Commands (elevation-aware)

| Operation | Command / Call |
|---|---|
| `write_setting` — plist | `defaults write <domain> <key> -<type> <value>` |
| `write_setting` — file | `IFileOperator.write_text(path, rewritten, elevated=<cfg>)` |
| `remove_setting` — plist | `defaults delete <domain> <key>` |
| `remove_setting` — file | `IFileOperator.write_text(path, rewritten, elevated=<cfg>)` |
| `write` — file | `IFileOperator.write_text` plus optional `chmod`/`chown` |
| `write` — plist | `write_text` to temp file, then `defaults import <domain> <path>`, then `IFileOperator.delete` temp |
| `backup_configuration` — plist | `defaults export <domain> <backup_path>` |
| `backup_configuration` — file | `cp -p <source> <backup_path>` |
| `restore_configuration` — plist | `defaults import <domain> <backup_path>` |
| `restore_configuration` — file | `cp -p <backup_path> <original>` |
| `set_permissions` | `chmod <mode> <source>` (plists refused) |
| `set_ownership` | `chown <user>:<group> <source>` (plists refused) |
| `reload_configuration` | `launchctl kickstart -k <domain>/<service>` |

---

## `defaults` Value Type Inference

`_infer_plist_type(value)` returns the appropriate `-type` flag for
`defaults write`:

| Python input | Flag | Example rendered |
|---|---|---|
| `bool` | `-bool` | `YES` / `NO` |
| `int` | `-int` | `42` |
| `float` | `-float` | `3.14` |
| `list` | `-array` | space-separated or `-array-add` for append |
| `dict` | `-dict` | `<key> <val> ...` |
| `str` (default) | `-string` | the string |

When `value` is a `str` but matches `r"^-?\d+$"` or `r"^-?\d+\.\d+$"` the caller
MUST pass an explicit `value_type` to preserve string semantics; otherwise the
manager coerces to int/float.

---

## Output Parsing

### `defaults read <domain>` Output

For a flat plist, output is shell-like:

```
{
    AppleInterfaceStyle = Dark;
    "com.apple.springing.enabled" = 1;
    NSAutomaticCapitalizationEnabled = 0;
}
```

Parser uses `re.findall(r'\"?([\w.-]+)\"?\s*=\s*(.+?);', text)` to extract key-value
pairs; nested dicts are flattened with dotted keys via a recursive parser.

### `stat -f "%A %Su %Sg" <source>` Output

Example: `0644 root wheel`.  Parser splits on single space.

### Format Parsers (Pure Python)

Identical to Ubuntu: INI via `configparser`, YAML via `yaml.safe_load`, shell via
regex.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `write_setting` — plist | `defaults read <domain> <key>` equals desired | Runner + string compare |
| `write_setting` — file | Current parsed value equals desired | Parse + compare |
| `remove_setting` — plist | `defaults read` exits non-zero ("does not exist") | Runner exit-code |
| `remove_setting` — file | Key absent | Parse + compare |
| `write` | Current content equals desired | `read_text` or export + compare |
| `set_permissions` | Current mode equals desired | `stat -f` + compare |
| `set_ownership` | Current owner/group equal desired | `stat -f` + compare |
| `reload_configuration` | None — operational action | — |

---

## Backup Naming

- File sources: `<source>.bak.<YYYYMMDDTHHMMSS>`.
- Plist sources: `/var/backups/automojo/<domain>.<YYYYMMDDTHHMMSS>.plist`.

`_restore_route(backup_path)` pattern-matches the path to decide between
`defaults import` and `cp -p`.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosConfigFileNotFoundError` | `backup_configuration`, `restore_configuration`, `set_permissions`, `set_ownership` target a missing source |
| `MacosConfigTemplateError` | Template helper missing a variable |
| `MacosConfigValidatorNotFoundError` | Unknown validator id |
| `MacosConfigSettingNotFoundError` | `read_setting` missing key or source |
| `MacosConfigQueryError` | Any other read-path failure |

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of the system.*

### Property 1: `read_setting` Plist Command
*For any* plist source and key, `read_setting` should invoke
`defaults read <domain> <key>`.
**Validates: Requirements 1.1**

### Property 2: `read_setting` File Parse
*For any* plain-file source, `read_setting` should call `IFileOperator.read_text`
and parse using the format-appropriate parser.
**Validates: Requirements 1.3**

### Property 3: `read_setting` Missing Key
*For any* missing key, `read_setting` should raise
`MacosConfigSettingNotFoundError`.
**Validates: Requirements 1.4**

### Property 4: `read_setting` Missing Source
*For any* missing source, `read_setting` should raise
`MacosConfigSettingNotFoundError` with `detail="source missing"`.
**Validates: Requirements 1.5**

### Property 5: `write_setting` Plist Command
*For any* plist source and value, `write_setting` should invoke
`defaults write <domain> <key> -<type> <value>` where `<type>` comes from
`_infer_plist_type`.
**Validates: Requirements 2.1**

### Property 6: `write_setting` File Path
*For any* plain-file source, `write_setting` should call `write_text` with the
rewritten content.
**Validates: Requirements 2.2**

### Property 7: `write_setting` Idempotency
*For any* source where the current value equals the desired value,
`write_setting` should return `status=SKIPPED`.
**Validates: Requirements 2.3**

### Property 8: `write_setting` Dry-Run
*For any* call with `dry_run=True`, `write_setting` should return
`status=DRY_RUN` and not invoke the runner or file operator.
**Validates: Requirements 2.7**

### Property 9: `remove_setting` Plist Command
*For any* plist source, `remove_setting` should invoke
`defaults delete <domain> <key>`.
**Validates: Requirements 3.2**

### Property 10: `remove_setting` File Path
*For any* plain-file source, `remove_setting` should call `write_text` with the
key removed.
**Validates: Requirements 3.3**

### Property 11: `remove_setting` Idempotency
*For any* source where the key is absent, `remove_setting` should return
`status=SKIPPED`.
**Validates: Requirements 3.1**

### Property 12: `list_settings` Plist Parse
*For any* plist source, `list_settings` should invoke `defaults read <domain>`
and parse the output into a list of `MacosSettingValue` with dotted keys for
nested structures.
**Validates: Requirements 4.2**

### Property 13: `list_settings` File Parse
*For any* plain-file source, `list_settings` should parse the file into
`MacosSettingValue` entries preserving order.
**Validates: Requirements 4.3**

### Property 14: `list_settings` Missing Source
*For any* missing source, `list_settings` should return an empty list.
**Validates: Requirements 4.1**

### Property 15: `write` Plist Import Sequence
*For any* plist source, `write` should populate a temp file, invoke
`defaults import <domain> <tmp>`, and then delete the temp file.
**Validates: Requirements 5.4**

### Property 16: `write` File Idempotency
*For any* plain-file source where current content equals desired, `write` should
return `status=SKIPPED`.
**Validates: Requirements 5.2**

### Property 17: `diff` File
*For any* plain-file source, `diff` should compute the unified diff in pure
Python.
**Validates: Requirements 6.1**

### Property 18: `diff` Plist Export
*For any* plist source, `diff` should invoke `defaults export <domain> -` and
diff the stdout against the desired content.
**Validates: Requirements 6.2**

### Property 19: `validate` Unknown Id
*For any* unregistered id, `validate` should raise
`MacosConfigValidatorNotFoundError`.
**Validates: Requirements 7.4**

### Property 20: `validate` Valid Mapping
*For any* registered validator with exit 0, `validate` should return
`valid=True`.
**Validates: Requirements 7.2**

### Property 21: `backup_configuration` Plist Export
*For any* plist source, `backup_configuration` should invoke
`defaults export <domain> <backup_path>` where `<backup_path>` is under
`/var/backups/automojo/`.
**Validates: Requirements 8.2**

### Property 22: `backup_configuration` File Copy
*For any* plain-file source, `backup_configuration` should invoke
`cp -p <source> <backup_path>` with a timestamped path.
**Validates: Requirements 8.3**

### Property 23: `backup_configuration` Missing
*For any* missing source, `backup_configuration` should raise
`MacosConfigFileNotFoundError`.
**Validates: Requirements 8.1**

### Property 24: `restore_configuration` Routing
*For any* backup path, `restore_configuration` should pattern-match the path to
dispatch `defaults import` (plist) or `cp -p` (file).
**Validates: Requirements 9.1, 9.2**

### Property 25: `set_permissions` Plist Refusal
*For any* plist domain, `set_permissions` should return `status=FAILED` with a
descriptive detail and no runner call.
**Validates: Requirements 10.2**

### Property 26: `set_permissions` File Idempotency
*For any* plain-file source whose current mode matches, `set_permissions`
should return `status=SKIPPED`.
**Validates: Requirements 10.3**

### Property 27: `set_ownership` File Idempotency
*For any* plain-file source whose current owner/group matches, `set_ownership`
should return `status=SKIPPED`.
**Validates: Requirements 11.3**

### Property 28: `reload_configuration` Command
*For any* call, `reload_configuration` should invoke
`launchctl kickstart -k <domain>/<service>`.
**Validates: Requirements 12.1**

### Property 29: Elevation for Writes
*For any* write operation with `elevated=True`, the runner or file operator
should be invoked with `elevated=True`.
**Validates: Requirements 13.1**

### Property 30: No Elevation for Reads
*For any* read operation, neither the runner nor the file operator should be
invoked with `elevated=True`.
**Validates: Requirements 13.2**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── configurationmanagement.py   ← MacosConfigurationManager, config, models,
                                    exceptions, ValidatorRegistry

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_configurationmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.fileoperators import LocalFileOperator
from automojo.sysman.macos.configurationmanagement import (
    MacosConfigurationManager,
    MacosConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

cfg_mgr = MacosConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=MacosConfigurationManagerConfig(elevated=True, dry_run=False),
)

# Plist domain write
result = cfg_mgr.write_setting(
    source="defaults:com.apple.finder",
    key="ShowPathbar",
    value="YES",
)
print(f"Write plist setting: {result.status}")

# Plain-file write
result = cfg_mgr.write_setting(
    source="/etc/ssh/sshd_config",
    key="PermitRootLogin",
    value="no",
)
print(f"Write file setting: {result.status}")

# Validation
validation = cfg_mgr.validate(
    source="/Library/LaunchDaemons/com.example.myapp.plist",
    validator_id="plist",
)
print(f"plist valid: {validation.valid}")

reload_result = cfg_mgr.reload_configuration(service="system/com.example.myapp")
print(f"Reload: {reload_result.status}")
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
from automojo.sysman.macos.configurationmanagement import (
    MacosConfigurationManager, MacosConfigurationManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-mac", username="admin"))
storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
cfg_mgr = MacosConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=MacosConfigurationManagerConfig(elevated=True),
)

backup = cfg_mgr.backup_configuration(source="defaults:com.apple.softwareupdate")
print(f"Backup at: {backup.backup_path}")
```

### Dry-Run Mode

```python
cfg = MacosConfigurationManagerConfig(elevated=True, dry_run=True)
cfg_mgr = MacosConfigurationManager(runner=runner, file_operator=file_operator, config=cfg)

result = cfg_mgr.write_setting(
    source="defaults:com.apple.finder",
    key="ShowPathbar",
    value="NO",
)
assert result.status == MacosConfigActionStatus.DRY_RUN
print(result.command)  # would invoke defaults write
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned `defaults
   read`, `defaults export`, `plutil -lint`, and `stat -f` stdout.
2. `MockFileOperator` backed by an in-memory `dict[str, str]` drives plain-file
   idempotency tests.
3. `_infer_plist_type` coverage test iterates every Python input type and
   asserts the chosen flag.
4. `_classify_source` test matrix asserts every prefix/suffix rule.
5. `list_settings` plist test uses a representative `defaults read` output with
   nested dicts; the parser must flatten to dotted keys and preserve order.
6. `backup_configuration` plist path test monkey-patches `datetime.utcnow` and
   asserts the computed backup path under `/var/backups/automojo/`.
7. Plist refusal tests for `set_permissions` / `set_ownership` assert no runner
   call occurred and `status=FAILED` with a descriptive `detail`.
8. Dry-run matrix test iterates every write verb and asserts no mutating
   runner/file-operator call.
9. Elevation matrix test asserts `elevated=True` flows to writes and is absent
   for reads.
10. No test requires root access or a live macOS host.

---

## Scope Mapping

| Abstract | macOS |
|---|---|
| Configuration source (plist path) | `/Library/Preferences/<domain>.plist`, `~/Library/Preferences/<domain>.plist` |
| Configuration source (plist domain) | `defaults:<domain>` (e.g., `defaults:com.apple.finder`) |
| Configuration source (file) | Absolute path under `/etc/` |
| Reload mechanism | `launchctl kickstart -k <domain>/<service>` |
| File backup naming | `<source>.bak.<YYYYMMDDTHHMMSS>` |
| Plist backup naming | `/var/backups/automojo/<domain>.<YYYYMMDDTHHMMSS>.plist` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for plain-file I/O and
  `ICommandRunner` for plist + permission + validator calls.
- **Plist/file routing.**  `_classify_source` picks the right storage backend
  from the source string.
- **Plist refusal for `set_permissions` / `set_ownership`.**  Plist domains
  don't have file modes; these verbs return `FAILED` with a descriptive
  `detail` rather than a confusing runner error.
- **`launchctl kickstart -k` for reload.**  macOS analogue to
  `systemctl reload`; the service identifier includes its launchd domain.
