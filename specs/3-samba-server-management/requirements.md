# Requirements

## Introduction

The samba-server-management feature provides the
`codecraftai-sysman-macos` package with the ability to manage the
SMB-share surface on macOS hosts.  macOS ships its own ``smbd``
daemon as part of the base operating system, exposed under launchd
as ``com.apple.smbd``.  Unlike the Samba project's binary on
Ubuntu / RHEL, the macOS SMB-share table is NOT primarily managed
through ``/etc/smb.conf``; the canonical management tool is the
built-in ``sharing`` command (with the ``-s`` mask controlling
which protocols expose a directory).

This spec implements the cross-OS samba-server-management contract
defined at
``codecraftai-sysman-common/specs/contracts/samba-server-management/contract.md``
for macOS, scoped to ``smbd`` and the ``sharing`` CLI.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All
system commands (``sharing``, ``launchctl``, ``dscl``, optionally
``pdbedit``) are executed exclusively through the
``codecraftai-interfaces`` command-runner abstraction.  No
``subprocess``, no direct ``open``, and no direct ``pathlib`` calls
are made inside this module.

## macOS-specific scope

macOS does NOT use the Samba ``passdb.tdb`` user database that
Ubuntu's ``smbpasswd`` writes to; SMB authentication on macOS goes
through the local-user directory (``dscl . -create /Users/<name>``
or ``sysadminctl -addUser``).  This pilot therefore **delegates
SMB user creation / removal to ``user-group-management``** and
returns ``status=FAILED`` with an explanatory ``message`` and
``detail`` for ``add_samba_user`` and ``remove_samba_user``.  The
``delegate_user_mgmt=True`` flag on
:class:`MacosSambaManagerConfig` documents this behaviour and
allows future implementations to flip the switch when the local-
user surface is wired through.

This feature complements — and does not duplicate — the
``com.apple.smbd`` lifecycle owned by ``3-service-management``,
the SMB-port firewall surface owned by ``3-firewall-management``,
and the local-account surface owned by ``3-user-group-management``.

---

## Requirements

### Requirement 1: Add a Share

**User Story:** As a maintenance coordinator, I want to add an SMB
share to the macOS sharing system, so that the declared share set
matches the desired state and the daemon advertises the new share
immediately.

#### Acceptance Criteria

1. WHEN the coordinator calls ``add_share(spec)`` THE SYSTEM SHALL
   invoke ``sharing -a <path> -S <name> -s 100 -g <permission>``
   via the runner (the ``-s 100`` mask enables SMB only; ``-g``
   carries a guest-access octal flag; ``-S`` sets the SMB share
   name).
2. WHEN ``sharing -l`` already lists a share whose name matches
   ``spec.name`` and whose path matches ``spec.path`` THE SYSTEM
   SHALL return ``status=SKIPPED`` without invoking ``sharing -a``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   without invoking the runner.
4. WHEN ``sharing -a`` exits non-zero THE SYSTEM SHALL return
   ``status=FAILED`` with ``exit_code``, ``stdout``, ``stderr``,
   and ``command`` populated.

---

### Requirement 2: Remove a Share

**User Story:** As a maintenance coordinator, I want to remove an
SMB share from the macOS sharing system, so that the daemon stops
advertising the share.

#### Acceptance Criteria

1. WHEN the coordinator calls ``remove_share(name)`` THE SYSTEM
   SHALL invoke ``sharing -r <name>`` via the runner.
2. WHEN ``sharing -l`` does NOT list a share named ``name`` THE
   SYSTEM SHALL return ``status=SKIPPED`` without invoking
   ``sharing -r``.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.
4. WHEN ``sharing -r`` exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.

---

### Requirement 3: List Shares

**User Story:** As a maintenance coordinator, I want to enumerate
the currently-registered macOS SMB shares, so that I can compare
against the declared desired state and detect drift.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_shares()`` THE SYSTEM SHALL
   invoke ``sharing -l`` via the runner with ``elevated=False`` and
   parse the output into ``list[MacosSambaShareInfo]``.
2. WHEN ``sharing -l`` exits non-zero AND ``stderr`` is non-empty
   THE SYSTEM SHALL raise ``MacosSambaQueryError``.
3. THE SYSTEM SHALL skip records whose protocol mask explicitly
   excludes SMB (the ``sharing -l`` output reports per-protocol
   enable bits).

---

### Requirement 4: Get Share

**User Story:** As a maintenance coordinator, I want to fetch a
single share record by name.

#### Acceptance Criteria

1. WHEN the coordinator calls ``get_share(name)`` THE SYSTEM SHALL
   invoke ``sharing -l`` and return the ``MacosSambaShareInfo`` for
   the matching record.
