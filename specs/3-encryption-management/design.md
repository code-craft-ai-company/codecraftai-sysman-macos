# Design

## Concept Mapping (FileVault / APFS encryption ↔ Linux LUKS ↔ Windows BitLocker)

The cross-OS encryption-management contract talks about *encrypted
volumes*, *passphrases*, and *recovery keys*.  macOS is the outlier
among the Wave-2.2 implementations because the boot volume and data
volumes use different native tooling: `fdesetup` for FileVault on the
boot volume, `diskutil apfs` for APFS encryption on data volumes.
The table below grounds the abstract contract in concrete macOS /
Linux / Windows terms so a reader can compare the three Wave-2.2
implementations side-by-side.

| Contract concept | macOS (this spec) | Ubuntu | Windows |
|---|---|---|---|
| Backend | FileVault (`fdesetup`) for boot; APFS encryption (`diskutil apfs`) for data | LUKS (`cryptsetup`) | BitLocker (PowerShell `BitLocker` module) |
| Volume identity | Mount point (`/`, `/Volumes/Data`) or APFS volume UUID | Block-device path (`/dev/sda3`) plus mapping name | Drive letter (`C:`) |
| Format verb | `fdesetup enable -user <user> -inputplist` (boot) or `diskutil apfs encryptVolume <volume> -user <user>` (data) | `cryptsetup luksFormat --type luks2 --cipher <c> --key-size <k>` | `Enable-BitLocker -EncryptionMethod XtsAes256 -PasswordProtector` |
| Erase / disable verb | `fdesetup disable` (boot, non-destructive) or `diskutil apfs decryptVolume <volume>` (data, DESTRUCTIVE) | `cryptsetup erase <device>` (DESTRUCTIVE) | `Disable-BitLocker -MountPoint <letter>:` |
| Status verb | `fdesetup status` (boot) or `diskutil info -plist <volume>` (data) | `cryptsetup luksDump <device>` + `cryptsetup status <name>` | `Get-BitLockerVolume -MountPoint <letter>:` |
| List verb | `diskutil apfs list -plist` filtered to encrypted volumes | `lsblk -J ...` filtered to `crypto_LUKS` | `Get-BitLockerVolume \| ConvertTo-Json` |
| Rotate-key verb | `fdesetup changerecovery -personal` (boot only) | `cryptsetup luksChangeKey` (old + new on stdin) | `Add-BitLockerKeyProtector` then `Remove-BitLockerKeyProtector` (old) |
| Backup recovery key verb | `fdesetup showrecoverykey` then `tee <path>` | `cryptsetup luksHeaderBackup --header-backup-file <path>` | `(Get-BitLockerVolume).KeyProtector` filtered to `RecoveryPassword` then write to path |
| Unlock verb | `diskutil apfs unlockVolume <volume>` (data only — boot is implicit) | `cryptsetup luksOpen <device> <name>` | `Unlock-BitLocker -MountPoint <letter>: -Password $secure` |
| Passphrase delivery | runner `stdin` (NEVER on argv); fdesetup uses an XML plist on stdin via `-inputplist` | runner `stdin` (NEVER on argv) | inside the PowerShell scriptblock; recorded argv has the literal redacted via regex |
| Destructive guard | `config.allow_destructive` gates `disable_encryption` (both kinds) | `config.allow_destructive` gates `disable_encryption` | `config.allow_destructive` gates `Disable-BitLocker` |
| Elevation | `sudo fdesetup ...` / `sudo diskutil ...` for writes; reads need no elevation | `sudo cryptsetup ...` for writes; reads need no elevation | UAC token / Administrator session for writes |

The macOS implementation is split internally between two code paths
(boot via fdesetup, data via diskutil) but exposes a single verb set
to callers — the manager classifies the volume identifier on every
call and dispatches the right tool.

---

## Overview

The encryption-management feature is implemented as a single module,
`automojo.sysman.macos.encryptionmanagement`, that exposes a
`MacosEncryptionManager` class.  The coordinator instantiates
`MacosEncryptionManager` and calls its methods to perform discrete
encryption actions: enable / disable encryption, query status, list
encrypted volumes, rotate the recovery key, back up the recovery
key, and unlock a data volume.

