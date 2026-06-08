# Requirements

## Introduction

The configuration management feature provides the `codecraftai-sysman-macos` package
with the ability to read, write, diff, validate, back up, restore, and adjust
permissions/ownership on macOS configuration artifacts.  This spec implements the
configuration-management contract defined at
`codecraftai-sysman-common/specs/contracts/configuration-management/contract.md`
for macOS.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *what*
configuration values to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner abstraction, and all
configuration file I/O is delegated to the `IFileOperator` abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are made inside this
module.

The abstract configuration-source identifier on macOS is either a plist path
(e.g., `/Library/Preferences/com.apple.softwareupdate.plist`), a plist domain name
(e.g., `defaults:com.apple.softwareupdate`), or a plain filesystem path
(e.g., `/etc/ssh/sshd_config`, `/etc/pam.d/sudo`).  Format detection is driven by
source prefix / suffix with an explicit caller override available.

---

## Requirements

### Requirement 1: Read a Setting

**User Story:** As a maintenance coordinator, I want to read a single setting from
a configuration source, so that I can inspect it without having to parse the
whole source.

#### Acceptance Criteria

1. WHEN the coordinator calls `read_setting(source, key)` and `source` resolves
   to a plist domain THE SYSTEM SHALL invoke `defaults read <domain> <key>` via
   the command runner and return a `MacosSettingValue` with `value_type="plist"`.
2. WHEN `source` resolves to a plist path THE SYSTEM SHALL invoke
   `defaults read <path-without-suffix> <key>` (or `plutil -p <path>` parsed in
   Python for keys with nested structure).
3. WHEN `source` resolves to a plain file THE SYSTEM SHALL read via
   `IFileOperator.read_text` and parse using the format-appropriate parser
   (INI, YAML, shell `KEY=VALUE`), returning `value_type` set accordingly.
4. WHEN the key is not present THE SYSTEM SHALL raise
   `MacosConfigSettingNotFoundError`.
5. WHEN the source does not exist THE SYSTEM SHALL raise
   `MacosConfigSettingNotFoundError` with `detail="source missing"`.
6. WHEN any other failure occurs THE SYSTEM SHALL raise `MacosConfigQueryError`.
7. WHEN `read_setting` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 2: Write a Setting

**User Story:** As a maintenance coordinator, I want to write a single setting in
a configuration source, so that I can change a value without rewriting the whole
source.

#### Acceptance Criteria

1. WHEN the coordinator calls `write_setting(source, key, value)` and `source`
   resolves to a plist THE SYSTEM SHALL infer the value type (`-string`,
   `-bool`, `-int`, `-float`, `-array`, `-dict`) and invoke
   `defaults write <domain> <key> -<type> <value>` via the command runner with
   `elevated=elevated`.
2. WHEN `source` resolves to a plain file THE SYSTEM SHALL read via
   `IFileOperator.read_text`, mutate the key in memory, re-serialise, and write
   via `IFileOperator.write_text(path=source, content=..., elevated=elevated)`.
3. WHEN the current value already equals the requested value THE SYSTEM SHALL
   return `status=SKIPPED`.
4. WHEN the source does not exist and writing is a plist THE SYSTEM SHALL allow
   `defaults write` to create it.  For plain files THE SYSTEM SHALL create the
   file with the rendered content.
5. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.
7. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the planned
   argv or diff in `command`/`diff` and no mutating call.

---

### Requirement 3: Remove a Setting

**User Story:** As a maintenance coordinator, I want to remove a single setting
from a configuration source, so that the system falls back to its default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_setting(source, key)` and the key is not
   present THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the key is present and `source` is a plist THE SYSTEM SHALL invoke
   `defaults delete <domain> <key>` via the command runner.
3. WHEN the key is present and `source` is a plain file THE SYSTEM SHALL read,
   mutate, re-serialise, and write via `IFileOperator.write_text`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: List Settings

**User Story:** As a maintenance coordinator, I want to list every setting in a
configuration source, so that I can enumerate options.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_settings(source)` and the source does not
   exist THE SYSTEM SHALL return an empty list.
2. WHEN `source` resolves to a plist THE SYSTEM SHALL invoke `defaults read
   <domain>` and parse the output; when nested structures are present the parser
   SHALL flatten to dotted keys (e.g., `NSGlobalDomain.AppleInterfaceStyle`).
3. WHEN `source` resolves to a plain file THE SYSTEM SHALL parse the file in
   Python and return one `MacosSettingValue` per key, preserving order.
4. WHEN parsing fails THE SYSTEM SHALL raise `MacosConfigQueryError`.
5. WHEN `list_settings` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 5: Write Whole-Source Content

**User Story:** As a maintenance coordinator, I want to overwrite an entire
configuration file with deterministic content, so that I can apply a whole-file
template.

#### Acceptance Criteria

1. WHEN the coordinator calls `write(source, content, mode, owner, group)` and
   `source` resolves to a plain file THE SYSTEM SHALL call
   `IFileOperator.write_text(path=source, content=content, elevated=elevated)`
   when content differs from current.
2. WHEN content matches current THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `mode`/`owner`/`group` are supplied and differ THE SYSTEM SHALL invoke
   `chmod`/`chown` via the runner after the write.
