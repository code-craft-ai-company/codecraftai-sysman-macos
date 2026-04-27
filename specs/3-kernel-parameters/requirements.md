# Requirements

## Introduction

The kernel parameters feature provides the `automojo-sysman-macos` package with
the ability to read, write, and persist `sysctl` kernel tunables on macOS
systems.  This spec implements the kernel-parameters contract defined at
`automojo-sysman-common/specs/contracts/kernel-parameters/contract.md` for
macOS using `sysctl` at runtime and the single flat `/etc/sysctl.conf` file for
persistence.  It includes querying a single parameter, listing all parameters,
writing parameters either at runtime, persistently, or both, reloading
persisted parameters into the running kernel, and removing a previously
persisted parameter.

A maintenance coordinator running in an external package drives all operations
by calling into this feature's public interfaces.  The feature does not decide
*which* parameters to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed
exclusively through the `automojo-interfaces` command-runner abstraction and
all persistent-file mutations are executed through the `automojo-interfaces`
file-operator abstraction, enabling dry-run simulation, auditing, and test
mocking without requiring a live system.

macOS's kernel-parameter surface differs from Linux in three important ways:

1. macOS uses BSD-style sysctl names (e.g., `net.inet.ip.forwarding`,
   `kern.maxfiles`) rather than Linux dotted-family names.
2. macOS does not support drop-in directories — persistent sysctl settings
   live in a single flat `/etc/sysctl.conf` file.
3. Some macOS tunables are read-only or require SIP to be disabled; these
   are documented as unsupported in the allowlist.

---

## Requirements

### Requirement 1: Get a Single Parameter

**User Story:** As a maintenance coordinator, I want to read the current runtime
value of a single sysctl parameter on macOS, so that I can make conditional
decisions based on its value.

#### Acceptance Criteria

1. WHEN the coordinator provides a parameter name THE SYSTEM SHALL invoke
   `sysctl -n <name>` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return a `MacosParameterValue`
   with `name=<name>`, `value=<stripped stdout>`, and `source="runtime"`.
3. WHEN the command fails with an "unknown oid" error THE SYSTEM SHALL
   return `None`.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise a
   `MacosKernelParameterQueryError` carrying the exit code and captured
   stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL still execute the read —
   reads do not mutate and are not short-circuited by dry-run.

---

### Requirement 2: Set a Parameter

**User Story:** As a maintenance coordinator, I want to set a sysctl parameter
value at runtime, persistently, or both on macOS, so that I can enforce a
desired kernel tuning state.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_parameter(name, value, persistent=False)`
   AND the current runtime value (read via `sysctl -n <name>`) already equals
   the requested value THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SKIPPED` and populate
   `current_value` / `applied_value` with the matching value.
2. WHEN the coordinator calls `set_parameter(name, value, persistent=False)`
   AND the runtime value differs THE SYSTEM SHALL invoke `sysctl -w
   <name>=<value>` via the command runner.
3. WHEN the coordinator calls `set_parameter(name, value, persistent=True)`
   AND the persistent file `/etc/sysctl.conf` already contains a matching
   `name=value` line THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SKIPPED`.
4. WHEN the coordinator calls `set_parameter(name, value, persistent=True)`
   AND the persisted value differs or is absent THE SYSTEM SHALL update
   `/etc/sysctl.conf` via the `IFileOperator`, AND THEN SHALL also apply the
   runtime change via `sysctl -w <name>=<value>`.
5. WHEN the parameter name is not in the macOS-supported prefix allowlist
   THE SYSTEM SHALL return a `MacosKernelParameterActionResult` with status
   `FAILED`, `message="parameter_not_supported"`, and SHALL NOT invoke the
   runner or file operator.
6. WHEN either the persistent-write step or the runtime-apply step fails THE
   SYSTEM SHALL return a `MacosKernelParameterActionResult` with status
   `FAILED`, a `message` identifying which step failed, and the captured
   exit code / stdout / stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command or
   file mutation and SHALL return a `MacosKernelParameterActionResult` with
   status `DRY_RUN`; for persistent writes the `diff` field SHALL carry the
   unified diff that would have been applied to `/etc/sysctl.conf`.
8. WHEN `requires_reboot` applies THE SYSTEM SHALL return
   `requires_reboot=False` for parameters writable through `sysctl -w`;
   macOS parameters that exist only in `/etc/sysctl.conf` and require reboot
   (e.g., some `kern.*` settings) SHALL be documented in the allowlist
   metadata and SHALL set `requires_reboot=True`.

---

### Requirement 3: List All Parameters

**User Story:** As a maintenance coordinator, I want to enumerate every sysctl
parameter and its current value on macOS, so that I can audit system
configuration.

#### Acceptance Criteria

1. WHEN the coordinator requests all parameters THE SYSTEM SHALL invoke
   `sysctl -a` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse every `name: value` AND
   every `name = value` line in stdout and SHALL return a `dict[str, str]`
   mapping name to value.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise a
   `MacosKernelParameterQueryError` carrying the exit code and captured
   stderr.
4. WHEN stdout contains lines that do not match either separator pattern THE
   SYSTEM SHALL silently skip those lines — macOS `sysctl -a` emits
   multi-line opaque blobs for certain keys (e.g., `kern.version`).

---

### Requirement 4: Reload Persisted Parameters

**User Story:** As a maintenance coordinator, I want to re-apply every
persisted sysctl setting into the running kernel on macOS, so that a batch of
`/etc/sysctl.conf` edits takes effect without rebooting.

#### Acceptance Criteria

1. WHEN the coordinator requests a reload THE SYSTEM SHALL invoke
   `sysctl -p /etc/sysctl.conf` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SUCCESS` and `name="*"`.