This feature **complements — and does not duplicate** —
`storage-management` (partitioning, APFS container creation),
`filesystem-management` (mount / unmount, fstab),
`package-management` (the `fdesetup` / `diskutil` binaries are part
of the macOS base system), and `service-management`.  The
`MacosEncryptionManager` brings encryption state into the desired
shape; everything outside that verb set lives in another feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `MacosEncryptionManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs argument lists and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without administrator
access or a live `fdesetup` / `diskutil` binary.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via
`SSHCommandRunner`).

**See Also:**
[Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and
aspect configuration.  See `3-storage-management` for partitioning
and `3-filesystem-management` for mount points.  The cross-OS
contract is at
`automojo-sysman-common/specs/contracts/encryption-management/contract.md`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Supplies passphrase / recovery-key bytes                 │
│  - Sets allow_destructive=True only when destructive intent │
│    is explicit                                              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosEncryptionManager                           │
│  automojo.sysman.macos.encryptionmanagement                 │
│                                                             │
│  enable_encryption()       disable_encryption()             │
│  get_encryption_status()                                    │
│  list_encrypted_volumes()                                   │
│  rotate_key()              backup_recovery_key()            │
│  unlock_volume()                                            │
│                                                             │
│  Internal:                                                  │
│    _classify_volume() — boot vs data                        │
│    _is_encrypted()    — fdesetup or diskutil probe          │
│    _diskutil_info()   — plist parse                         │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner.run(args, elevated=, stdin=<secret>)
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosEncryptionManager`

**Location:**
`source/packages/automojo/sysman/macos/encryptionmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `enable_encryption` | `(volume, spec) → MacosEncryptionActionResult` | Routes to `fdesetup enable -inputplist` (boot) or `diskutil apfs encryptVolume` (data); passphrase via stdin; idempotent via boot/data status probe |
| `disable_encryption` | `(volume) → MacosEncryptionActionResult` | Routes to `fdesetup disable` (boot) or `diskutil apfs decryptVolume` (data); DESTRUCTIVE; gated by `config.allow_destructive` |
| `get_encryption_status` | `(volume) → MacosEncryptionStatus` | Routes to `fdesetup status` (boot) or `diskutil info -plist` (data) |
| `list_encrypted_volumes` | `() → list[MacosEncryptedVolumeInfo]` | `diskutil apfs list -plist`; emits one row per encrypted volume |
| `rotate_key` | `(volume) → MacosEncryptionActionResult` | `fdesetup changerecovery -personal` (boot only — data volumes return FAILED with a descriptive detail) |
| `backup_recovery_key` | `(volume, path) → MacosEncryptionActionResult` | `fdesetup showrecoverykey` then `tee <path>` with the key on stdin (boot only) |
| `unlock_volume` | `(volume, passphrase) → MacosEncryptionActionResult` | `diskutil apfs unlockVolume <volume>` with passphrase via stdin (data only — boot returns SKIPPED) |

### `MacosEncryptionManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched; the would-have-been argv is recorded |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `allow_destructive` | `bool` | `False` | When `True`, `disable_encryption` is permitted to dispatch the underlying tool |
| `default_cipher` | `str` | `"aes-xts"` | Recorded onto status / result records when the underlying tool does not surface a cipher token |

### `MacosEncryptionVolumeKind`

`BOOT` for the system boot volume (mount point `/`, `/System`, or
the literal `boot` / `system` token).  `DATA` for everything else.
The classifier lives at `MacosEncryptionManager._classify_volume`
and is called on every verb entry.

---

## Data Models

### Result and status types

```python
class MacosEncryptionActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class MacosEncryptionActionResult:
    status: MacosEncryptionActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None              # SANITISED — never carries a passphrase
    volume: str | None
    device: str | None
    requires_reboot: bool                  # True for boot-volume verbs
```

### Spec / status / list types

```python
@dataclass
class MacosEncryptionSpec:
    cipher: str | None = None              # reserved — FileVault / APFS are fixed-cipher
    key_size: int | None = None            # reserved
    passphrase: str | None = None          # required for enable_encryption
    recovery_key_protector: bool = True
    mount_point: str | None = None         # informational; this feature does NOT mount
    user: str | None = None                # required for enable_encryption

@dataclass
class MacosEncryptionStatus:
    volume: str
    is_encrypted: bool
    is_unlocked: bool
    cipher: str | None
    key_size: int | None                   # always None — macOS tools do not surface key size
    progress_percent: float | None
    protector_types: list[str]             # ["user", "recovery"] for FileVault, ["user", "passphrase"] for APFS

@dataclass
class MacosEncryptedVolumeInfo:
    volume: str
    device: str
    mount_point: str | None
    is_encrypted: bool
    is_unlocked: bool
    cipher: str | None
    key_size: int | None
    progress_percent: float | None
    protector_types: list[str]
```

---

## Key Method Designs

### `enable_encryption(volume, spec)`

1. Classify the volume.  Build the boot-or-data argv per the
   classification.
2. If `dry_run`, return `DRY_RUN`.
3. Validate `spec.passphrase` and `spec.user` — return FAILED with a
   descriptive detail when either is missing.
4. Probe the encrypted state.  Return `SKIPPED` when already
   encrypted.
