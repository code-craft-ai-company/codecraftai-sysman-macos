# Design

## Overview

The package-management feature is implemented as a single module,
`ccai.sysman.macos.packagemanagement`, that exposes a
`MacosPackageManager` class.  This spec implements the package-management
contract defined at
`codecraftai-sysman-common/specs/contracts/package-management/contract.md`
for macOS using Homebrew (`brew`) as the primary package manager.  The
coordinator instantiates the manager with a command runner and a config,
then calls install / remove / upgrade verbs plus a read-only query
surface.

All system interaction happens through `ICommandRunner`.  Idempotency
guards rely on `brew list --versions` (is-installed check) and
`brew info --json=v2` / `brew outdated --json=v2` (version check and
upgrade availability).  No `IFileOperator` is required.

Homebrew refuses to run as root.  The manager therefore **never** passes
`elevated=True` to the runner.  Instead, the `MacosPackageManagerConfig`
exposes a `brew_user` field.  When the current process is running as
root, the manager wraps every argv with `sudo -u <brew_user> -H --` so
the `brew` subprocess executes as an unprivileged user while the runner
call itself remains at `elevated=False`.  This is an extension beyond
the base contract's elevation model.

The feature works identically whether commands execute locally (via
`MacosLocalCommandRunner`) or remotely over SSH (via
`MacosSSHCommandRunner`).

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
│  - Injects runner into MacosPackageManager                  │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              MacosPackageManager                            │
│  ccai.sysman.macos.packagemanagement                    │
│                                                             │
│  install_package()  remove_package()  upgrade_package()     │
│  upgrade_all()      is_installed()                          │
│  get_package_info() list_installed()                        │
│  list_upgradable()  install_local()                         │
│  refresh_metadata()                                         │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to (elevated=False always)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosPackageManager`

**Location:** `source/packages/ccai/sysman/macos/packagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_package` | `(name, version=None) → MacosPackageActionResult` | `brew install <name>[@<version>]` |
| `remove_package` | `(name) → MacosPackageActionResult` | `brew uninstall <name>` |
| `upgrade_package` | `(name, version=None) → MacosPackageActionResult` | `brew upgrade <name>` |
| `upgrade_all` | `() → MacosPackageActionResult` | `brew upgrade` |
| `is_installed` | `(name) → bool` | `brew list --versions <name>` |
| `get_package_info` | `(name) → MacosPackageInfo \| None` | `brew info --json=v2 <name>` |
| `list_installed` | `() → list[MacosPackageInfo]` | `brew list --versions` |
| `list_upgradable` | `() → list[MacosPackageUpdateInfo]` | `brew outdated --json=v2` |
| `install_local` | `(filepath) → MacosPackageActionResult` | `brew install --cask <filepath>` |
| `refresh_metadata` | `() → MacosPackageActionResult` | `brew update` |

### `MacosPackageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write dispatches |
| `elevated` | `bool` | `False` | Documented for contract conformance; always ignored — a warning fires if `True` |
| `assume_yes` | `bool` | `True` | Documented for contract conformance; `brew` is non-interactive by default |
| `brew_user` | `str \| None` | `None` | Unprivileged user to execute `brew` as when the process is root |

---

## Data Models

### Enum

```python
class MacosPackageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### Dataclasses

```python
@dataclass
class MacosPackageActionResult:
    status: MacosPackageActionStatus
    package_name: str
    version: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None


@dataclass
class MacosPackageInfo:
    name: str
    version: str
    release: str          # always "" — Homebrew has no release/revision split
    arch: str             # "arm64", "x86_64", or "any"
    origin: str | None    # tap name, e.g. "homebrew/core" or "homebrew/cask"


@dataclass
class MacosPackageUpdateInfo:
    name: str
    installed_version: str
    available_version: str
    arch: str             # always "any" — brew outdated does not emit arch


class MacosPackageQueryError(Exception):
    """Raised when list_installed, list_upgradable, or get_package_info fails unexpectedly."""