3. WHEN the command fails THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `FAILED`, the exit code,
   and captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return a `MacosKernelParameterActionResult` with status `DRY_RUN`.
5. WHEN `/etc/sysctl.conf` does not exist THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SKIPPED` and
   `message="no_persistent_file"`.

---

### Requirement 5: Remove a Persisted Parameter

**User Story:** As a maintenance coordinator, I want to remove a previously
persisted sysctl parameter from `/etc/sysctl.conf`, so that future reboots
revert to the kernel default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_persisted_parameter(name)` AND
   `/etc/sysctl.conf` does not exist THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SKIPPED`.
2. WHEN the coordinator calls `remove_persisted_parameter(name)` AND the
   file exists but does not contain the parameter THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SKIPPED`.
3. WHEN the parameter is present THE SYSTEM SHALL rewrite `/etc/sysctl.conf`
   via the `IFileOperator` with the matching line removed while preserving
   all other lines and comments.
4. WHEN the file write succeeds THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `SUCCESS`.
5. WHEN the file write fails THE SYSTEM SHALL return a
   `MacosKernelParameterActionResult` with status `FAILED`, the exit code,
   and captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any mutation
   and SHALL return a `MacosKernelParameterActionResult` with status
   `DRY_RUN` and a `diff` field showing the would-be file mutation.

---

### Requirement 6: Parameter-Prefix Allowlist

**User Story:** As a maintenance coordinator, I want the manager to reject
parameters outside macOS's writable sysctl namespace, so that typos or
unsupported tunables fail fast with a clear error.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept parameter names whose dotted prefix begins with
   any of: `kern.`, `net.`, `hw.`, `vm.`, `debug.`, `machdep.`, `security.`.
2. WHEN a parameter name does not match the allowlist THE SYSTEM SHALL
   return a `MacosKernelParameterActionResult` with status `FAILED` and a
   `message` equal to `"parameter_not_supported: <name>"`.
3. THE SYSTEM SHALL NOT treat allowlist rejection as a runner failure — no
   command or file mutation is invoked.
4. THE SYSTEM SHALL document in its allowlist metadata which accepted
   parameters are read-only under SIP (e.g., `kern.securelevel` on some
   macOS versions) and return `FAILED` with
   `message="sip_read_only: <name>"` for runtime writes against them.

---

### Requirement 7: Persistent File Format

**User Story:** As a maintenance coordinator, I want persisted parameters
written in macOS's `/etc/sysctl.conf` format, so that system initialization
consumes them correctly on reboot.

#### Acceptance Criteria

1. THE SYSTEM SHALL write entries as `<name>=<value>\n` (no spaces around
   the `=`) because macOS's `sysctl -p` parser is strict.
2. WHEN `/etc/sysctl.conf` already exists the system SHALL read it, modify
   only the target line (or append if absent), and write the merged content
   back — existing unrelated lines and comments MUST be preserved verbatim.
3. WHEN the file does not exist the system SHALL create it with a header
   `# managed by automojo-sysman-macos` on line 1 and the target line on
   line 2.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all kernel-parameter
write commands to run with the privileges required by `sysctl` and
persistent-file writes, so that mutations succeed on a standard system where
the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for `sysctl -w`, `sysctl -p`, and
   to the file operator for every `/etc/sysctl.conf` write.
2. WHEN performing read-only operations (`get_parameter`, `list_parameters`)
   THE SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to
   no elevation and allow the runner/file operator to surface the resulting
   permission error as a `MacosKernelParameterActionResult` with
   `status=FAILED` for writes.

---

### Requirement 9: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode that
produces the full would-be command sequence without mutating the system, so
that change reviews are possible before roll-out.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL NOT
   invoke `sysctl -w`, `sysctl -p`, or any file-operator write.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command` field of
   every returned `MacosKernelParameterActionResult` with the argv that
   would have been executed.
3. WHEN dry-run is active AND the operation is a persistent write or
   remove THE SYSTEM SHALL populate the `diff` field with the unified
   diff that would have been applied.
4. WHEN dry-run is active THE SYSTEM SHALL still perform read operations
   to evaluate idempotency guards — dry-run is a write-only short-circuit.
