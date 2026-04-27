# Design

## Overview

The repository management feature is implemented as a single module,
`automojo.sysman.macos.repositorymanagement`, that exposes a
`MacosRepositoryManager` class.  This spec implements the
repository-management contract defined at
`automojo-sysman-common/specs/contracts/repository-management/contract.md`
for macOS using Homebrew taps (`brew tap`, `brew untap`, `brew
update`, `brew tap-info`).

The coordinator instantiates `MacosRepositoryManager` and calls its
methods.  All command invocation is delegated to an `ICommandRunner`;
**no file operator is required** for macOS repository-management
because Homebrew's tap registry lives in the Homebrew prefix and the
manager does not edit it directly.

Homebrew taps are named `owner/repo` pairs.  Many contract fields
(GPG, mirror list, priority) are not applicable to Homebrew; the
manager accepts them with a documented warning and proceeds.  The
manager also surfaces the Homebrew quirk that `enable_repository` is
a no-op SKIPPED for present taps and `disable_repository` is a
documented FAILED because Homebrew has no soft-disable concept —
`remove_repository` (→ `brew untap`) is the closest equivalent.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosRepositoryManager               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           MacosRepositoryManager                            │
│  automojo.sysman.macos.repositorymanagement                 │
│                                                             │
│  add_repository()       remove_repository()                 │
│  enable_repository()    disable_repository()                │
│  list_repositories()    get_repository()                    │
│  refresh_repositories() set_priority()                      │
│  import_gpg_key()                                           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosRepositoryManager`

**Location:** `source/packages/automojo/sysman/macos/repositorymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_repository` | `(spec) → MacosRepositoryActionResult` | `brew tap <name> [<url>]` |
| `remove_repository` | `(name) → MacosRepositoryActionResult` | `brew untap <name>` |
| `enable_repository` | `(name) → MacosRepositoryActionResult` | No-op: SKIPPED if present, FAILED if absent |
| `disable_repository` | `(name) → MacosRepositoryActionResult` | No-op: FAILED "homebrew_no_disable" |
| `list_repositories` | `() → list[MacosRepositoryInfo]` | `brew tap` |
| `get_repository` | `(name) → MacosRepositoryInfo \| None` | Enumerate + filter |
| `refresh_repositories` | `(name=None) → MacosRepositoryActionResult` | `brew update` |
| `set_priority` | `(name, priority) → MacosRepositoryActionResult` | No-op SKIPPED (raises if unknown) |
| `import_gpg_key` | `(key_source) → MacosRepositoryActionResult` | No-op SKIPPED (raises for missing local file) |

### `MacosRepositoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | Accepted but unused; Homebrew refuses root |
| `repos_dir` | `str \| None` | `None` | Unused on macOS; contract-mandated field |
| `include_urls` | `bool` | `False` | When `True`, `list_repositories` also calls `brew tap-info --json <tap>` for each tap |

---

## Data Models

### Enums

```python
class MacosRepositoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### Dataclasses

```python
@dataclass
class MacosRepositorySpec:
    name: str                      # "owner/repo"
    display_name: str
    base_url: str | None           # optional custom URL for private taps
    mirror_list_url: str | None    # N/A — accepted with warning
    enabled: bool
    gpg_check: bool                # N/A — accepted with warning
    gpg_key: str | None            # N/A — accepted with warning
    priority: int | None           # N/A — accepted with warning
    channel: str | None            # N/A — accepted with warning

@dataclass
class MacosRepositoryInfo:
    name: str
    display_name: str
    enabled: bool
    url: str | None
    priority: int | None

@dataclass
class MacosRepositoryActionResult:
    status: MacosRepositoryActionStatus
    name: str
    file_path: str | None          # always None on macOS
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    message: str | None

class MacosRepositoryQueryError(Exception):
    """Raised when list_repositories fails unexpectedly."""

class MacosRepositoryNotFoundError(Exception):
    """Raised when set_priority targets an unknown tap."""

class MacosRepositoryKeyFileNotFoundError(Exception):
    """Raised when import_gpg_key is given a missing local file path."""
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

macOS does not need an `IFileOperator` for this feature.

---

## Repository-Name Mapping

| Contract field | macOS mapping | Notes |
|---|---|---|
| `name` | `owner/repo` Homebrew tap | e.g., `homebrew/cask`, `aws/tap`. |
| `display_name` | Informational only | Not persisted by Homebrew. |
| `base_url` | Optional 2nd arg to `brew tap` | For private/non-github taps. |
| `mirror_list_url` | N/A | Accepted with warning. |
| `enabled` | Always True once tapped | `disable` returns FAILED. |
| `gpg_check` | N/A | Homebrew verifies via HTTPS / Git signing. |
| `gpg_key` | N/A | Accepted with warning; `import_gpg_key` returns SKIPPED. |
| `priority` | N/A | `set_priority` returns SKIPPED or raises NotFound. |
| `channel` | N/A | Accepted with warning. |

