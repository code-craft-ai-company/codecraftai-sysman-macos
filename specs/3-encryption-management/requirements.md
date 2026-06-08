# Requirements

## Introduction

The encryption management feature provides the
`codecraftai-sysman-macos` package with the ability to enable / disable
FileVault on the boot volume and APFS encryption on data volumes,
query encryption status, list encrypted volumes, rotate the personal
recovery key, back up the recovery key to a filesystem path, and
unlock a locked APFS volume by passing a passphrase.  This spec
implements the encryption-management contract defined at
`codecraftai-sysman-common/specs/contracts/encryption-management/contract.md`
for macOS by wrapping the `fdesetup` (boot / FileVault) and
`diskutil apfs` (data / APFS encryption) toolchains.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* volumes should be encrypted, *which*
passphrases should be used, or *whether* a recovery-key backup
should be archived to off-host storage — it provides the capability
to *manipulate FileVault / APFS encryption state* reliably,
idempotently, and with full auditability.  All `fdesetup` and
`diskutil` commands are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction; passphrase bytes
flow on runner `stdin` and never appear on argv.

Disk partitioning, filesystem creation, mounting, and Spotlight
indexing are NOT in scope here.  Filesystem-level concerns live in
`filesystem-management`; partition-level concerns live in
`storage-management`.  iCloud-escrowed recovery keys, MDM-managed
FileVault enrolment, and Sealed System Volume cryptography are
intentionally deferred — they are reserved as macOS extensions.

---

## Requirements

### Requirement 1: Enable Encryption (Boot or Data Volume)

**User Story:** As a maintenance coordinator, I want a single verb
that enables encryption on either the boot volume (FileVault) or a
data volume (APFS encryption), so that infrastructure code does not
need to branch on volume kind.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_encryption(volume, spec)` AND
   the volume is the boot volume (mount point `/` or token `boot`)
   THE SYSTEM SHALL dispatch
   `fdesetup enable -user <user> -inputplist` through the command
   runner with `elevated=True` when configured, with the username +
   passphrase plist on stdin.
2. WHEN the coordinator calls `enable_encryption(volume, spec)` AND
   the volume is a data volume THE SYSTEM SHALL dispatch
   `diskutil apfs encryptVolume <volume> -user <user>` with the
   passphrase delivered via runner `stdin`.
3. WHEN the volume is already encrypted THE SYSTEM SHALL skip
   execution and return
   `MacosEncryptionActionResult(status=SKIPPED)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosEncryptionActionResult(status=DRY_RUN)`.
5. WHEN `spec.passphrase` is None THE SYSTEM SHALL return
   `MacosEncryptionActionResult(status=FAILED, detail="passphrase_required")`
   and SHALL NOT dispatch any command.
6. WHEN `spec.user` is None or empty THE SYSTEM SHALL return
   `MacosEncryptionActionResult(status=FAILED, detail="user_required")`
   and SHALL NOT dispatch any command.
7. WHEN the operation targets the boot volume THE SYSTEM SHALL set
   `requires_reboot=True` on the returned result.
8. THE SYSTEM SHALL NEVER include the passphrase value in the
   recorded `command` field on the returned result.

---

### Requirement 2: Disable Encryption (Boot or Data Volume)

**User Story:** As a maintenance coordinator, I want a guarded path
to disable encryption on a volume, so that decommissioning is
auditable and accidental destruction is impossible.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_encryption(volume)` AND the
   volume is the boot volume THE SYSTEM SHALL dispatch
   `fdesetup disable` with `elevated=True` when configured.
2. WHEN the coordinator calls `disable_encryption(volume)` AND the
   volume is a data volume THE SYSTEM SHALL dispatch
   `diskutil apfs decryptVolume <volume>` with `elevated=True`
   when configured.
3. WHEN `config.allow_destructive` is False THE SYSTEM SHALL NOT
   dispatch the disable command and SHALL return
   `MacosEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`.
4. WHEN the volume is already plaintext THE SYSTEM SHALL skip
   execution and return
   `MacosEncryptionActionResult(status=SKIPPED)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosEncryptionActionResult(status=DRY_RUN)`.