2. WHEN no matching record exists THE SYSTEM SHALL return ``None``.
3. WHEN ``get_share`` is invoked THE SYSTEM SHALL pass
   ``elevated=False``.

---

### Requirement 5: Set Share Permissions

**User Story:** As a maintenance coordinator, I want to update the
ACL of an existing macOS SMB share.

#### Acceptance Criteria

1. WHEN the coordinator calls ``set_share_permissions(name, acl)``
   THE SYSTEM SHALL invoke ``dscl . -change /SharePoints/<name>
   smb_<key> <old> <new>`` for each principal-list change required
   to bring the share into line with ``acl``.
2. WHEN the existing principal lists already match ``acl`` (order-
   insensitive) THE SYSTEM SHALL return ``status=SKIPPED``.
3. WHEN no SharePoint exists for ``name`` THE SYSTEM SHALL return
   ``status=SKIPPED`` with ``message="share not present"``.
4. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.

---

### Requirement 6: Reload Config

**User Story:** As a maintenance coordinator, I want to ask the
running daemon to re-read its configuration, so that changes that
bypassed the manager (e.g., manual edits) take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls ``reload_config()`` THE SYSTEM SHALL
   invoke ``launchctl kickstart -k system/com.apple.smbd`` via the
   runner.
2. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``.
3. WHEN the runner exits 0 THE SYSTEM SHALL return ``status=SUCCESS``.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   ``status=FAILED``.
5. ``reload_config`` MUST NOT skip — it is operational and
   unconditional.

---

### Requirement 7: Add a Samba User (Delegated)

**User Story:** As a maintenance coordinator, I want to register
an SMB user, but on macOS this surface is the local user
directory.

#### Acceptance Criteria

1. WHEN ``delegate_user_mgmt=True`` THE SYSTEM SHALL return
   ``status=FAILED`` with ``detail="add_samba_user delegated"`` and
   ``message`` carrying the delegation explanation directing
   callers to the ``user-group-management`` feature.
2. THE SYSTEM SHALL NEVER place the password on argv.  The
   recorded ``command`` field on the returned result MUST NOT
   contain the password.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   with the sanitised would-have-been ``command``.

---

### Requirement 8: Remove a Samba User (Delegated)

#### Acceptance Criteria

1. WHEN ``delegate_user_mgmt=True`` THE SYSTEM SHALL return
   ``status=FAILED`` with the delegation explanation.

---

### Requirement 9: List Samba Users

**User Story:** As a maintenance coordinator, I want to enumerate
the registered SMB users, but on macOS this maps to the local-user
directory.

#### Acceptance Criteria

1. WHEN the coordinator calls ``list_samba_users()`` and a
   ``pdbedit`` binary is available on PATH (best-effort) THE
   SYSTEM SHALL invoke ``pdbedit -L -v`` and parse the output.
2. WHEN ``pdbedit`` is not available THE SYSTEM SHALL return an
   empty list (the macOS pilot does not enumerate local users
   through ``dscl`` by default).
3. WHEN ``pdbedit`` exits non-zero AND ``stderr`` is non-empty THE
   SYSTEM SHALL raise ``MacosSambaQueryError``.

---

### Requirement 10: Elevation Policy

#### Acceptance Criteria

1. WHEN ``elevated=True`` THE SYSTEM SHALL pass ``elevated=True``
   for every write verb.
2. WHEN performing reads (``list_shares``, ``get_share``,
   ``list_samba_users``) THE SYSTEM SHALL pass ``elevated=False``.

---

### Requirement 11: Dry-Run

#### Acceptance Criteria

1. WHEN ``dry_run=True`` THE SYSTEM SHALL NOT dispatch any
   mutating command.
2. WHEN ``dry_run=True`` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions.
3. WHEN ``dry_run=True`` THE SYSTEM SHALL return ``status=DRY_RUN``
   for every mutating verb with the would-have-been argv on the
   result.

---

### Requirement 12: Failure Reporting

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return ``status=FAILED`` with ``exit_code``,
   ``stdout``, ``stderr``, and ``command`` populated.
2. THE SYSTEM SHALL NEVER include passphrase material in the
   recorded ``command`` field on any returned result.

---

### Requirement 13: Idempotency Across Verbs

#### Acceptance Criteria

1. ``add_share`` MUST be idempotent on (name, path).
2. ``remove_share`` MUST skip when the share is not present.
3. ``set_share_permissions`` MUST skip when the existing ACL
   matches.
4. ``add_samba_user`` and ``remove_samba_user`` are delegated and
   MUST surface a ``FAILED`` result with the delegation message.
5. ``reload_config`` MUST NOT short-circuit.
