# Requirements

## Introduction

The service-management feature provides the `codecraftai-sysman-macos` package with the
ability to manage launchd system and user services on macOS.  This spec implements the
service-management contract defined at
`codecraftai-sysman-common/specs/contracts/service-management/contract.md` for macOS
using `launchctl`.  It covers start/stop/restart/reload, enable/disable, a fallback
for mask/unmask (launchd has no direct equivalent), status queries, service listing,
and a no-op `reload_daemon`, with full idempotency and dry-run support.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interface.  The feature does not decide *what* to
run — it provides the capability to do so reliably, idempotently, and auditably.  All
system interaction happens through `ICommandRunner`; no direct `subprocess` call
lives in the manager.  The abstract `ServiceScope.SYSTEM` maps to the `system`
launchd domain; `ServiceScope.USER` maps to `gui/<uid>` using the
`MacosServiceManagerConfig.target_uid` field.  Writes in system scope pass
`elevated=True` when the config allows; reads never elevate.

Because launchctl does not expose a dedicated "mask" concept, macOS emulates masking
by disabling the service AND installing an override plist that refuses to load the
service.  Callers that do not want the override-plist fallback can disable it via
`MacosServiceManagerConfig.mask_fallback_enabled=False`; in that mode, `mask_service`
becomes an alias for `disable_service`.

---

## Requirements

### Requirement 1: Start a Service

**User Story:** As a maintenance coordinator, I want to start a launchd service,
idempotently and with dry-run support, so that maintenance workflows are safe to
re-run.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_service(name, scope)` with `scope=SYSTEM` THE
   SYSTEM SHALL invoke `launchctl kickstart -k system/<name>`; with `scope=USER` THE
   SYSTEM SHALL invoke `launchctl kickstart -k gui/<uid>/<name>`.
2. WHEN the current `active_state` is already `"active"` THE SYSTEM SHALL return a
   `MacosServiceActionResult` with `status=SKIPPED` without invoking the start
   command.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosServiceActionResult`
   with `status=SUCCESS` and the service/scope populated.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosServiceActionResult` with
   `status=FAILED`, `exit_code`, `stdout`, `stderr`, and `command` populated.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the runner and SHALL
   return `status=DRY_RUN`.

---

### Requirement 2: Stop a Service

**User Story:** As a maintenance coordinator, I want to stop a launchd service,
idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_service(name, scope)` THE SYSTEM SHALL invoke
   `launchctl bootout <domain>/<name>` (graceful) per scope.
2. WHEN the current `active_state` is `"inactive"` THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the stop command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with the exit
   code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `stop_service` encounters a service whose plist is ephemeral and `bootout`
   returns non-zero with an `Operation not permitted` stderr (TCC denial) THE
   SYSTEM SHALL return `status=FAILED` with the stderr preserved verbatim.

---

### Requirement 3: Restart a Service

**User Story:** As a maintenance coordinator, I want to restart a launchd service
unconditionally.

#### Acceptance Criteria

1. WHEN the coordinator calls `restart_service(name, scope)` THE SYSTEM SHALL invoke
   `launchctl kickstart -k <domain>/<name>` (the `-k` flag kills the existing
   instance before restarting).
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `restart_service` is called the idempotency guard SHALL NOT skip the
   command regardless of current state.

---

### Requirement 4: Reload a Service

**User Story:** As a maintenance coordinator, I want to send SIGHUP (or equivalent)
to a running launchd service, so that the process re-reads its configuration without
restarting.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_service(name, scope)` AND the current
   `active_state` is `"active"` THE SYSTEM SHALL invoke
   `launchctl kill SIGHUP <domain>/<name>`.
2. WHEN the current `active_state` is not `"active"` THE SYSTEM SHALL return
   `status=FAILED` with `message="service is not active; cannot reload"` without
   invoking the kill command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 5: Enable a Service

**User Story:** As a maintenance coordinator, I want to enable a launchd service for
automatic start at boot or login, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_service(name, scope)` THE SYSTEM SHALL invoke
   `launchctl enable <domain>/<name>`.
2. WHEN the current `enabled_state` is already `"enabled"` THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 6: Disable a Service

**User Story:** As a maintenance coordinator, I want to disable a launchd service,
idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_service(name, scope)` THE SYSTEM SHALL invoke
   `launchctl disable <domain>/<name>`.
2. WHEN the current `enabled_state` is `"disabled"` or `"masked"` THE SYSTEM SHALL
   return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 7: Mask a Service (emulated)

**User Story:** As a maintenance coordinator, I want a uniform `mask_service` verb
that works on macOS even though launchd lacks a direct mask concept.

#### Acceptance Criteria

1. WHEN the coordinator calls `mask_service(name, scope)` AND
   `config.mask_fallback_enabled=True` THE SYSTEM SHALL dispatch a two-step
   sequence: (a) `launchctl disable <domain>/<name>`, (b) write an override plist at
   `/Library/LaunchDaemons/<name>.override.plist` (or
   `~/Library/LaunchAgents/<name>.override.plist` for user scope) containing
   `<key>Disabled</key><true/>` via `IFileOperator.write_text`.
2. WHEN `config.mask_fallback_enabled=False` THE SYSTEM SHALL treat `mask_service`
   as an alias for `disable_service` and SHALL NOT write any override plist.
3. WHEN the current `enabled_state` is `"masked"` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN either step of the two-step sequence fails THE SYSTEM SHALL return
   `status=FAILED` with the first step's failure populated.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` without
   invoking the runner or the file operator.

