# Requirements

## Introduction

The package-management feature provides the `automojo-sysman-macos` package
with the ability to manage Homebrew-managed software packages on macOS
systems.  This spec implements the package-management contract defined at
`automojo-sysman-common/specs/contracts/package-management/contract.md`
for macOS using `brew` as the primary package manager.  It covers
install / remove / upgrade / upgrade-all, installed-package queries, a
packaged listing, an upgradable listing (via `brew outdated`), local
installation from a `.pkg` / `.dmg` / cask, and metadata refresh.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface.  The feature
does not decide *what* to install — it provides the capability to do so
reliably, idempotently, and auditably.  All system interaction happens
through `ICommandRunner`; no direct `subprocess` call lives in the
manager.

Because Homebrew refuses to run as root (`brew` exits non-zero with
`Error: Running Homebrew as root is extremely dangerous and no longer
supported.`), the Homebrew bindings **never** pass `elevated=True`.  The
`MacosPackageManagerConfig` exposes a `brew_user` field that identifies
the unprivileged account the runner should execute `brew` as; when the
runner is local and already executing as that user, no wrapper is needed.
This is an extension beyond the base contract's elevation model and is
documented in the design.

---

## Requirements

### Requirement 1: Install a Package

**User Story:** As a maintenance coordinator, I want to install a Homebrew
formula at the latest version or at a pinned version, idempotently and
with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_package(name, version=None)` THE
   SYSTEM SHALL invoke `["brew", "install", name]` via the runner; with a
   supplied `version` THE SYSTEM SHALL invoke `["brew", "install",
   f"{name}@{version}"]`.
2. WHEN the formula is already installed at any version AND `version is
   None` THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the formula is already installed at a version matching `version`
   THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosPackageActionResult` with `status=SUCCESS`, the resolved
   `version`, and the argv captured in `command`.
5. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with
   captured `exit_code`, `stdout`, `stderr`, and `command`.
6. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
7. THE SYSTEM SHALL NEVER pass `elevated=True` to the runner for any
   `brew` invocation, regardless of config, because Homebrew refuses
   to run as root.

---

### Requirement 2: Remove a Package

**User Story:** As a maintenance coordinator, I want to remove an
installed Homebrew formula, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_package(name)` THE SYSTEM SHALL
   invoke `["brew", "uninstall", name]` via the runner.
2. WHEN the formula is not installed THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 3: Upgrade a Package

**User Story:** As a maintenance coordinator, I want to upgrade a single
Homebrew formula, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_package(name, version=None)` THE
   SYSTEM SHALL invoke `["brew", "upgrade", name]` via the runner.
2. WHEN the formula is not installed THE SYSTEM SHALL return
   `status=SKIPPED` with `message="package is not installed; nothing
   to upgrade"`.
3. WHEN the formula is already at the latest available version (per
   `brew outdated --json=v2`) THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN a `version` is supplied AND the installed version already
   matches THE SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Upgrade All Packages

