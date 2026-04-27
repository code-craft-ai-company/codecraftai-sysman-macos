# Requirements

## Introduction

The environment configuration feature provides the `automojo-sysman-macos`
package with the ability to set, unset, and query system-wide and per-user
environment variables on macOS.  This spec implements the
environment-configuration contract defined at
`automojo-sysman-common/specs/contracts/environment-configuration/contract.md`
for macOS using `launchctl setenv` / `launchctl unsetenv` for runtime values,
`/etc/launchd.conf` (legacy) and `/etc/paths.d/*` for persistent system-scope
hints, and per-user `~/.zshenv` for user-scope persistence.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *what*
variables to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All environment-file I/O is delegated
to the `IFileOperator` abstraction and all `launchctl` commands to the
`ICommandRunner` abstraction; `pwd.getpwnam` is the single direct call made by
the module and is isolated in a private helper.

The abstract `EnvScope` on macOS maps to:

- **SYSTEM**: `launchctl setenv NAME VALUE` (runtime, system domain) PLUS
  `/etc/launchd.conf` (persistence, deprecated but still honoured on
  Sequoia/Sonoma bootstraps) PLUS `/etc/paths.d/automojo` when the variable is
  `PATH` specifically.
- **USER**: `launchctl asuser <uid> launchctl setenv NAME VALUE` (runtime,
  user domain) PLUS `~<user>/.zshenv` (persistence, zsh is the default shell).

---

## Requirements

### Requirement 1: Set a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a system-scope
environment variable, so that the value is visible to every launchd-managed
service and every login.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_system_env(name, value)` THE SYSTEM SHALL
   invoke `launchctl setenv <name> <value>` via the command runner AND write
   the variable as a `setenv <name> "<value>"` line in `/etc/launchd.conf`.
2. WHEN `name == "PATH"` THE SYSTEM SHALL additionally write the value (one
   entry per line) to `/etc/paths.d/automojo` via `IFileOperator.write_text`.
3. WHEN both the runtime value (queried via `launchctl getenv <name>`) AND
   the persisted value already match THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `/etc/launchd.conf` does not exist THE SYSTEM SHALL create it with the
   single `setenv` line.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner or the file
   operator's mutating methods and SHALL return `status=DRY_RUN`.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner
   and to every `write_text` call.
7. WHEN any underlying call fails THE SYSTEM SHALL return `status=FAILED` with
   `message` populated.

---

### Requirement 2: Unset a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a system-scope
environment variable, so that future services and logins no longer see it.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_system_env(name)` and the variable is
   absent from both runtime (`launchctl getenv` exits non-zero) and
   persistent storage THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the variable is present in the runtime domain THE SYSTEM SHALL invoke
   `launchctl unsetenv <name>` via the command runner.
3. WHEN the variable is present in `/etc/launchd.conf` THE SYSTEM SHALL
   rewrite the file with the matching `setenv` line removed.
4. WHEN `name == "PATH"` and `/etc/paths.d/automojo` exists THE SYSTEM SHALL
   delete it via `IFileOperator.delete`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner or the file
   operator's mutating methods.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True`.

---

### Requirement 3: Get a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
system-scope variable, so that I can check its current value.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_env(name)` THE SYSTEM SHALL invoke
   `launchctl getenv <name>` via the command runner; when exit code 0, parse
   stdout as the value and return `MacosEnvValue(scope=SYSTEM,
   source="launchctl")`.
2. WHEN `launchctl getenv` exits non-zero THE SYSTEM SHALL read
   `/etc/launchd.conf` via `IFileOperator.read_text` and return a
   `MacosEnvValue` when the variable is present.
3. WHEN neither source yields the variable THE SYSTEM SHALL return `None`.
4. WHEN `get_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 4: List System-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every system-scope
variable, so that I can enumerate the full environment.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_system_env()` THE SYSTEM SHALL invoke
   `launchctl export` via the command runner and parse the shell-compatible
   output, then merge with parsed `/etc/launchd.conf` contents.
2. WHEN a variable appears in both sources with identical values THE SYSTEM
   SHALL emit one entry.
3. WHEN a variable appears in both sources with different values THE SYSTEM
   SHALL use `launchctl export` as authoritative.
4. WHEN both sources are empty or missing THE SYSTEM SHALL return an empty
   dict.
5. WHEN `list_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 5: Set a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a user-scope
environment variable, so that a specific user's future sessions see the value.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_user_env(user, name, value)` and the user
   does not exist THE SYSTEM SHALL return `status=FAILED` with `message="user
   not found: <user>"` and SHALL NOT invoke the runner or file operator.