5. For boot: build the `<plist>` payload via :func:`plistlib.dumps`
   carrying `Username` and `Password` keys.  Dispatch
   `fdesetup enable -user <user> -inputplist` with the plist on
   stdin.
6. For data: dispatch
   `diskutil apfs encryptVolume <volume> -user <user>` with the
   passphrase on stdin.
7. Mark `requires_reboot=True` for boot results.

### `disable_encryption(volume)`

1. Classify the volume.  Build the boot-or-data argv.
2. If `dry_run`, return `DRY_RUN`.
3. Probe encrypted state; return `SKIPPED` when already plaintext.
4. Enforce the destructive guard: when
   `config.allow_destructive=False`, return FAILED carrying
   `_DESTRUCTIVE_GUARD_DETAIL` without dispatching.
5. Dispatch the disable command.

### `get_encryption_status(volume)`

1. Classify the volume.
2. For boot: dispatch `fdesetup status`.  Pure-Python parser detects
   on/off and progress percent.  Boot is always unlocked while the
   system is running.
3. For data: dispatch `diskutil info -plist <volume>`.  Pure-Python
   plist parser extracts `Encryption` / `FileVault` / `Locked` keys.
4. Build the `MacosEncryptionStatus`.  Cipher defaults to
   `"aes-xts"` when encrypted; the actual cipher is fixed by
   FileVault / APFS, so the default is correct rather than a guess.

### `list_encrypted_volumes()`

1. Dispatch `diskutil apfs list -plist`.
2. Walk `Containers[].Volumes[]`.
3. Filter to entries where `Encryption` or `FileVault` is True.
4. Build one `MacosEncryptedVolumeInfo` per surviving entry.

### `rotate_key(volume)`

1. Classify; reject with FAILED+detail when the volume is `DATA`
   (macOS native tooling does not expose APFS-data rotation).
2. Build `fdesetup changerecovery -personal` argv.
3. If `dry_run`, return `DRY_RUN`.
4. Dispatch.  No idempotency guard (unconditional).

### `backup_recovery_key(volume, path)`

1. Classify; reject with FAILED+detail when the volume is `DATA`.
2. Build `fdesetup showrecoverykey` argv.
3. If `dry_run`, return `DRY_RUN`.
4. Dispatch.  Capture `stdout` from the runner.
5. Dispatch `tee <path>` with the captured key on stdin.
6. Wrap the tee result.  The first command (showrecoverykey) is
   recorded on the result's `command` field; the tee call is an
   internal implementation detail.

### `unlock_volume(volume, passphrase)`

1. Classify; return SKIPPED with
   `detail="boot_volume_unlock_is_implicit"` when boot.
2. Build `diskutil apfs unlockVolume <volume>` argv.
3. If `dry_run`, return `DRY_RUN`.
4. Probe `diskutil info -plist`'s `Locked` key.  When `False`,
   return SKIPPED.
5. Dispatch with `stdin=passphrase`.

---

## Boot vs Data Volume Routing

macOS is unique among the Wave-2.2 OSes in that its boot-volume
encryption (FileVault) and data-volume encryption (APFS) use
different native tools.  The manager classifies the `volume`
argument on every verb entry:

| Volume identifier | Classification | Tool |
|---|---|---|
| `/` | `BOOT` | `fdesetup` |
| `/System` | `BOOT` | `fdesetup` |
| `boot`, `system` (case-insensitive) | `BOOT` | `fdesetup` |
| `/Volumes/<name>` | `DATA` | `diskutil apfs` |
| `/dev/disk2s5` | `DATA` | `diskutil apfs` |
| `<APFS-uuid>` | `DATA` | `diskutil apfs` |
| anything else | `DATA` | `diskutil apfs` |

Some verbs are intentionally one-sided:

- `rotate_key` and `backup_recovery_key` operate ONLY on the boot
  volume (fdesetup-only).  Data-volume calls return FAILED with a
  descriptive detail.
- `unlock_volume` operates ONLY on data volumes.  Boot-volume calls
  return SKIPPED with `detail="boot_volume_unlock_is_implicit"`.

---

## Passphrase / Recovery-Key Handling

Passphrase bytes flow through the runner's `stdin` parameter; they
NEVER appear on argv.  Two delivery patterns exist:

**Direct stdin** (data-volume verbs).  `diskutil apfs encryptVolume`
and `diskutil apfs unlockVolume` read the passphrase interactively
when the `-passphrase` flag is omitted; the runner satisfies that
prompt by writing the passphrase string to stdin:

```python
self._runner.run(
    ["diskutil", "apfs", "encryptVolume", volume, "-user", user],
    elevated=True,
    stdin=passphrase,             # <- passphrase here
)
```