class MacosPackageFileNotFoundError(Exception):
    """Raised when install_local() is called with a non-existent path."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Command Mappings

### Write Commands (all run without `elevated=True`)

| Operation | argv |
|---|---|
| Install | `brew install <name>` |
| Install at version | `brew install <name>@<version>` |
| Remove | `brew uninstall <name>` |
| Upgrade | `brew upgrade <name>` |
| Upgrade all | `brew upgrade` |
| Install local (cask) | `brew install --cask <filepath>` |
| Refresh | `brew update` |

### Read Commands

| Operation | argv |
|---|---|
| Is installed | `brew list --versions <name>` |
| Package info | `brew info --json=v2 <name>` |
| List installed | `brew list --versions` |
| List upgradable | `brew outdated --json=v2` |

### `sudo -u` Wrapper

When the current effective user is root AND `config.brew_user` is set,
every argv above is prefixed with `["sudo", "-u", <brew_user>, "-H",
"--"]`.  The runner call itself is still made with `elevated=False`.
When the process is already running as `brew_user` (or any
non-root user), no wrapper is applied.

---

## Output Parsing

### `brew list --versions <name>` Output

```
wget 1.21.4
```

Space-separated: the first token is the name, the remaining tokens are
versions (most-recent last).  The parser takes the last token as the
"active" version.  A zero exit with non-empty stdout → installed;
non-zero exit or empty stdout → not installed.

### `brew info --json=v2 <name>` Output (truncated)

```json
{
  "formulae": [
    {
      "name": "wget",
      "full_name": "wget",
      "tap": "homebrew/core",
      "versions": {"stable": "1.21.4"},
      "installed": [
        {"version": "1.21.4", "installed_on_request": true}
      ]
    }
  ],
  "casks": []
}
```

Parser:
- If `formulae` is empty and stderr contains `Error: No available
  formula with the name`, return `None`.
- `name` → `formulae[0].name`.
- `version` → `formulae[0].installed[-1].version` when installed is
  non-empty, otherwise `formulae[0].versions.stable`.
- `arch` → `"any"` (brew does not emit per-package arch here).
- `origin` → `formulae[0].tap`.

### `brew list --versions` Output

```
git 2.43.0
python@3.12 3.12.1
wget 1.21.4
```

One formula per line.  `MacosPackageInfo.version` = last version token.

### `brew outdated --json=v2` Output

```json
{
  "formulae": [
    {
      "name": "curl",
      "installed_versions": ["8.4.0"],
      "current_version": "8.6.0",
      "pinned": false,
      "pinned_version": null
    }
  ],
  "casks": []
}
```

Parser iterates `formulae[]`, emitting one `MacosPackageUpdateInfo` per
entry (`installed_version` = last element of `installed_versions`).

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `install_package(name, None)` | `is_installed(name)` is `True` | already installed |
| `install_package(name, V)` | `get_package_info(name).version == V` | already at target version |
| `remove_package(name)` | `is_installed(name)` is `False` | already removed |
| `upgrade_package(name, None)` | `is_installed(name)` is `False` → SKIPPED; else `brew outdated` shows `name` not outdated | already up to date |
| `upgrade_package(name, V)` | installed version equals `V` | already at target version |
| `upgrade_all` | never skips | — |
| `install_local` | never skips (the coordinator signalled intent by supplying a file) | — |
| `refresh_metadata` | never skips (contract: operational) | — |

All idempotency probes go through read paths and do NOT apply the
`sudo -u` wrapper when the process is NOT root (probes are still
subject to the wrapper when root, so the wrapper is applied uniformly
to every argv).

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosPackageQueryError` | `list_installed`, `list_upgradable`, or `get_package_info` fails unexpectedly; also raised once when the process is root and `brew_user` is `None` |
| `MacosPackageFileNotFoundError` | `install_local()` called with a path that does not exist on disk |

All write failures surface as
`MacosPackageActionResult(status=FAILED, …)`.

---

## Correctness Properties

### Property 1: Install Command Construction

*For any* `install_package(name, None)` whose guard does not skip, the runner is invoked with `["brew", "install", name]`.

**Validates: Requirements 1.1**

### Property 2: Install Versioned Command Construction

*For any* `install_package(name, "1.2.3")` whose guard does not skip, the runner is invoked with `["brew", "install", "name@1.2.3"]`.

**Validates: Requirements 1.1**

### Property 3: Install Idempotency (Any Version)

*For any* already-installed formula, `install_package(name, None)` returns `SKIPPED` without dispatching the install command.

**Validates: Requirements 1.2**

### Property 4: Install Idempotency (Exact Version)

*For any* formula installed at version `V`, `install_package(name, V)` returns `SKIPPED` without dispatching the install command.

**Validates: Requirements 1.3**

### Property 5: Install Never Elevates

*For any* install invocation with `config.elevated=True`, the runner is still called with `elevated=False`.

**Validates: Requirements 10.1**

### Property 6: Remove Command Construction

*For any* `remove_package(name)` whose guard does not skip, the runner is invoked with `["brew", "uninstall", name]`.

**Validates: Requirements 2.1**

### Property 7: Remove Idempotency

*For any* formula not installed, `remove_package(name)` returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 8: Upgrade Command Construction

*For any* `upgrade_package(name, None)` whose guard does not skip, the runner is invoked with `["brew", "upgrade", name]`.

**Validates: Requirements 3.1**

### Property 9: Upgrade Not Installed Skipped

*For any* `upgrade_package(name)` where the formula is not installed, the result is `SKIPPED` with `message="package is not installed; nothing to upgrade"`.

**Validates: Requirements 3.2**

### Property 10: Upgrade Already Latest

*For any* formula not listed by `brew outdated`, `upgrade_package(name, None)` returns `SKIPPED`.

**Validates: Requirements 3.3**

### Property 11: Upgrade Already At Version

*For any* `upgrade_package(name, V)` where installed version equals `V`, the result is `SKIPPED`.

**Validates: Requirements 3.4**

### Property 12: Upgrade All Command

*For any* `upgrade_all()`, the runner is invoked with `["brew", "upgrade"]`.

**Validates: Requirements 4.1**

### Property 13: Upgrade All Never Skips

*For any* `upgrade_all()`, the idempotency guard does not skip.

**Validates: Requirements 4.5**

### Property 14: Is Installed True

*For any* formula whose `brew list --versions` exits 0 with non-empty stdout, `is_installed` returns `True`.

**Validates: Requirements 5.2**

### Property 15: Is Installed False

*For any* formula whose `brew list --versions` exits non-zero or has empty stdout, `is_installed` returns `False`.

**Validates: Requirements 5.3**

### Property 16: Package Info Parse

*For any* `brew info --json=v2` output with a non-empty `formulae[]` array, `get_package_info` returns a `MacosPackageInfo` with `name`, `version`, and `origin` set.

**Validates: Requirements 5.4**

### Property 17: Package Info Not Installed

*For any* `brew info` failure matching the "No available formula" pattern, `get_package_info` returns `None` without raising.

**Validates: Requirements 5.5**

### Property 18: Package Info Unexpected Error

*For any* other `brew info` failure, `MacosPackageQueryError` is raised.

**Validates: Requirements 5.6**

### Property 19: List Installed Parse

*For any* valid `brew list --versions` output, `list_installed` returns one `MacosPackageInfo` per line with the last version token.

**Validates: Requirements 6.2**

### Property 20: List Installed Error

*For any* failure of `brew list --versions`, `MacosPackageQueryError` is raised.

**Validates: Requirements 6.3**

### Property 21: List Upgradable Parse

*For any* valid `brew outdated --json=v2` output, `list_upgradable` returns one `MacosPackageUpdateInfo` per `formulae[]` entry with `arch="any"`.

**Validates: Requirements 7.2**

### Property 22: List Upgradable Empty

*For any* output where `formulae[]` is empty, `list_upgradable` returns `[]`.

**Validates: Requirements 7.4**

### Property 23: Install Local Missing File

*For any* `install_local(path)` where `path` does not exist, `MacosPackageFileNotFoundError` is raised and the runner is NOT invoked.

**Validates: Requirements 8.2**

### Property 24: Install Local Command

*For any* `install_local(path)` where the file exists and we are not in dry-run, the runner is invoked with `["brew", "install", "--cask", path]`.

**Validates: Requirements 8.1**

### Property 25: Install Local Dry-Run After File Check

*For any* `install_local(path)` in dry-run mode with a non-existent file, the result is still `MacosPackageFileNotFoundError`.

**Validates: Requirements 8.2, 8.5**

### Property 26: Refresh Metadata Command

*For any* `refresh_metadata()`, the runner is invoked with `["brew", "update"]`.

**Validates: Requirements 9.1**

### Property 27: Refresh Metadata Never Skips

*For any* `refresh_metadata()`, the idempotency guard does not skip.

**Validates: Requirements 9.5**

### Property 28: brew_user Wrapper When Root

*For any* write or read invocation where the effective user is root and `brew_user` is set, the dispatched argv is prefixed with `["sudo", "-u", <brew_user>, "-H", "--"]` and the runner call is made with `elevated=False`.

**Validates: Requirements 10.3**

### Property 29: Root Without brew_user Raises

*For any* first dispatch where the effective user is root and `brew_user` is `None`, the manager raises `MacosPackageQueryError` on the first invocation.

**Validates: Requirements 10.4**

### Property 30: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner is NOT invoked and the result status is `DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 3.7, 4.4, 8.5, 9.4**

---

## Module Layout

```
source/packages/ccai/sysman/macos/
├── __init__.py
└── packagemanagement.py       ← MacosPackageManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/macos/
    └── test_packagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.macos.runners import (
    MacosLocalCommandRunner, MacosLocalRunnerConfig,
)
from ccai.sysman.macos.packagemanagement import (
    MacosPackageManager, MacosPackageManagerConfig,
)

runner = MacosLocalCommandRunner(MacosLocalRunnerConfig())
config = MacosPackageManagerConfig(dry_run=False, brew_user=None)
pkg_mgr = MacosPackageManager(runner=runner, config=config)

refresh = pkg_mgr.refresh_metadata()
print(f"refresh: {refresh.status.value}")

result = pkg_mgr.install_package(name="wget", version=None)
print(f"install wget: {result.status.value} version={result.version}")
```

### SSH Execution (Running As Root, With brew_user)

```python
from ccai.sysman.macos.runners import (
    MacosSSHCommandRunner, MacosSSHRunnerConfig,
)

runner = MacosSSHCommandRunner(
    MacosSSHRunnerConfig(host="mac-ci-01", username="root"),
)
pkg_mgr = MacosPackageManager(
    runner=runner,
    config=MacosPackageManagerConfig(brew_user="brewuser"),
)
pkg_mgr.upgrade_all()
# Each argv is wrapped as:
#   ["sudo", "-u", "brewuser", "-H", "--", "brew", "upgrade"]
```

### Dry-Run Preview

```python
config = MacosPackageManagerConfig(dry_run=True)
pkg_mgr = MacosPackageManager(runner=runner, config=config)
result = pkg_mgr.remove_package(name="git")
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** keyed on argv prefix lets each test queue
   responses for `brew list --versions`, `brew info --json=v2`,
   `brew outdated --json=v2`, and the write commands separately.
2. One command-construction test per write verb asserts the exact
   argv.  Where `brew_user` is set under a simulated root process,
   the argv is asserted to include the `sudo -u` prefix.
3. Idempotency tests cover installed/not-installed,
   version-match/version-miss, and up-to-date/outdated scenarios.
4. `get_package_info` tests cover the installed path, the "No
   available formula" path (returns `None`), and an unexpected-error
   path (raises).
5. `list_installed` parsing is tested with a realistic multi-line
   `brew list --versions` output including versioned formulae such
   as `python@3.12`.
6. `list_upgradable` parsing is tested with a realistic
   `brew outdated --json=v2` payload.
7. `install_local` tests assert `MacosPackageFileNotFoundError` when
   the path is missing AND that the runner is not invoked in that
   case.
8. A dedicated test asserts `config.elevated=True` is ignored and
   emits a one-time warning.
9. A dedicated test asserts running-as-root without `brew_user`
   raises `MacosPackageQueryError` on first dispatch.
10. No test requires root access or a live `brew` installation.

---

## Scope Mapping

| Contract verb | macOS implementation |
|---|---|
| `install_package` | `brew install <name>[@<version>]` |
| `remove_package` | `brew uninstall <name>` |
| `upgrade_package` | `brew upgrade <name>` |
| `upgrade_all` | `brew upgrade` |
| `is_installed` | `brew list --versions <name>` |
| `get_package_info` | `brew info --json=v2 <name>` |
| `list_installed` | `brew list --versions` |
| `list_upgradable` | `brew outdated --json=v2` |
| `install_local` | `brew install --cask <filepath>` |
| `refresh_metadata` | `brew update` |

---

## Extensions Beyond the Contract

### Mac App Store / `mas` Integration

A future extension will add a companion `MacosMasPackageManager` that
honors the same contract surface but uses the `mas` CLI against signed-in
Apple IDs.  The two managers can be composed behind a selector.

### `brew_user` Root Avoidance

The contract's elevation model is "write-elevate, read-do-not-elevate".
Homebrew's hard refusal to run as root forces this implementation to
always pass `elevated=False` and instead use a `sudo -u` user-swap
wrapper when the process is already root.  This deviation is documented
here and enforced by Property 28 and Property 29.

### Cask Install From Local File

`install_local` maps to `brew install --cask` rather than treating the
file as a formula.  A future extension may add a `pkg_installer` flag to
route `.pkg` files to macOS's native `installer -pkg <file> -target /`
command when the caller knows the artifact is not a cask.
