# Requirements

## Introduction

The bootloader-management feature provides the
`automojo-sysman-macos` package with the ability to manage the
**OS-level bootloader configuration layer** on macOS.  It implements
the cross-OS contract defined at
`automojo-sysman-common/specs/contracts/bootloader-management/contract.md`.

### What this feature is NOT

This feature is **distinct from `boot-management`** (the UEFI
firmware boot-entry feature in `automojo-sysman-uefi`).  On Apple
Silicon there is no UEFI surface at all; on Intel Macs the firmware
boot entry is owned by Apple's iBoot rather than user-space tools.
This feature addresses the **OS-level bless / NVRAM-boot-args layer
that sits between the firmware and the kernel**.

| Feature | Layer | Tools |
|---------|-------|-------|
| `boot-management` (UEFI) | Firmware boot entries | `efibootmgr`, `bcdedit /enum firmware` |
| `bootloader-management` (this) | OS-level bless + NVRAM | `bless`, `nvram` |

### macOS bootloader model

macOS does not expose a multi-entry boot menu the way GRUB or
systemd-boot does.  The native primitives are:

1. **`bless`** -- marks (blesses) a volume as bootable and
   optionally selects it as the active boot device via the
   ``--setBoot`` flag.
2. **`nvram`** -- exposes NVRAM variables (notably ``boot-args``)
   that influence how the kernel launches.  This module reads
   ``boot-args`` to populate the active entry's `cmdline` field but
   never writes it.
3. **`/Library/Preferences/SystemConfiguration/com.apple.Boot.plist`**
   -- pre-Apple-Silicon root-disk loader configuration.  Honoured
   by Intel Macs.  This module does not touch the plist directly;
   the ``bless`` tool owns it.

### Implication for the cross-OS contract

* "Boot entries" on macOS are blessed volumes, not menu items.
* `add_entry` accepts only ``MacosBootEntryKind.BLESS_VOLUME``
  specs; other kinds are rejected with `FAILED`.
* `update_bootloader_config` and `regenerate_config` are
  OS-managed -- macOS rebuilds its own loader caches when
  `bless` runs.  Both verbs return a `SUCCESS` no-op carrying a
  descriptive `message`.
* `remove_entry` runs `bless --unbless`, which has limited support
  on sealed system volumes; the runner surfaces the resulting
  permission error to the caller.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface.  All
system commands (`bless`, `nvram`) are executed through the
`automojo-interfaces` command-runner abstraction.  All mutating
operations respect dry-run mode.

---

## Requirements

### Requirement 1: List Boot Entries

**User Story:** As a maintenance coordinator, I want to enumerate
the volumes the macOS firmware considers bootable so that I can
reason about the host's boot surface.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_entries()` THE SYSTEM SHALL
   invoke `bless --info --verbose` via the runner without
   elevation.
2. THE SYSTEM SHALL also invoke `nvram boot-args` to populate the
   active entry's `cmdline` field.
3. THE SYSTEM SHALL return a single `MacosBootEntryInfo` for the
   resolved active boot volume, with `is_default=True`.
4. WHEN `bless --info` reports no boot path AND
   `config.default_volume` is `None` THE SYSTEM SHALL return an
   empty list.
5. WHEN `bless --info` reports no boot path AND
   `config.default_volume` is set THE SYSTEM SHALL synthesise a
   single entry pointing at the configured fallback volume.
6. WHEN `bless --info` fails for an unexpected reason THE SYSTEM
   SHALL raise `MacosBootloaderQueryError`.
7. THE SYSTEM SHALL never pass `elevated=True` for any of these
   reads.

---

### Requirement 2: Get Default Entry

**User Story:** As a maintenance coordinator, I want to know which
volume is currently the active boot device so that I can compare it
against the maintenance baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_default_entry()` THE SYSTEM
   SHALL prefer `bless --info --getBoot` (terse form) and SHALL
   fall back to parsing the verbose form when the terse form fails
   or returns empty.
2. THE SYSTEM SHALL populate the entry's `cmdline` from the
   `nvram boot-args` payload.
3. WHEN no boot device can be resolved THE SYSTEM SHALL raise
   `MacosBootloaderQueryError`.
4. THE SYSTEM SHALL never pass `elevated=True`.

---

### Requirement 3: Set Default Entry

**User Story:** As a maintenance coordinator, I want to change the
active boot volume so that the next reboot lands on the declared
volume.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_default_entry(entry_id=<path>)`
   THE SYSTEM SHALL invoke
   `bless --mount <path> --setBoot` via the runner with elevated
   privileges.
2. WHEN the supplied path is already the active boot device THE
   SYSTEM SHALL return a `MacosBootloaderActionResult` with status
   `SKIPPED` and SHALL NOT issue the `bless` call.