4. WHEN `source` resolves to a plist domain THE SYSTEM SHALL invoke
   `defaults import <domain> <path>` where `<path>` is a temp file populated with
   `content` via `write_text`, then delete the temp file via
   `IFileOperator.delete`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the full
   diff in `diff`.

---

### Requirement 6: Diff Current vs Desired

**User Story:** As a maintenance coordinator, I want to compute a diff between
current and desired content, so that I can preview changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `diff(source, desired_content)` and `source` is a
   plain file THE SYSTEM SHALL read via `IFileOperator.read_text` and compute
   the unified diff using `difflib.unified_diff`.
2. WHEN `source` is a plist THE SYSTEM SHALL invoke `defaults export <domain>
   -` via the runner to retrieve the current plist XML and diff it against
   `desired_content` using `difflib.unified_diff`.
3. WHEN the source does not exist THE SYSTEM SHALL treat current as empty.
4. WHEN current equals desired THE SYSTEM SHALL return
   `MacosConfigDiffResult(has_diff=False, diff="")`.
5. WHEN `diff` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 7: Validate a Source

**User Story:** As a maintenance coordinator, I want to validate a configuration
source using a registered validator, so that I can catch errors before a reload.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate(source, validator_id)` and
   `validator_id` is registered THE SYSTEM SHALL invoke the validator's argv
   (e.g., `plutil -lint <path>`, `sshd -t -f <path>`, `nginx -t -c <path>`,
   `apachectl -t -f <path>`) via the command runner without elevation.
2. WHEN exit code 0 THE SYSTEM SHALL return `valid=True`.
3. WHEN exit code is non-zero THE SYSTEM SHALL return `valid=False` with
   `exit_code`, `stdout`, `stderr` populated.
4. WHEN `validator_id` is not registered THE SYSTEM SHALL raise
   `MacosConfigValidatorNotFoundError`.
5. WHEN `dry_run=True` THE SYSTEM SHALL still invoke the validator since
   validation is read-only.

---

### Requirement 8: Backup a Source

**User Story:** As a maintenance coordinator, I want to take a timestamped backup
of a configuration source, so that I can recover from a bad change.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_configuration(source)` and the source does
   not exist THE SYSTEM SHALL raise `MacosConfigFileNotFoundError`.
2. WHEN `source` resolves to a plist domain THE SYSTEM SHALL invoke
   `defaults export <domain> <backup_path>` where `<backup_path>` is
   `/var/backups/ccai/<domain>.<YYYYMMDDTHHMMSS>.plist`.
3. WHEN `source` resolves to a plain file THE SYSTEM SHALL invoke
   `cp -p <source> <backup_path>` where `<backup_path>` is
   `<source>.bak.<YYYYMMDDTHHMMSS>`.
4. WHEN the command succeeds THE SYSTEM SHALL return
   `status=SUCCESS` with `backup_path`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the planned
   `backup_path`.

---

### Requirement 9: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a backup over its
original source, so that I can roll back.

#### Acceptance Criteria

1. WHEN the coordinator calls `restore_configuration(backup_path)` and the path
   ends in `.plist` under the plist backup directory THE SYSTEM SHALL derive
   the domain and invoke `defaults import <domain> <backup_path>`.
2. WHEN the backup path matches `<source>.bak.<YYYYMMDDTHHMMSS>` THE SYSTEM
   SHALL derive the original path and invoke `cp -p <backup_path> <original>`.
3. WHEN the backup does not exist THE SYSTEM SHALL raise
   `MacosConfigFileNotFoundError`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 10: Set Permissions

**User Story:** As a maintenance coordinator, I want to apply `chmod` to a
plain-file configuration source, so that permissions match policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_permissions(source, mode)` and the source
   does not exist THE SYSTEM SHALL raise `MacosConfigFileNotFoundError`.
2. WHEN `source` resolves to a plist domain THE SYSTEM SHALL return
   `status=FAILED` with `detail="plist domains have no file mode"` and SHALL NOT
   invoke the runner.
3. WHEN the current mode already matches THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN the current mode differs THE SYSTEM SHALL invoke `chmod <mode> <source>`
   via the runner.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 11: Set Ownership

**User Story:** As a maintenance coordinator, I want to apply `chown` to a
plain-file configuration source, so that ownership matches policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_ownership(source, user, group)` and the
   source does not exist THE SYSTEM SHALL raise `MacosConfigFileNotFoundError`.
2. WHEN `source` resolves to a plist domain THE SYSTEM SHALL return
   `status=FAILED` with `detail="plist domains have no owner"`.
3. WHEN the current owner and group already match THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN either differs THE SYSTEM SHALL invoke `chown <user>:<group> <source>`
   via the runner.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 12: Reload Configuration

**User Story:** As a maintenance coordinator, I want to trigger the launchd job
that owns a configuration to reload, so that changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_configuration(service)` THE SYSTEM SHALL
   invoke `launchctl kickstart -k <domain>/<service>` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 13: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all write operations to run
with root privileges, so that writes under `/Library/Preferences/` and `/etc/`
succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner and
   file operator for every write verb.
2. WHEN performing reads THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on every path.
