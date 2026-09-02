# Requirements

## Introduction

The python-environment-management feature provides the `codecraftai-sysman-macos`
package with the ability to deploy Python virtual environments with a declared
dependency set on macOS hosts.  This spec implements the
python-environment-management contract defined at
`codecraftai-sysman-common/specs/contracts/python-environment-management/contract.md`.

Every venv / pip verb is inherited unchanged from
`ccai.sysman.common.pythonenvironmentmanagementbase.PythonEnvironmentManagementBase`
(see `codecraftai-sysman-common/specs/python-environment-management-base/`).  This
package contributes exactly two things:

1. The POSIX layout — `<venv>/bin/python`, `python3` launcher, `test -d` / `rm -rf`
   probes.
2. `ensure_prerequisites()`, which installs a Homebrew Python formula through the
   existing `MacosPackageManager` — inheriting its Homebrew never-run-as-root rule —
   rather than issuing `brew` itself.

The `/usr/bin/python3` shipped with macOS is a Command Line Tools stub; on a host
without the tools it prompts (GUI) or fails (SSH).  Fleet automation should set
`base_interpreter` to a Homebrew interpreter after `ensure_prerequisites()`.

---

## Requirements

### Requirement 1: Select the POSIX Layout

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `PosixPythonEnvironmentLayout` from `_build_layout()`.
2. THE SYSTEM SHALL resolve the environment interpreter to `<path>/bin/python` and
   the default base interpreter to `["python3"]`.

---

### Requirement 2: Ensure Homebrew Prerequisites

#### Acceptance Criteria

1. WHEN the coordinator calls `ensure_prerequisites()` THE SYSTEM SHALL install the
   `python` formula via `MacosPackageManager.install_package`, sharing this
   manager's runner.
2. THE SYSTEM SHALL construct the `MacosPackageManagerConfig` with this manager's
   `dry_run` and `elevated` flags and the config's `brew_user`.
3. THE SYSTEM SHALL NOT pass `elevated=True` to the runner for any `brew` call,
   regardless of `config.elevated` (inherited Homebrew inversion); WHEN `brew_user`
   is set THE SYSTEM SHALL wrap the argv with `sudo -u <brew_user>`.
4. WHEN `config.prerequisite_packages` is set (for example `["python@3.12"]`) THE
   SYSTEM SHALL use it; WHEN the `packages` argument is supplied THE SYSTEM SHALL
   use it in preference to both.
5. WHEN an install returns `FAILED` THE SYSTEM SHALL stop and return the results
   collected so far.
6. WHEN dry-run is active THE SYSTEM SHALL return `DRY_RUN` per formula and SHALL
   NOT dispatch `brew install`.

---

### Requirement 3: Accept a Plain Base Config

#### Acceptance Criteria

1. WHEN constructed with a plain `PythonEnvManagerConfig` THE SYSTEM SHALL behave as
   if `prerequisite_packages=None` and `brew_user=None`.

---

### Requirement 4: OS-Prefixed Aliases

#### Acceptance Criteria

1. THE SYSTEM SHALL export `MacosPythonEnvActionStatus`, `MacosPythonEnvActionResult`,
   `MacosPythonEnvDeployResult`, `MacosPythonEnvInfo`, `MacosPythonEnvQueryError`,
   `MacosPythonEnvSpec`, `MacosPythonPackageInfo`, `MacosPythonEnvManagerConfig`,
   and `MacosPythonEnvironmentManager`.
