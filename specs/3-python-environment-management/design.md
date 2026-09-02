# Design

## Overview

`ccai.sysman.macos.pythonenvironmentmanagement` is a thin subclass of the shared
`PythonEnvironmentManagementBase`.  Every venv and pip verb, the deploy algorithm,
the requirement probe, and the read surface are documented in
`codecraftai-sysman-common/specs/python-environment-management-base/design.md`.
This document covers only what macOS adds.

---

## Architecture

```
MacosPythonEnvironmentManager
├── _build_layout()           → PosixPythonEnvironmentLayout
├── ensure_prerequisites()    → MacosPackageManager.install_package(formula) × N
│                                (same runner; brew never elevated; sudo -u brew_user wrap)
└── (inherited)               → create_environment, remove_environment,
                                install_packages, install_requirements,
                                uninstall_packages, upgrade_pip, deploy, reads
```

---

## Components

### `MacosPythonEnvManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| *(contract fields)* | — | — | inherited from `PythonEnvManagerConfig` |
| `prerequisite_packages` | `list[str] \| None` | `None` → `["python"]` | Homebrew formulae for `ensure_prerequisites` |
| `brew_user` | `str \| None` | `None` | Forwarded to `MacosPackageManagerConfig.brew_user` |

### `MacosPythonEnvironmentManager`

| Method | Description |
|---|---|
| `_build_layout()` | Returns `PosixPythonEnvironmentLayout()` |
| `ensure_prerequisites(packages=None)` | Installs each formula via `MacosPackageManager`; stops at first `FAILED` |
| `_resolve_prerequisite_packages(packages)` | argument > config > default |
| `_build_package_manager()` | Builds `MacosPackageManager(runner, MacosPackageManagerConfig(dry_run, elevated, brew_user))` |
| `_typed_config()` | Upgrades a plain `PythonEnvManagerConfig` |

---

## Command Mappings

| Step | Command (through the runner) | Elevated |
|---|---|---|
| Prerequisite probe | `[sudo -u <brew_user> --] brew list --versions <formula>` | never |
| Prerequisite install | `[sudo -u <brew_user> --] brew install <formula>` | **never** (Homebrew inversion) |
| Create venv | `<base_interpreter> -m venv [--system-site-packages] <path>` | `config.elevated` |
| Existence probe | `test -d <path>` | never |
| Validity probe | `<path>/bin/python -c "import platform; print(platform.python_version())"` | never |
| pip (any) | `<path>/bin/python -m pip --disable-pip-version-check --no-input …` | writes only |
| Remove | `rm -rf <path>` | `config.elevated` |

Interpreter selection: after `ensure_prerequisites()` the Homebrew interpreter is
at `/opt/homebrew/bin/python3` (Apple silicon) or `/usr/local/bin/python3`
(Intel).  The default `["python3"]` launcher resolves whichever is first on the
runner's `PATH`; SSH sessions frequently do not include the Homebrew prefix, so
coordinators SHOULD set `base_interpreter` explicitly.

---

## Correctness Properties

1. **Layout.** `manager.layout` is a `PosixPythonEnvironmentLayout`.
2. **Prerequisite default.** `brew install python`, never elevated even with
   `config.elevated=True`.
3. **brew_user wrap.** With `brew_user="ops"` the argv begins `sudo -u ops` and ends
   `brew install <formula>`.
4. **Prerequisite precedence and short-circuit.** Argument > config > default; the
   first `FAILED` is the last attempted.
5. **Prerequisite dry-run.** Every result is `DRY_RUN`; no `brew install`.
6. **Plain config.** Accepted; macOS-only fields default.
7. **Deploy argv.** With `base_interpreter=["/opt/homebrew/bin/python3"]` a fresh
   `deploy` issues that interpreter's `-m venv`, pip upgrade, and pip install in
   order, elevated per config.

---

## Module Layout

```
source/packages/ccai/sysman/macos/
└── pythonenvironmentmanagement.py

source/testroots/testbase/ccai/tests/sysman/macos/
└── test_pythonenvironmentmanagement.py
```

---

## Testing Strategy

The shared base suite covers the verbs.  The macOS suite asserts layout selection,
the Homebrew delegation (argv, never-elevate, `brew_user` wrap, precedence,
short-circuit, dry-run), plain config acceptance, and one end-to-end deploy with a
Homebrew interpreter, all through a `_MockCommandRunner`.