---

## Command Mappings

### Write Commands (no elevation — Homebrew refuses root)

| Operation | Command |
|---|---|
| Add tap | `brew tap <name>` or `brew tap <name> <url>` |
| Remove tap | `brew untap <name>` |
| Refresh all | `brew update` |
| Refresh one | `brew update <name>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| List taps | `brew tap` |
| Tap URL (optional) | `brew tap-info --json <tap>` |

### No-op verbs (no runner invocation)

- `enable_repository` — SKIPPED when tap present, FAILED when absent.
- `disable_repository` — FAILED always (Homebrew has no disable).
- `set_priority` — SKIPPED (or raise `MacosRepositoryNotFoundError`).
- `import_gpg_key` — SKIPPED (or raise
  `MacosRepositoryKeyFileNotFoundError`).

---

## Output Parsing

### `brew tap` output

Each non-empty line is a tap name in `owner/repo` form.  The parser
strips trailing whitespace and skips empty lines.

### `brew tap-info --json <tap>` output

The JSON emitted by `brew tap-info --json` is a list of objects; the
parser takes the first object, reads the `remote` field (a URL), and
uses it as `MacosRepositoryInfo.url`.  Non-zero exit or absent
`remote` → `url=None`.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `add_repository` | Tap already in `brew tap` output | `brew tap` |
| `remove_repository` | Tap not in `brew tap` output | `brew tap` |
| `enable_repository` | N/A — always SKIPPED or FAILED | — |
| `disable_repository` | N/A — always FAILED | — |
| `set_priority` | N/A — always SKIPPED or NotFound | — |
| `import_gpg_key` | N/A — always SKIPPED or KeyFileNotFound | — |

`refresh_repositories` has no guard.  `list_repositories` /
`get_repository` are reads.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosRepositoryQueryError` | `brew tap` returns non-zero |
| `MacosRepositoryNotFoundError` | `set_priority` for an unknown tap |
| `MacosRepositoryKeyFileNotFoundError` | `import_gpg_key` with missing local file |

All other write failures are surfaced as
`MacosRepositoryActionResult` with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Add-Tap Command Construction

*For any* spec without `base_url`, `add_repository()` should construct `brew tap <name>` without elevation.

**Validates: Requirements 1.2**

### Property 2: Add-Tap With Custom URL Command Construction

*For any* spec with `base_url` set, `add_repository()` should construct `brew tap <name> <url>`.

**Validates: Requirements 1.1**

### Property 3: Add-Tap Idempotency

*For any* tap already present in `brew tap` output, `add_repository()` should return `SKIPPED` without invoking `brew tap`.

**Validates: Requirements 1.3**

### Property 4: Add-Tap N/A Field Warning

*For any* spec with `gpg_key`, `mirror_list_url`, or `priority` set, the manager should accept the spec and log a warning the field is not applicable to Homebrew.

**Validates: Requirements 1.6**

### Property 5: Remove-Tap Command Construction

*For any* `remove_repository(name)`, the manager should dispatch `brew untap <name>`.

**Validates: Requirements 2.1**

### Property 6: Remove-Tap Idempotency

*For any* `remove_repository(name)` where the tap is not in `brew tap` output, the result should be `SKIPPED`.

**Validates: Requirements 2.2**

### Property 7: Enable-Repository SKIPPED When Present

*For any* `enable_repository(name)` where the tap is present, the result should be `SKIPPED` with `message="homebrew_taps_always_enabled"`.

**Validates: Requirements 3.1**

### Property 8: Enable-Repository FAILED When Absent

*For any* `enable_repository(name)` where the tap is absent, the result should be `FAILED` with `message="repository_not_found"`.

**Validates: Requirements 3.2**

### Property 9: Enable-Repository Never Invokes Runner Mutations

*For any* `enable_repository()` call, the runner should not be invoked to mutate taps (only `brew tap` read to check presence).

**Validates: Requirements 3.3**

### Property 10: Disable-Repository FAILED Always

*For any* `disable_repository(name)`, the result should be `FAILED` with `message="homebrew_no_disable: untap_required"`.

**Validates: Requirements 4.1**

### Property 11: Disable-Repository Never Invokes Runner