---

### Requirement 8: Unmask a Service (emulated)

**User Story:** As a maintenance coordinator, I want `unmask_service` to reverse
whichever mask strategy the manager was configured with.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmask_service(name, scope)` AND
   `config.mask_fallback_enabled=True` THE SYSTEM SHALL dispatch a two-step
   sequence: (a) remove the override plist via `IFileOperator.remove(path)`, (b)
   `launchctl enable <domain>/<name>`.
2. WHEN `config.mask_fallback_enabled=False` THE SYSTEM SHALL treat
   `unmask_service` as an alias for `enable_service`.
3. WHEN the current `enabled_state` is not `"masked"` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN either step fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 9: Get Service Status

**User Story:** As a maintenance coordinator, I want to query a launchd service's
current state without elevation.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_service_status(name, scope)` THE SYSTEM SHALL
   invoke `launchctl print <domain>/<name>` via the runner without elevation.
2. WHEN `print` exits non-zero with stdout/stderr matching the "service not found"
   pattern THE SYSTEM SHALL return
   `MacosServiceStatus(active_state="not-found", enabled_state="not-found",
   main_pid=None, description=None)` WITHOUT raising.
3. WHEN `print` fails with an unexpected error THE SYSTEM SHALL raise
   `MacosServiceQueryError` with the exit code and stderr.
4. WHEN `print` succeeds THE SYSTEM SHALL parse the `state = running|not running`,
   `pid = <int>`, `program = <path>`, and any inline `disabled` flag to populate
   `active_state`, `main_pid`, `description`, and `enabled_state`.

---

### Requirement 10: List Services

**User Story:** As a maintenance coordinator, I want to list all launchd services
for a scope.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_services(scope)` THE SYSTEM SHALL invoke
   `launchctl print-disabled <domain>` AND `launchctl list` and merge the two
   result sets by service name.
2. WHEN the commands succeed THE SYSTEM SHALL return one `MacosServiceInfo` per
   unique label with `active_state` from `list`, `enabled_state` from
   `print-disabled`, and `description=None`.
3. WHEN either command fails THE SYSTEM SHALL raise `MacosServiceQueryError`.
4. WHEN `scope=USER` and the target uid has no `gui/<uid>` domain THE SYSTEM SHALL
   raise `MacosServiceQueryError` with a descriptive message.

---

### Requirement 11: Reload the Daemon (no-op)

**User Story:** As a maintenance coordinator, I want a uniform `reload_daemon`
verb, so that cross-OS orchestration does not have to special-case macOS.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_daemon(scope)` THE SYSTEM SHALL NOT invoke
   the runner and SHALL return
   `MacosServiceActionResult(status=SUCCESS, service_name="",
   message="launchd re-reads plists on bootstrap; no daemon-reload equivalent",
   scope=scope)`.
2. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
3. WHEN the idempotency guard is evaluated for `reload_daemon` THE SYSTEM SHALL
   NEVER skip (but the effective operation is still a no-op).

---

### Requirement 12: Scope Mapping

**User Story:** As a maintenance coordinator, I want `ServiceScope` to map
deterministically to launchd domain strings.

#### Acceptance Criteria

1. WHEN `scope=MacosServiceScope.SYSTEM` THE SYSTEM SHALL use the domain `system`.
2. WHEN `scope=MacosServiceScope.USER` AND
   `MacosServiceManagerConfig.target_uid` is set THE SYSTEM SHALL use the domain
   `gui/<target_uid>`.
3. WHEN `scope=MacosServiceScope.USER` AND `target_uid` is not set THE SYSTEM SHALL
   raise `ValueError` before invoking the runner.
4. WHEN an unrecognised scope is supplied THE SYSTEM SHALL raise `ValueError`.
5. WHEN the returned `MacosServiceActionResult` has a `scope` field the value SHALL
   equal the scope that was used to build the command.

---

### Requirement 13: Elevation

**User Story:** As a maintenance coordinator, I want system-scope writes to pass
`elevated=True` when the config enables elevation, and reads to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write AND the scope is `SYSTEM` THE
   SYSTEM SHALL pass `elevated=True` to the runner.
2. WHEN `config.elevated=True` AND the verb is a write AND the scope is `USER` AND
   `target_uid != os.getuid()` THE SYSTEM SHALL pass `elevated=True` (managing
   another user's `gui/<uid>` requires root on macOS).
3. WHEN `config.elevated=True` AND the verb is a write AND the scope is `USER` AND
   `target_uid == os.getuid()` THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the verb is a read or idempotency probe THE SYSTEM SHALL NEVER pass
   `elevated=True`.