6. WHEN the operation targets the boot volume THE SYSTEM SHALL set
   `requires_reboot=True`.

---

### Requirement 3: Get Encryption Status for a Volume

**User Story:** As a maintenance coordinator, I want to query the
encryption status of a single volume, so that I can branch on
encrypted / unlocked / cipher fields without parsing
`fdesetup` / `diskutil` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_encryption_status(volume)` AND
   the volume is the boot volume THE SYSTEM SHALL dispatch
   `fdesetup status` and parse the FileVault on/off + progress lines.
2. WHEN the coordinator calls `get_encryption_status(volume)` AND
   the volume is a data volume THE SYSTEM SHALL dispatch
   `diskutil info -plist <volume>` and parse the
   `Encryption` / `FileVault` / `Locked` keys.
3. THE SYSTEM SHALL return a `MacosEncryptionStatus` populated with
   `is_encrypted`, `is_unlocked`, `cipher` (`"aes-xts"` when
   encrypted, `None` otherwise), `key_size` (always None — macOS
   tools do not surface the key size), `progress_percent` (when
   FileVault is mid-conversion), and `protector_types`.
4. THE SYSTEM SHALL NEVER pass `elevated=True` to the runner for
   this verb.

---

### Requirement 4: List Encrypted Volumes

**User Story:** As a maintenance coordinator, I want to enumerate
every encrypted APFS volume on the host, so that I can compare
actual encryption state against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_encrypted_volumes()` THE
   SYSTEM SHALL dispatch `diskutil apfs list -plist` through the
   command runner WITHOUT elevation.
2. THE SYSTEM SHALL parse the plist tree and emit one
   `MacosEncryptedVolumeInfo` per volume whose `Encryption` (or
   `FileVault`) field is True.
3. THE SYSTEM SHALL NEVER include plaintext volumes in the
   returned list.
4. WHEN `diskutil` exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.

---

### Requirement 5: Rotate the Personal Recovery Key

**User Story:** As a maintenance coordinator, I want to rotate the
FileVault personal recovery key, so that compromised credentials
can be replaced.

#### Acceptance Criteria

1. WHEN the coordinator calls `rotate_key(volume)` AND the volume
   is the boot volume THE SYSTEM SHALL dispatch
   `fdesetup changerecovery -personal` with `elevated=True` when
   configured.
2. WHEN the coordinator calls `rotate_key(volume)` AND the volume
   is a data volume THE SYSTEM SHALL return
   `MacosEncryptionActionResult(status=FAILED,
   detail="rotate_key_unsupported_for_data_volume_on_macos")`
   without dispatching any command.  macOS native tooling does not
   expose a recovery-key rotation verb for APFS data volumes.
3. THE SYSTEM SHALL NOT skip rotation for the boot volume.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 6: Back Up the Recovery Key

**User Story:** As a maintenance coordinator, I want to back up the
FileVault personal recovery key to a filesystem path, so that a
copy is available for recovery in the event of credential loss.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_recovery_key(volume, path)`
   AND the volume is the boot volume THE SYSTEM SHALL dispatch
   `fdesetup showrecoverykey` to capture the recovery key on
   stdout, then dispatch `tee <path>` with the recovery key
   delivered via runner `stdin`.
2. WHEN the coordinator calls `backup_recovery_key(volume, path)`
   AND the volume is a data volume THE SYSTEM SHALL return
   `MacosEncryptionActionResult(status=FAILED,
   detail="backup_recovery_key_unsupported_for_data_volume_on_macos")`
   without dispatching any command.
3. THE SYSTEM SHALL NEVER include the recovery key value in the
   recorded `command` field on the returned result.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `MacosEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 7: Unlock an Encrypted Data Volume