*For any* `disable_repository()` call, the runner should not be invoked.

**Validates: Requirements 4.2**

### Property 12: List-Taps Command Construction

*For any* call to `list_repositories()`, the manager should invoke `brew tap` without elevation.

**Validates: Requirements 5.1**

### Property 13: List-Taps Parser

*For any* `brew tap` output, `list_repositories()` should return one entry per non-empty line with `enabled=True`, `url=None`, `priority=None`.

**Validates: Requirements 5.3**

### Property 14: List-Taps Failure Raises

*For any* non-zero exit from `brew tap`, `list_repositories()` should raise `MacosRepositoryQueryError`.

**Validates: Requirements 5.2**

### Property 15: List-Taps Optional URL Enrichment

*For any* `list_repositories()` call with `include_urls=True`, the manager should invoke `brew tap-info --json <tap>` for each tap and populate `url`.

**Validates: Requirements 5.4**

### Property 16: Get-Repository Returns None For Unknown

*For any* unknown name, `get_repository()` should return `None`.

**Validates: Requirements 6.2**

### Property 17: Refresh-All Command Construction

*For any* `refresh_repositories(name=None)`, the manager should dispatch `brew update`.

**Validates: Requirements 7.1**

### Property 18: Refresh-One Command Construction

*For any* `refresh_repositories(name=<name>)`, the manager should dispatch `brew update <name>`.

**Validates: Requirements 7.2**

### Property 19: Refresh No Idempotency Guard

*For any* `refresh_repositories()` call in non-dry-run mode, the runner should be invoked — no `SKIPPED`.

**Validates: Requirements 7.6**

### Property 20: Set-Priority Not-Found Raises

*For any* `set_priority(name, ...)` where the tap is absent, `MacosRepositoryNotFoundError` should be raised.

**Validates: Requirements 8.1**

### Property 21: Set-Priority SKIPPED When Tap Exists

*For any* `set_priority(name, ...)` where the tap exists, the result should be `SKIPPED` with `message="homebrew_has_no_priority_concept"`.

**Validates: Requirements 8.2**

### Property 22: Set-Priority Never Invokes Runner Mutations

*For any* `set_priority()` call, the runner should not be invoked to mutate (only read via `brew tap` for presence check).

**Validates: Requirements 8.3**

### Property 23: Import-Key Missing Local Raises

*For any* `import_gpg_key(local_path)` where the path does not exist, `MacosRepositoryKeyFileNotFoundError` should be raised.

**Validates: Requirements 9.1**

### Property 24: Import-Key SKIPPED Otherwise

*For any* `import_gpg_key` with a valid URL or extant local file, the result should be `SKIPPED` with `message="homebrew_taps_verified_via_https"`.

**Validates: Requirements 9.2**

### Property 25: Import-Key Never Invokes Runner

*For any* `import_gpg_key()` call, the runner should not be invoked.

**Validates: Requirements 9.3**

### Property 26: Elevation Never Set

*For any* method call, the runner should NEVER be invoked with `elevated=True`, regardless of the config.

**Validates: Requirements 10.1**

### Property 27: Elevation Ignored With Warning

*For any* construction where `elevated=True` is passed in the config, the manager should log a one-time warning at construction.

**Validates: Requirements 10.2**

### Property 28: Dry-Run Short-Circuit For Mutating Methods

*For any* `add_repository`, `remove_repository`, or `refresh_repositories` call with `dry_run=True`, the runner's write commands should not be invoked, and the result should be `DRY_RUN`.

**Validates: Requirements 1.7, 2.5, 7.5, 11.1**

### Property 29: Dry-Run Precondition Exceptions Still Raise

*For any* dry-run `import_gpg_key` with a missing local file, or dry-run `set_priority` against an unknown tap, the appropriate exception should still be raised.

**Validates: Requirements 11.4**

### Property 30: Dry-Run Still Reads For Idempotency

*For any* `add_repository` / `remove_repository` call in dry-run mode, the `brew tap` idempotency read should still execute.