3. WHEN the `bless` call fails THE SYSTEM SHALL return
   `MacosBootloaderActionResult(status=FAILED, exit_code=<code>,
   stderr=<captured>)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.
5. THE SYSTEM SHALL set `requires_reboot=True` on success.

---

### Requirement 4: Add Boot Entry

**User Story:** As a maintenance coordinator, I want to bless a
recovery / alternate volume so that the firmware will accept it as
a bootable target.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `add_entry(spec=MacosBootEntrySpec(...))` AND
   `spec.entry_kind=BLESS_VOLUME` THE SYSTEM SHALL invoke
   `bless --mount <volume_path>` via the runner with elevated
   privileges.
2. WHEN `spec.entry_kind` is anything other than `BLESS_VOLUME`
   THE SYSTEM SHALL return `FAILED` with `stderr` token
   `unsupported_entry_kind` and SHALL NOT invoke `bless`.
3. WHEN `spec.volume_path` is `None` or empty THE SYSTEM SHALL
   return `FAILED` with `stderr` token `invalid_entry_spec` and
   SHALL NOT invoke `bless`.
4. WHEN the supplied volume is already the active boot device
   THE SYSTEM SHALL return `SKIPPED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 5: Remove Boot Entry

**User Story:** As a maintenance coordinator, I want to unbless a
volume so that the firmware no longer treats it as bootable.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_entry(entry_id=<path>)` THE
   SYSTEM SHALL invoke `bless --unbless <path>` via the runner with
   elevated privileges.
2. WHEN the volume is not the active boot device (the only
   blessed-volume signal macOS exposes) THE SYSTEM SHALL return
   `SKIPPED`.
3. WHEN the host is a sealed system volume and `bless --unbless`
   surfaces a permission error THE SYSTEM SHALL return `FAILED`
   carrying the runner stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 6: Update Bootloader Config (No-Op)

**User Story:** As a maintenance coordinator, I want a single verb
that exists for cross-OS contract conformance even though macOS has
no user-driven update step.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_bootloader_config()` THE
   SYSTEM SHALL return `SUCCESS` without invoking the runner.
2. THE RESULT SHALL carry a `message` indicating that the
   bootloader is OS-managed and that no user-driven update step
   exists.
3. THE RESULT SHALL set `requires_reboot=False`.

---

### Requirement 7: Regenerate Config (No-Op)

**User Story:** As a maintenance coordinator, I want a single verb
that exists for cross-OS contract conformance even though macOS has
no user-driven regenerate step.

#### Acceptance Criteria

1. WHEN the coordinator calls `regenerate_config()` THE SYSTEM
   SHALL return `SUCCESS` without invoking the runner.
2. THE RESULT SHALL carry a `message` indicating the no-op
   rationale.
3. THE RESULT SHALL set `requires_reboot=False`.

---

### Requirement 8: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeat calls
to the same write verb to be no-ops when the host already matches
the desired state, so that maintenance runs are safe to re-execute.

#### Acceptance Criteria

1. `set_default_entry()` SHALL skip when the supplied path is
   already the active boot device.
2. `add_entry()` SHALL skip when the supplied path is already
   blessed (i.e. is the active boot device).
3. `remove_entry()` SHALL skip when the supplied path is not the
   active boot device.
4. `update_bootloader_config()` and `regenerate_config()` SHALL
   return SUCCESS unconditionally.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to
elevate when configured but reads to never elevate, so that the
manager works on hosts that do not run as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (`set_default_entry`, `add_entry`, `remove_entry`) THE SYSTEM
   SHALL pass `elevated=True` to the runner for the `bless` call.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read (`list_entries`, `get_default_entry`)
   THE SYSTEM SHALL never pass `elevated=True`, regardless of the
   config.
4. Idempotency probes (the `bless --info` reads) MUST never
   elevate.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run
mode that exercises the planning logic without touching the host.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND a write verb is invoked THE
   SYSTEM SHALL NOT call `runner.run` for the mutating step.
2. THE SYSTEM SHALL still return a `MacosBootloaderActionResult`
   with status `DRY_RUN` and the `command` field populated with the
   argv that would have been executed.
3. Read verbs SHALL execute unchanged in dry-run mode -- reads do
   not mutate.

---

### Requirement 11: Distinction From UEFI Boot-Management

**User Story:** As a maintenance coordinator, I want the
bootloader-management feature to never accidentally touch the UEFI
firmware NVRAM so that the firmware boot entries remain owned by
the platform.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER invoke `efibootmgr`.
2. THE SYSTEM SHALL NEVER read or write firmware boot variables
   directly.
3. THE SYSTEM SHALL document the distinction in the design.md and
   the module docstring so that downstream readers do not confuse
   the two layers.

---

### Requirement 12: Destructive-Change Safety

**User Story:** As a maintenance coordinator, I want clear signals
when a write changes the active boot device so that I can pair the
change with a backup-and-verify workflow.

#### Acceptance Criteria

1. THE SYSTEM SHALL set `requires_reboot=True` on every successful
   write result.
2. THE SYSTEM SHALL surface the underlying `bless` stderr verbatim
   on `FAILED` results so that operators can triage permission /
   sealed-volume errors.
3. THE SYSTEM SHALL document in the module docstring that
   misconfigurations can render the host unbootable and that
   callers SHOULD verify the resulting state via
   `get_default_entry` before rebooting.
