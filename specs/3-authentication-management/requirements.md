# Requirements

## Introduction

The authentication management feature provides the
`automojo-sysman-macos` package with the ability to manage the macOS
authentication stack: per-service Pluggable Authentication Modules
(PAM) files under `/etc/pam.d/` and a best-effort surface over the
authorization database at `/etc/authorization` (an XML property list
controlling rights and rules consumed by `Authorization Services`).
It implements the cross-OS authentication-management contract defined
at
`automojo-sysman-common/specs/contracts/authentication-management/contract.md`
for macOS.

macOS authentication is a hybrid: its PAM stack lives in the same
`/etc/pam.d/` location as Linux but the OS itself routes most
GUI-driven privilege requests through `Authorization Services` against
the authorization database. The Open Directory daemon
(`opendirectoryd`) handles directory binding and credential lookup.
This pilot focuses on PAM-stack management, which is the surface that
overlaps cleanly with the cross-OS contract; the authorization
database is exposed only as an out-of-scope marker.

A maintenance coordinator running in an external package drives all
operations by calling this feature's public verbs.  All commands are
executed through the `automojo-interfaces` command-runner abstraction.

This feature complements — and does not duplicate — the SSH-daemon
hardening surface owned by `3-ssh-server-configuration`, the
local-account surface owned by `3-user-group-management`, and the
directory-services surface owned by `3-identity-management`.

---

## Requirements

### Requirement 1: Read a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to read the
current PAM stack for a named subsystem (`login`, `sudo`, `su`, `sshd`,
`cron`), so that I can compare it against a declared baseline.

#### Acceptance Criteria

1. WHEN `get_auth_config(subsystem)` is called with a recognised
   subsystem THE SYSTEM SHALL invoke `cat /etc/pam.d/<service>` via
   the command runner with `elevated=False`.
2. WHEN the file exists THE SYSTEM SHALL parse the PAM stack into a
   list of `MacosAuthEntry` records preserving `module_type`,
   `control`, `module_path`, and `args` in declaration order.
3. WHEN the subsystem maps to `MacosAuthSubsystem.OTHER` THE SYSTEM
   SHALL accept a free-form service name and resolve the path in
   the same way.
4. WHEN the file is missing THE SYSTEM SHALL raise
   `MacosAuthenticationSubsystemNotFoundError`.
5. WHEN the runner reports a non-zero exit code for any other reason
   THE SYSTEM SHALL raise `MacosAuthenticationQueryError`.

---

### Requirement 2: Write a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to write a
declared PAM stack to `/etc/pam.d/<service>`, so I can drive the
service's authentication policy from a versioned source-of-truth.

#### Acceptance Criteria

1. WHEN `set_auth_config(subsystem, config)` is called THE SYSTEM SHALL
   read the current file via `cat`, render the supplied
   `MacosAuthConfig` into PAM stack syntax, and compare both
   verbatim.
2. WHEN the rendered content equals the current content THE SYSTEM
   SHALL return `MacosAuthActionResult(status=SKIPPED)` without
   invoking `tee`.
3. WHEN the rendered content differs and `config.dry_run=True` THE
   SYSTEM SHALL return `status=DRY_RUN` with the would-have-been
   `tee` argv populated.
4. WHEN the rendered content differs and dry-run is off THE SYSTEM
   SHALL invoke `tee /etc/pam.d/<service>` with `stdin=<rendered>`
   and `elevated=True`.
5. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.

---

### Requirement 3: List Registered Authentication Methods

**User Story:** As a maintenance coordinator, I want to enumerate the
PAM modules referenced anywhere under `/etc/pam.d/`, so I can decide
which to enable or disable per subsystem.

#### Acceptance Criteria

1. WHEN `list_auth_methods()` is called THE SYSTEM SHALL invoke
   `ls -1 /etc/pam.d` to obtain the list of service files.
2. THE SYSTEM SHALL `cat` each service file and aggregate every
   referenced `module_path` into a single `MacosAuthMethodInfo`
   record with `name=<module_path>`.
3. THE SYSTEM SHALL set `enabled=True` when the module appears in
   ANY non-comment-out line and `enabled=False` only when every
   reference is comment-prefixed (`# module ...`).
4. THE SYSTEM SHALL set `description` to a comma-joined summary of
   the service files that reference the module.
5. THE list operation SHALL execute with `elevated=False`.

---

### Requirement 4: Enable an Authentication Method

**User Story:** As a maintenance coordinator, I want to enable a
named PAM module across the canonical subsystems so that the
modules's directives are applied on the next authentication.

#### Acceptance Criteria