**Validates: Requirements 11.3**

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── repositorymanagement.py    ← MacosRepositoryManager, config,
                                  models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_repositorymanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.repositorymanagement import (
    MacosRepositoryManager,
    MacosRepositoryManagerConfig,
    MacosRepositorySpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = MacosRepositoryManagerConfig(dry_run=False)
repo_mgr = MacosRepositoryManager(runner=runner, config=config)

# Add a tap
spec = MacosRepositorySpec(
    name="homebrew/cask",
    display_name="Homebrew Casks",
    base_url=None,
    mirror_list_url=None,
    enabled=True,
    gpg_check=False,
    gpg_key=None,
    priority=None,
    channel=None,
)
result = repo_mgr.add_repository(spec=spec)
print(f"Add: {result.status}")

# Add a tap with a custom URL
private_spec = MacosRepositorySpec(
    name="org-internal/tools",
    display_name="Private internal tools",
    base_url="git@github.com:org-internal/homebrew-tools.git",
    mirror_list_url=None,
    enabled=True,
    gpg_check=False,
    gpg_key=None,
    priority=None,
    channel=None,
)
repo_mgr.add_repository(spec=private_spec)

# Refresh
repo_mgr.refresh_repositories()

# List
for info in repo_mgr.list_repositories():
    print(f"{info.name} enabled={info.enabled}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.macos.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.macos.storage import SQLiteStorage
from automojo.sysman.macos.repositorymanagement import (
    MacosRepositoryManager,
    MacosRepositoryManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="mac-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=120.0)))

repo_mgr = MacosRepositoryManager(
    runner=runner,
    config=MacosRepositoryManagerConfig(),
)

repo_mgr.refresh_repositories()
```

### Dry-Run Mode

```python
config = MacosRepositoryManagerConfig(dry_run=True)
repo_mgr = MacosRepositoryManager(runner=runner, config=config)

result = repo_mgr.add_repository(spec=spec)
assert result.status == MacosRepositoryActionStatus.DRY_RUN
print(result.command)      # ["brew", "tap", "homebrew/cask"]
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner`.
- `brew tap` fixture strings drive idempotency-guard and
  `list_repositories` parser tests.
- Dry-run tests assert zero `brew tap` / `brew untap` / `brew
  update` mutating invocations.
- The `enable_repository` SKIPPED / FAILED behavior is tested with
  present and absent taps.
- The `disable_repository` always-FAILED behavior is tested.
- The `set_priority` raises-on-not-found branch is tested.
- The `import_gpg_key` raises-on-missing-local-file branch is
  tested.
- A dedicated test asserts the runner is NEVER invoked with
  `elevated=True` regardless of config.
- A dedicated test covers the optional `include_urls=True`
  enrichment path via `brew tap-info --json`.
- No test requires a live Homebrew installation.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.
Each correctness property is implemented as a property-based test
that:

- Generates random but valid inputs (tap names in `owner/repo`
  form, arbitrary URL strings).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: repository-management (macos), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | macOS implementation | Notes |
|---|---|---|
| `add_repository` | `brew tap <name> [<url>]` | N/A field warnings. |
| `remove_repository` | `brew untap <name>` | Idempotent. |
| `enable_repository` | No-op: SKIPPED or FAILED | Taps always enabled. |
| `disable_repository` | No-op: FAILED | Homebrew has no disable. |
| `list_repositories` | `brew tap` | Optional `brew tap-info --json` enrichment. |
| `get_repository` | Enumerate + filter | `None` on miss. |
| `refresh_repositories` | `brew update [<name>]` | No idempotency guard. |
| `set_priority` | No-op: SKIPPED (or NotFound) | No priority concept. |
| `import_gpg_key` | No-op: SKIPPED (or KeyFileNotFound) | HTTPS + Git signing. |

---

## Extensions beyond the contract

- **Documented no-op verbs.**  `enable_repository`,
  `disable_repository`, `set_priority`, and `import_gpg_key` are
  retained for cross-OS API uniformity but are deliberate no-ops
  that surface the Homebrew feature gap loudly via SKIPPED / FAILED
  messages.
- **Optional URL enrichment.**  `include_urls=True` triggers a
  `brew tap-info --json` per tap for diagnostics.  This is opt-in
  because it can be slow on machines with many taps.
- **N/A field warnings.**  Specs that set `gpg_key`,
  `mirror_list_url`, or `priority` are accepted with a one-time
  warning at construction or first-use time.
- **Non-elevated.**  The manager never passes `elevated=True`.  This
  is documented in the config and surfaces as a warning if the
  coordinator mistakenly enables it.

## Out of Scope

- **Homebrew bundle files** (Brewfile) as multi-tap specifications.
- **Formula-level operations** (handled by a separate package-management
  manager).
- **MacPorts / Fink / Nix** as alternative package managers.
- **Repository mirroring / local mirror hosting** — Homebrew has no
  first-class mirror concept.
- **Client-certificate authentication** — out of scope.
- **Signed-formula repository support** beyond Homebrew's default
  HTTPS + Git commit signing.
- **Mass-apply from a structured policy document** (YAML / TOML) —
  out of scope.