**User Story:** As a maintenance coordinator, I want to unlock a
locked APFS volume by passing a passphrase, so that the plaintext
filesystem becomes available.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_volume(volume, passphrase)`
   AND the volume is a data volume THE SYSTEM SHALL dispatch
   `diskutil apfs unlockVolume <volume>` with the passphrase
   delivered via runner `stdin`.
2. WHEN the volume is already unlocked (probed via
   `diskutil info -plist`'s `Locked` key) THE SYSTEM SHALL skip
   execution and return
   `MacosEncryptionActionResult(status=SKIPPED)`.
3. WHEN the volume is the boot volume THE SYSTEM SHALL skip
   execution and return
   `MacosEncryptionActionResult(status=SKIPPED,
   detail="boot_volume_unlock_is_implicit")`.
4. THE SYSTEM SHALL NEVER include the passphrase value in the
   recorded `command` field on the returned result.

---

### Requirement 8: Boot vs Data Volume Routing

**User Story:** As a maintenance coordinator, I want a single
`MacosEncryptionManager` API regardless of whether the target is
the boot volume or a data volume, so that infrastructure code does
not hard-code the underlying tool.

#### Acceptance Criteria

1. THE SYSTEM SHALL classify a volume as `BOOT` when its
   identifier is `/`, `/System`, `boot`, or `system`.
2. THE SYSTEM SHALL classify every other volume identifier as
   `DATA`.
3. THE SYSTEM SHALL route `BOOT` verb calls through `fdesetup`.
4. THE SYSTEM SHALL route `DATA` verb calls through `diskutil apfs`.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
verb to honour the configured elevation flag without having to
re-pass it to each call, so that infrastructure code does not
duplicate `sudo`-style routing.

#### Acceptance Criteria

1. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb.
2. WHEN performing read-only operations
   (`get_encryption_status`, `list_encrypted_volumes`, all
   idempotency probes) THE SYSTEM SHALL NEVER pass
   `elevated=True`.
3. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.

---

### Requirement 10: Passphrase / Recovery-Key Hygiene

**User Story:** As a security-conscious operator, I want to be
certain that no passphrase or recovery-key byte ever appears in a
recorded argv, log line, or audit capture from this manager, so
that secrets do not leak through ordinary observability.

#### Acceptance Criteria

1. THE SYSTEM SHALL pass every passphrase via the runner's
   `stdin` parameter, either directly (data-volume verbs) or
   wrapped in an `fdesetup -inputplist` payload (boot-volume
   verbs).
2. THE SYSTEM SHALL NEVER include a passphrase value as a
   positional argument on any argv.
3. THE SYSTEM SHALL NEVER include a recovery-key value as a
   positional argument on any argv.
4. THE SYSTEM SHALL NEVER include a passphrase or recovery-key
   value in the `command` field on any returned
   `MacosEncryptionActionResult`.

---

### Requirement 11: Destructive Operation Guard

**User Story:** As a security-conscious operator, I want
destructive encryption operations to be opt-in, so that an
accidental call to a destructive verb cannot wipe a key slot or
trigger a multi-hour decrypt pass.

#### Acceptance Criteria

1. THE SYSTEM SHALL treat `disable_encryption` as a destructive
   verb on both boot and data volumes.
2. WHEN `config.allow_destructive` is False AND the destructive
   verb is invoked AND the volume is currently encrypted THE
   SYSTEM SHALL return
   `MacosEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`
   without dispatching the destructive command.
3. WHEN `config.allow_destructive` is True THE SYSTEM SHALL
   dispatch the destructive command after verifying the
   idempotency guard.

---

### Requirement 12: Observability

**User Story:** As a maintenance coordinator, I want the manager's
result objects to carry enough metadata to drive cross-OS
reporting without OS-specific branching, so that operator-facing
dashboards remain shape-stable.

#### Acceptance Criteria

1. THE SYSTEM SHALL populate every `MacosEncryptionActionResult`
   with `volume`, `device` (when resolvable), `command`, and
   `status`.
2. THE SYSTEM SHALL set `requires_reboot=True` on every result
   that targeted the boot volume.
3. THE SYSTEM SHALL surface the runner's exit code, stdout, and
   stderr on every result whose status is `SUCCESS` or `FAILED`.