1. WHEN `enable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the module is already enabled.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the module is found exclusively as a comment-prefixed line
   THE SYSTEM SHALL strip the leading `#` from each occurrence and
   re-write the affected file via `tee` with `elevated=True`.
4. WHEN the module is not present anywhere in the configured
   subsystems THE SYSTEM SHALL raise
   `MacosAuthenticationSubsystemNotFoundError`.
5. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 5: Disable an Authentication Method

**User Story:** As a maintenance coordinator, I want to disable a
named PAM module across the canonical subsystems so that the
module's directives are skipped on the next authentication.

#### Acceptance Criteria

1. WHEN `disable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the module is already disabled or absent.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the module is enabled THE SYSTEM SHALL prepend a single `#`
   to each occurrence and re-write the affected file via `tee` with
   `elevated=True`.
4. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 6: Validate a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want a syntactic
validation pass over a PAM file so I can detect malformed lines
before deploying.

#### Acceptance Criteria

1. WHEN `validate_auth_config(subsystem)` is called THE SYSTEM SHALL
   read the live file via `cat /etc/pam.d/<service>` and re-parse it
   using the internal stack parser.
2. THE SYSTEM SHALL return a `MacosAuthValidationResult` with
   `valid: bool`, `errors: list[str]`, and `warnings: list[str]`.
3. THE pilot SHALL document that PAM has no canonical validator and
   that the parser-driven check covers structural issues only.
4. UNRECOGNISED `module_type` tokens, missing `control` tokens, and
   missing `module_path` tokens SHALL be reported as `errors`.
5. THE validate operation SHALL execute with `elevated=False`.

---

### Requirement 7: Authorization Database Out-of-Scope Marker

**User Story:** As a maintenance coordinator, I want a clear statement
of which macOS authentication surfaces this feature does NOT manage,
so I do not assume coverage.

#### Acceptance Criteria

1. THE SYSTEM SHALL document that the macOS authorization database at
   `/etc/authorization` (a binary plist consumed by Authorization
   Services) is NOT managed by this feature in the pilot.
2. THE SYSTEM SHALL expose `authz_path` on
   `MacosAuthenticationManagerConfig` for forward compatibility but
   SHALL NOT read from or write to it in any verb.
3. THE design document SHALL explain that authz-database management
   requires `security authorizationdb read/write` invocations and
   pure-XML round-trip tooling that is out of scope for the pilot.

---

### Requirement 8: Subsystem Mapping

**User Story:** As a maintenance coordinator, I want a typed enum of
known PAM subsystems so I cannot pass a typo for a service name.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `MacosAuthSubsystem` with members
   `LOGIN`, `SUDO`, `SU`, `SSH`, `CRON`, `OTHER`.
2. EACH enum member except `OTHER` SHALL map to the canonical PAM
   service file name (`login`, `sudo`, `su`, `sshd`, `cron`).
3. WHEN `OTHER` is used the caller MUST supply a `name` field used
   as the literal service-file basename.
4. THE manager's verbs SHALL accept either an `MacosAuthSubsystem`
   or a raw string; raw strings are routed through the OTHER
   pathway internally.

---

### Requirement 9: Idempotency

**User Story:** As a maintenance coordinator, I want every write verb
to be idempotent so I can re-run the manager without side effects.

#### Acceptance Criteria

1. THE SYSTEM SHALL return `SKIPPED` from `set_auth_config` when the
   rendered desired content equals the current file content
   verbatim.
2. THE SYSTEM SHALL return `SKIPPED` from `enable_method` when the
   module is already enabled (un-commented) on every observed
   subsystem.
3. THE SYSTEM SHALL return `SKIPPED` from `disable_method` when the
   module is already disabled or absent.
4. Reads (`get_auth_config`, `list_auth_methods`,
   `validate_auth_config`) SHALL never short-circuit.

---

### Requirement 10: Elevation, Dry-Run, and Error Reporting

**User Story:** As a maintenance coordinator, I want every PAM-stack
write to elevate, every read to stay unprivileged, and every failure
to surface with the underlying exit code and stderr.

#### Acceptance Criteria

1. THE SYSTEM SHALL pass `elevated=True` for every write verb when
   `config.elevated=True`.
2. THE SYSTEM SHALL pass `elevated=False` for every read verb.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke the runner
   for any write verb.
4. WHEN the runner returns a non-zero exit code THE SYSTEM SHALL
   return `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
5. THE SYSTEM SHALL NOT catch `SemanticError` and SHALL NOT silently
   swallow exceptions.
6. THE returned `MacosAuthActionResult` SHALL carry `subsystem` and
   `method` attributes when meaningful so downstream reports can
   route results without re-parsing the `command` argv.