2. WHEN the user exists THE SYSTEM SHALL resolve the uid via
   `pwd.getpwnam(user).pw_uid` AND the home via `pwd.getpwnam(user).pw_dir`.
3. WHEN the user exists THE SYSTEM SHALL invoke
   `launchctl asuser <uid> launchctl setenv <name> <value>` via the command
   runner AND write the variable as an `export <name>="<value>"` line in
   `<home>/.zshenv`.
4. WHEN the target `.zshenv` does not exist THE SYSTEM SHALL create it with
   the single export line.
5. WHEN both the runtime value (via `launchctl asuser ... getenv`) AND the
   persisted value already match THE SYSTEM SHALL return `status=SKIPPED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner or the file
   operator's mutating methods.
7. WHEN `elevated=True` AND the target user differs from the current user THE
   SYSTEM SHALL pass `elevated=True`.  When they match THE SYSTEM SHALL pass
   `elevated=False`.

---

### Requirement 6: Unset a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a user-scope
environment variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_user_env(user, name)` and the user does
   not exist THE SYSTEM SHALL return `status=FAILED` with `message="user not
   found: <user>"`.
2. WHEN neither runtime nor persistent sources have the variable THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN the runtime has the variable THE SYSTEM SHALL invoke
   `launchctl asuser <uid> launchctl unsetenv <name>`.
4. WHEN the `.zshenv` has the variable THE SYSTEM SHALL rewrite the file with
   the matching line removed.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT invoke the runner or file
   operator's mutating methods.
6. WHEN `elevated=True` THE SYSTEM SHALL follow the same elevation policy as
   `set_user_env`.

---

### Requirement 7: Get a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
user-scope variable, so that I can check its current value.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user_env(user, name)` and the user does
   not exist THE SYSTEM SHALL raise `MacosEnvConfigError`.
2. WHEN the user exists THE SYSTEM SHALL first invoke
   `launchctl asuser <uid> launchctl getenv <name>`; on exit 0 parse stdout
   and return `MacosEnvValue(scope=USER, source="launchctl-user")`.
3. WHEN the launchctl query exits non-zero THE SYSTEM SHALL parse
   `<home>/.zshenv` via `IFileOperator.read_text`.
4. WHEN neither source yields the variable THE SYSTEM SHALL return `None`.
5. WHEN `get_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 8: List User-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every user-scope
variable set for a user.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_user_env(user)` and the user does not
   exist THE SYSTEM SHALL raise `MacosEnvConfigError`.
2. WHEN the `.zshenv` does not exist AND the runtime export is empty THE
   SYSTEM SHALL return an empty dict.
3. WHEN either source has content THE SYSTEM SHALL merge them (runtime wins
   on conflict) into a `dict[str, str]`.
4. WHEN `list_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want system-scope writes to
run as root, so that writes under `/etc/` succeed and `launchctl` in the
system domain is accepted.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` on every runner
   and file-operator call for system-scope writes.
2. WHEN `elevated=True` AND the user-scope target differs from the current
   user THE SYSTEM SHALL pass `elevated=True`.
3. WHEN `elevated=True` AND the user-scope target matches the current user
   THE SYSTEM SHALL pass `elevated=False`.
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` everywhere.
5. WHEN performing reads THE SYSTEM SHALL NEVER pass `elevated=True`.

---

### Requirement 10: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write verb to honour
dry-run mode, so that plans can be validated without system change.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for every
   write verb without invoking any mutating runner or file-operator method.
2. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code path.
3. WHEN `dry_run=True` THE SYSTEM SHALL still perform reads so the guard
   logic can distinguish `SKIPPED` from `DRY_RUN`.
