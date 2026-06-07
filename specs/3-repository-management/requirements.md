# Requirements

## Introduction

The repository management feature provides the `codecraftai-sysman-macos`
package with the ability to manage Homebrew taps on macOS systems.
This spec implements the repository-management contract defined at
`codecraftai-sysman-common/specs/contracts/repository-management/contract.md`
for macOS using `brew tap`, `brew untap`, and `brew update`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* taps to configure — it provides the
capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through
the `codecraftai-interfaces` command-runner abstraction.

macOS's Homebrew tap model differs substantially from apt/dnf
repositories:

- Taps are named `owner/repo` (e.g., `homebrew/cask`,
  `homebrew/core`, `aws/tap`).
- There is no concept of enabling or disabling an individual tap
  short of untapping it.  `enable_repository` and `disable_repository`
  are therefore documented as `SKIPPED` and `FAILED` respectively for
  Homebrew.
- There is no concept of priority, mirror list, or GPG key at the tap
  level.  Those fields of `RepositorySpec` are accepted but ignored
  with a documented warning.
- Homebrew runs as the invoking user (not as root) on macOS — there
  is no elevation for write operations.

Because the contract explicitly anticipates this divergence (see the
contract's Repository-Name Mapping table and Elevation table), the
manager honors the verb set but surfaces gaps loudly.

---

## Requirements

### Requirement 1: Add a Tap

**User Story:** As a maintenance coordinator, I want to add a
Homebrew tap, so that formulae from that tap become installable.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_repository(spec)` AND
   `spec.base_url` is set THE SYSTEM SHALL invoke `brew tap
   <spec.name> <spec.base_url>` via the command runner without
   elevation.
2. WHEN `spec.base_url` is NOT set THE SYSTEM SHALL invoke `brew tap
   <spec.name>` via the command runner without elevation.
3. WHEN the tap is already present in the output of `brew tap`
   (listed by name) THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `FAILED`, the exit
   code, captured stderr, and the `command`.
6. WHEN `spec.gpg_key`, `spec.mirror_list_url`, or `spec.priority`
   is set THE SYSTEM SHALL accept the spec, log a one-time warning
   that the field is not applicable to Homebrew taps, and continue.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   command and SHALL return a `MacosRepositoryActionResult` with
   status `DRY_RUN`.

---

### Requirement 2: Remove a Tap

**User Story:** As a maintenance coordinator, I want to remove a
Homebrew tap, so that formulae from that tap are no longer
installable.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_repository(name)` THE SYSTEM
   SHALL invoke `brew untap <name>` via the command runner without
   elevation.
2. WHEN the tap is NOT present in the output of `brew tap` THE
   SYSTEM SHALL return a `MacosRepositoryActionResult` with status
   `SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `FAILED`, the exit
   code, captured stderr, and the `command`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   command and SHALL return a `MacosRepositoryActionResult` with
   status `DRY_RUN`.

---

### Requirement 3: Enable a Tap (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`enable_repository` API on macOS so that mixed-OS orchestration is
not forced to special-case Homebrew.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_repository(name)` AND the tap
   is present in the output of `brew tap` THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SKIPPED` and
   `message="homebrew_taps_always_enabled"`.
2. WHEN the tap is NOT present THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `FAILED` and
   `message="repository_not_found"`.
3. THE SYSTEM SHALL NOT execute any command for
   `enable_repository` — the method is deliberately a no-op so
   cross-OS code can call it unconditionally.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN`.

---

### Requirement 4: Disable a Tap

**User Story:** As a maintenance coordinator, I want a uniform
`disable_repository` API on macOS, so that mixed-OS orchestration is
not forced to special-case Homebrew.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_repository(name)` THE SYSTEM
   SHALL return a `MacosRepositoryActionResult` with status
   `FAILED` and `message="homebrew_no_disable:
   untap_required"`.
2. THE SYSTEM SHALL NOT execute any command for
   `disable_repository` — Homebrew has no soft-disable concept;
   the only way to stop using a tap is to fully remove it via
   `remove_repository`.
3. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN` and the same message.

---

### Requirement 5: List Taps

**User Story:** As a maintenance coordinator, I want to enumerate
every Homebrew tap, so that I can audit configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_repositories()` THE SYSTEM SHALL
   invoke `brew tap` via the command runner without elevation.
2. WHEN the command exits non-zero THE SYSTEM SHALL raise a
   `MacosRepositoryQueryError` carrying the exit code and captured
   stderr.
3. WHEN the command succeeds THE SYSTEM SHALL parse each non-empty
   line of stdout as a tap name and SHALL return a
   `list[MacosRepositoryInfo]` with one entry per line: `name`=tap,
   `display_name`=tap, `enabled=True`, `url=None` (Homebrew does
   not expose tap URLs via `brew tap` without additional calls),
   `priority=None`.
4. WHEN the coordinator requests tap URLs for diagnostic purposes
   THE SYSTEM SHALL support an optional `include_urls=True` mode
   that invokes `brew tap-info --json <tap>` for each tap and
   populates `url` from the `remote` field — this is NOT invoked
   by default because `brew tap-info` can be slow.

---

### Requirement 6: Get a Single Tap

**User Story:** As a maintenance coordinator, I want to resolve a
tap by name, so that I can inspect its state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_repository(name)` THE SYSTEM
   SHALL invoke `list_repositories()` internally and return the
   matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.
3. WHEN `list_repositories()` raises THE SYSTEM SHALL propagate
   the `MacosRepositoryQueryError`.

---

### Requirement 7: Refresh Taps

**User Story:** As a maintenance coordinator, I want to refresh
Homebrew's formulae metadata, so that newly added taps are consulted.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_repositories(name=None)` THE
   SYSTEM SHALL invoke `brew update` via the command runner without
   elevation.
2. WHEN the coordinator calls `refresh_repositories(name=<name>)`
   THE SYSTEM SHALL invoke `brew update <name>` via the command
   runner without elevation (Homebrew supports per-tap updates as
   of brew 3.x).
3. WHEN `brew update` succeeds THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SUCCESS`.
4. WHEN `brew update` fails THE SYSTEM SHALL return a result with
   status `FAILED`, the exit code, and captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.
6. THE SYSTEM SHALL NOT evaluate an idempotency guard on
   `refresh_repositories` — refresh is operational.

---

### Requirement 8: Set Priority (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`set_priority` API on macOS.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_priority(name, priority)` AND
   the tap does not exist THE SYSTEM SHALL raise a
   `MacosRepositoryNotFoundError` with the missing name.
2. WHEN the tap exists THE SYSTEM SHALL return a
   `MacosRepositoryActionResult` with status `SKIPPED` and
   `message="homebrew_has_no_priority_concept"`.
3. THE SYSTEM SHALL NOT execute any command for `set_priority` —
   Homebrew has no priority model.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN`.

---

### Requirement 9: Import a GPG Key (N/A)

**User Story:** As a maintenance coordinator, I want a uniform
`import_gpg_key` API on macOS.

#### Acceptance Criteria

1. WHEN the coordinator calls `import_gpg_key(key_source)` AND
   `key_source` is a local path AND the file does not exist THE
   SYSTEM SHALL raise `MacosRepositoryKeyFileNotFoundError` —
   precondition exceptions take precedence.
2. WHEN `key_source` exists (URL or readable local file) THE SYSTEM
   SHALL return a `MacosRepositoryActionResult` with status
   `SKIPPED` and `message="homebrew_taps_verified_via_https"`.
3. THE SYSTEM SHALL NOT execute any command for `import_gpg_key` —
   Homebrew taps rely on HTTPS + Git commit signing rather than
   explicit GPG key import.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a result
   with status `DRY_RUN` (subject to the precondition exception
   rule above).

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want the manager to
NOT elevate Homebrew commands, because Homebrew explicitly refuses
to run as root.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER pass `elevated=True` to the command
   runner for ANY method — Homebrew always runs as the invoking
   user.
2. WHEN the coordinator sets `elevated=True` in the config THE
   SYSTEM SHALL silently ignore it for command-runner invocation
   AND SHALL log a one-time warning at construction time.
3. THE SYSTEM SHALL document in its configuration docstrings that
   `elevated` is unused on macOS repository-management.

---

### Requirement 11: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that simulates tap changes without mutating the system.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM
   SHALL NOT invoke `brew tap`, `brew untap`, or `brew update` for
   any mutating method.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have
   been executed.
3. WHEN dry-run is active THE SYSTEM SHALL still perform read
   operations (`brew tap` for idempotency guards) to evaluate
   SKIPPED / FAILED status correctly.
4. WHEN dry-run is active THE SYSTEM SHALL still raise
   `MacosRepositoryNotFoundError` and
   `MacosRepositoryKeyFileNotFoundError` — precondition exceptions
   take precedence.