**Plist stdin** (boot-volume `enable_encryption`).  `fdesetup
enable -inputplist` consumes a username / password dictionary as a
plist on stdin.  The manager builds the plist via
:func:`plistlib.dumps`:

```python
payload = plistlib.dumps({
    "Username": user,
    "Password": passphrase,
}).decode("utf-8")
self._runner.run(
    ["fdesetup", "enable", "-user", user, "-inputplist"],
    elevated=True,
    stdin=payload,                # <- plist carrying passphrase
)
```

In both cases the recorded `command` field on the
`MacosEncryptionActionResult` contains only the argv that was
actually dispatched.  Because the passphrase never appears on argv,
the field is automatically free of secrets — no post-hoc
sanitisation step is required.  The Linux Ubuntu sibling uses the
same direct-stdin pattern; the Windows sibling — which builds a
PowerShell scriptblock that embeds the passphrase as a literal
inside the script — handles the same problem differently and
applies a regex-based redaction step before recording the argv.

The recovery-key backup flow has its own hygiene contract.
`fdesetup showrecoverykey` returns the key on stdout; the manager
captures it into a local Python variable, immediately writes it to
the destination path via `tee` on stdin, and never holds it in a
named field beyond the local frame.

---

## Destructive Operation Guard

`disable_encryption` is destructive in the data-volume case:
`diskutil apfs decryptVolume` rewrites every block on the volume
during the decryption pass.  The manager refuses to dispatch the
disable command unless `config.allow_destructive=True`; otherwise
it returns `FAILED` carrying
`detail="destructive_operation_requires_explicit_confirm"`.

The same guard applies to the boot-volume case
(`fdesetup disable`).  Boot-volume disable is technically
non-destructive in that the data is preserved in plaintext; the
guard exists to keep cross-OS behaviour uniform.  Coordinators
that drive a "factory-reset" workflow are expected to set the flag
in their own config layer.

---

## Dry-Run Behaviour

Dry-run disables: `enable_encryption`, `disable_encryption`,
`rotate_key`, `backup_recovery_key`, `unlock_volume`.

Dry-run allows: `get_encryption_status`, `list_encrypted_volumes`
(read-only).

---

## Safety and Correctness Properties

1. **No passphrase on argv.** Every passphrase flows on `stdin`,
   either directly or wrapped in a plist.
2. **Idempotent enable.** `enable_encryption` returns SKIPPED when
   the volume is already encrypted.
3. **Destructive guard.** `disable_encryption` requires
   `config.allow_destructive=True`.
4. **Idempotent unlock.** `unlock_volume` returns SKIPPED when the
   volume is already unlocked.
5. **Boot vs data routing.** Every verb classifies the volume on
   entry and dispatches the right native tool.
6. **Pure-Python parsing.** `fdesetup status`, `diskutil info`,
   and `diskutil apfs list` parsers run in pure Python via
   :mod:`plistlib`.
7. **Read-only reads.** `get_encryption_status` and
   `list_encrypted_volumes` never dispatch a write and never pass
   `elevated=True`.

---

## Testing Strategy

- `_MockCommandRunner` records every `run` call (args + elevation
  flag + stdin payload) and answers from a pre-seeded `results`
  dict keyed by argv tuple.
- Per-verb tests for command construction, idempotency (SKIPPED),
  dry-run (`DRY_RUN`), and FAILED-on-non-zero.
- Boot-vs-data routing tests for `enable_encryption`,
  `disable_encryption`, and `unlock_volume`.
- Passphrase-hygiene test that scans every recorded argv and
  asserts no plaintext passphrase appears.
- Elevation tests that assert writes pass `elevated=True` (per
  config) and reads do not.
- Module-level parser tests for `fdesetup status` and the plist
  parsers.
- Destructive-guard test that asserts
  `disable_encryption(allow_destructive=False)` returns FAILED
  without dispatching the disable command.
- No live `fdesetup` or `diskutil` binary required.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
└── encryptionmanagement.py

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_encryptionmanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Partitioning / APFS containers | `3-storage-management` | Coordinator runs storage first; this feature encrypts the resulting APFS volume |
| Mount / unmount | `3-filesystem-management` | Coordinator mounts the unlocked volume after this feature unlocks it |
| Boot-time unlock (SecureToken / FileVault) | macOS native flow | Implicit — the bootloader unlocks the boot volume before this feature runs; this feature never re-unlocks the boot volume |
| MDM / institutional recovery key | macOS native MDM channel | Reserved as macOS extension |
| Sealed System Volume cryptography | macOS base system | Out of scope — this feature does NOT touch the sealed system volume |
| Credential / passphrase routing | `automojo-credentials` | Caller passes the passphrase string into the verbs; bytes flow on `stdin` to the runner |