**User Story:** As a maintenance coordinator, I want to upgrade every
installed formula to its latest version in one call.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_all()` THE SYSTEM SHALL invoke
   `["brew", "upgrade"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `MacosPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. THE SYSTEM SHALL NEVER skip `upgrade_all` — `brew upgrade` already
   no-ops when there is nothing to upgrade.

---

### Requirement 5: Query Installed State

**User Story:** As a maintenance coordinator, I want to know whether a
formula is installed and get its metadata without elevation.

#### Acceptance Criteria

1. WHEN the coordinator calls `is_installed(name)` THE SYSTEM SHALL
   invoke `["brew", "list", "--versions", name]` via the runner.
2. WHEN the command exits 0 AND stdout is non-empty THE SYSTEM SHALL
   return `True`.
3. WHEN the command exits non-zero OR stdout is empty THE SYSTEM SHALL
   return `False`.
4. WHEN the coordinator calls `get_package_info(name)` THE SYSTEM SHALL
   invoke `["brew", "info", "--json=v2", name]` and parse the
   `formulae[0]` entry to produce a `MacosPackageInfo(name=<name>,
   version=<installed_on_request version or linked_keg>,
   release="", arch=<current architecture or "any">,
   origin=<tap or "homebrew/core">)`.
5. WHEN the formula is unknown (`brew info` exits non-zero with
   `Error: No available formula with the name "<name>"`) THE SYSTEM
   SHALL return `None` WITHOUT raising.
6. WHEN `brew info` fails for any other reason THE SYSTEM SHALL raise
   `MacosPackageQueryError`.

---

### Requirement 6: List Installed Packages

**User Story:** As a maintenance coordinator, I want a uniform list of
installed formulae for inventory.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_installed()` THE SYSTEM SHALL
   invoke `["brew", "list", "--versions"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line
   (`<name> <version1> [<version2> ...]`) and return a list of
   `MacosPackageInfo` objects using the *last* version token as the
   active version.
3. WHEN the command fails THE SYSTEM SHALL raise
   `MacosPackageQueryError`.
4. WHEN the output is empty THE SYSTEM SHALL return an empty list.

---

### Requirement 7: List Upgradable Packages

**User Story:** As a maintenance coordinator, I want the list of
formulae with available upgrades.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_upgradable()` THE SYSTEM SHALL
   invoke `["brew", "outdated", "--json=v2"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL parse the
   `formulae[]` array and return a list of `MacosPackageUpdateInfo`
   with `name`, `installed_version` (the last element of
   `installed_versions`), `available_version` (`current_version`),
   and `arch="any"` (Homebrew does not emit a per-package arch in
   this output).
3. WHEN the command fails THE SYSTEM SHALL raise
   `MacosPackageQueryError`.
4. WHEN the `formulae[]` array is empty THE SYSTEM SHALL return an
   empty list.

---

### Requirement 8: Install From a Local File

**User Story:** As a maintenance coordinator, I want to install a cask
package from a local `.dmg` / `.pkg` via the cask subcommand.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_local(filepath)` AND the file
   exists THE SYSTEM SHALL invoke `["brew", "install", "--cask",
   filepath]` via the runner.
2. WHEN the file does NOT exist THE SYSTEM SHALL raise
   `MacosPackageFileNotFoundError` before invoking the runner.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   AFTER the file-existence check.

---

### Requirement 9: Refresh Metadata

**User Story:** As a maintenance coordinator, I want to refresh Homebrew
metadata before making decisions about available versions.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_metadata()` THE SYSTEM SHALL
   invoke `["brew", "update"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `MacosPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. THE SYSTEM SHALL NOT apply an idempotency guard on
   `refresh_metadata`.

---

### Requirement 10: Elevation and brew_user

**User Story:** As a maintenance coordinator, I want the manager to
respect Homebrew's prohibition against running as root.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER pass `elevated=True` to the runner for any
   `brew` invocation, regardless of `config.elevated`.
2. WHEN `config.elevated=True` is set THE SYSTEM SHALL emit a one-time
   warning on first use that the flag is ignored because Homebrew
   refuses to run as root.
3. WHEN `config.brew_user` is set AND the current effective user is
   not `brew_user` THE SYSTEM SHALL wrap the runner argv with
   `["sudo", "-u", <brew_user>, "-H", "--"]` so the runner still does
   not pass `elevated=True` but the `brew` subprocess executes as the
   intended user.  This `sudo -u` wrapper is runtime execution
   semantics and does NOT set `elevated=True` on the runner call.
4. WHEN `config.brew_user` is `None` AND the current effective user is
   root THE SYSTEM SHALL raise `MacosPackageQueryError` on the first
   `brew` dispatch with a message explaining that `brew_user` is
   required when running as root.
5. WHEN `config.assume_yes=True` (the default) THE SYSTEM SHALL
   include no extra flag — `brew` is non-interactive by default in
   its `install` / `upgrade` paths; the field is kept for contract
   conformance.
