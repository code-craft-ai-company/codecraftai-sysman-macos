# Requirements

## Introduction

The identity management feature provides the
`automojo-sysman-macos` package with the ability to bind a macOS host
to a Microsoft Active Directory (AD) realm and resolve directory-
backed users and groups against the local Directory Services search
path.  This spec implements the cross-OS contract defined at
`automojo-sysman-common/specs/contracts/identity-management/contract.md`
for macOS using `dsconfigad` (AD bind / unbind / status), `dscl`
(Directory Services reads through the `/Search` node), and
`dscacheutil` plus a `killall -HUP mDNSResponder` kick (cache
invalidation).

A maintenance coordinator drives all operations by calling into this
feature's public surface.  The feature does not decide *which* realm
the host should bind to — it provides the capability to *manipulate
realm membership and inspect directory state* reliably, idempotently,
and with full auditability.  Every system command is executed
exclusively through the `automojo-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking
without requiring a live system.  Administrator passwords land on
the runner-bound argv (because `dsconfigad` accepts the password on
argv) but are scrubbed to the literal `<redacted>` token in the argv
recorded on the `MacosIdentityActionResult` so the password never
leaks into audit captures.

This feature does NOT manage local user accounts (see
`3-user-group-management`), does NOT provision MDM policy (out of
scope for the cross-OS contract), and does NOT manage CA trust
anchors (see `3-certificate-trust-management`).

---

## Requirements

### Requirement 1: Bind the host to an Active Directory realm

**User Story:** As a maintenance coordinator, I want to bind a
macOS host to an Active Directory realm using supplied admin
credentials, so that the host participates in the declared identity
directory.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosRealmJoinSpec` with
   `realm`, `admin_user`, and `admin_password` THE SYSTEM SHALL
   build a `dsconfigad -add <realm> -username <admin_user>
   -password <admin_password>` argv (extended with `-computer
   <spec.computer_name>`, `-ou <spec.ou_path>` (or
   `config.default_ou`), and `-preferred <spec.domain_controller>`
   when those fields are set, plus `-force` when `config.force`
   is True) and dispatch it through the runner with
   `elevated=True` when configured.
2. THE SYSTEM SHALL replace the value following each ``-password``
   flag in the recorded argv with the literal `<redacted>` so the
   password never leaks into audit captures.
3. WHEN `dsconfigad -show` already shows the host bound to
   `spec.realm` THE SYSTEM SHALL skip execution and return a
   `MacosIdentityActionResult` with `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return a `MacosIdentityActionResult` with
   `status=DRY_RUN` and the would-have-been (sanitised) argv in
   `command`.
5. WHEN `dsconfigad -add` succeeds THE SYSTEM SHALL return a
   `MacosIdentityActionResult` with `status=SUCCESS`,
   `realm=spec.realm`, `username=spec.admin_user`, and the captured
   `exit_code`, `stdout`, and `stderr`.
6. WHEN `dsconfigad -add` fails THE SYSTEM SHALL return a
   `MacosIdentityActionResult` with `status=FAILED`, the captured
   `exit_code` and `stderr`, and the sanitised argv in `command`.

---

### Requirement 2: Unbind the host from a realm

**User Story:** As a maintenance coordinator, I want to remove a
macOS host from a realm using supplied admin credentials, so that
decommissioned hosts no longer authenticate against the directory.

#### Acceptance Criteria

1. WHEN the coordinator calls `leave_realm(admin_user,
   admin_password, realm)` THE SYSTEM SHALL build a
   `dsconfigad -remove -username <admin_user> -password
   <admin_password>` argv (with `-force` when `config.force` is
   True) and dispatch it with `elevated=True` when configured.
2. THE SYSTEM SHALL sanitise the recorded argv just as for
   `join_realm`.
3. WHEN `dsconfigad -show` reports the host is not bound to any
   realm THE SYSTEM SHALL return `status=SKIPPED` without invoking
   `dsconfigad -remove`.
4. WHEN the caller supplies a non-`None` `realm` argument and the
   currently-bound realm differs (case-insensitive compare) THE
   SYSTEM SHALL return `status=SKIPPED` without invoking
   `dsconfigad -remove`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN`.
6. WHEN `dsconfigad -remove` succeeds THE SYSTEM SHALL return
   `status=SUCCESS`; on failure, `status=FAILED` with captured
   diagnostics.

---

### Requirement 3: Query realm-bind status

**User Story:** As a maintenance coordinator, I want to know whether
a macOS host is currently bound to a realm, which realm it is, and
the host's computer-account name, so that downstream logic can branch
on bind state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_realm_status()` THE SYSTEM SHALL
   dispatch `dsconfigad -show` through the runner without elevation.
2. WHEN the parsed output reports `Active Directory Domain = -`
   AND `Active Directory Forest = -` THE SYSTEM SHALL return
   `MacosRealmStatus(joined=False)` with every other field `None`.
3. WHEN the parsed output reports a non-dash domain THE SYSTEM
   SHALL return `MacosRealmStatus(joined=True)` with
   `current_realm` set to the domain (preferred) or forest, plus
   `realm_kind=MacosRealmKind.ACTIVE_DIRECTORY` and `computer_name`
   set to the parsed `Computer Account` line when not a dash.
4. WHEN `dsconfigad -show` exits non-zero THE SYSTEM SHALL raise
   `MacosIdentityQueryError` carrying the argv, exit code, and
   stderr.

---

### Requirement 4: List joined realms (single-realm reality)

**User Story:** As a maintenance coordinator that drives a multi-OS
rollout, I want a uniform list-of-realms verb on macOS, so that
my coordinator code does not have to special-case the platform.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_joined_realms()` THE SYSTEM
   SHALL return a list of zero or one `MacosRealmInfo` records.
2. WHEN the host is bound (per `get_realm_status`) THE SYSTEM SHALL
   return a single-element list whose entry carries the bound
   realm, `realm_kind`, and `is_default=True`.
3. WHEN the host is not bound THE SYSTEM SHALL return an empty
   list.
4. THE SYSTEM SHALL document that Open Directory binds are NOT
   enumerated by this method — callers needing OD node listing
   should call `dscl /Search -read /` directly.

---

### Requirement 5: Synchronise users from the directory

**User Story:** As a maintenance coordinator, I want to flush the
Directory Services cache so the next NSS lookup refreshes from the
directory, so that newly-added or recently-modified directory
accounts become visible without waiting.

#### Acceptance Criteria

1. WHEN the coordinator calls `sync_users()` THE SYSTEM SHALL
   dispatch `dscacheutil -flushcache` followed by `killall -HUP
   mDNSResponder` through the runner with `elevated=True` when
   configured.
2. WHEN the `dscacheutil` step exits non-zero THE SYSTEM SHALL
   skip the `killall` step and return `status=FAILED` with the
   captured `dscacheutil` diagnostics.
3. WHEN both steps run, THE SYSTEM SHALL map the final
   `killall` exit code to the result's `status` (zero → SUCCESS,
   non-zero → FAILED).
4. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking either step.

---

### Requirement 6: Resolve a user

**User Story:** As a maintenance coordinator, I want to resolve a
username through `dscl /Search`, so that downstream logic can
branch on existence and the user's UID / GID / home / shell / realm
without parsing dscl output itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_user(username)` THE SYSTEM
   SHALL dispatch `dscl /Search -read /Users/<username>` through
   the runner without elevation.
2. WHEN `dscl` exits zero THE SYSTEM SHALL parse the
   `Attribute: value` lines into a `MacosUserResolveResult` with
   `found=True` and populated `uid` (`UniqueID`), `gid`
   (`PrimaryGroupID`), `home` (`NFSHomeDirectory`), and `shell`
   (`UserShell`) fields.
3. WHEN the parsed `OriginalNodeName` line points at an
   `/Active Directory/<realm>/...` node THE SYSTEM SHALL set
   `realm` to the AD domain extracted from that path; otherwise
   `realm` SHALL be `None`.
4. WHEN `dscl` exits non-zero THE SYSTEM SHALL return a
   `MacosUserResolveResult` with `found=False`.

---

### Requirement 7: Resolve a group

**User Story:** As a maintenance coordinator, I want to resolve a
groupname through `dscl /Search`, so that downstream logic can
branch on existence, the group's GID, members, and realm without
parsing dscl output itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_group(groupname)` THE SYSTEM
   SHALL dispatch `dscl /Search -read /Groups/<groupname>` through
   the runner without elevation.
2. WHEN `dscl` exits zero THE SYSTEM SHALL parse the
   `Attribute: value` lines into a `MacosGroupResolveResult` with
   `found=True`, populated `gid`, and a `members` list parsed
   from the whitespace-split `GroupMembership` attribute.
3. WHEN the parsed `OriginalNodeName` line points at an AD node THE
   SYSTEM SHALL set `realm` accordingly; otherwise `realm` SHALL
   be `None`.
4. WHEN `dscl` exits non-zero THE SYSTEM SHALL return a
   `MacosGroupResolveResult` with `found=False`.

---

### Requirement 8: Dry-run, elevation, and credential handling

**User Story:** As a maintenance coordinator, I want every mutating
operation to respect dry-run, run with declared elevation, surface
failures structurally, and route credentials safely.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT contact a
   directory server and SHALL NOT dispatch any mutating command.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every
   write verb (`join_realm`, `leave_realm`, `sync_users`) with
   `elevated=True` to the runner.
3. WHEN `config.elevated=True` and the coordinator calls a read
   verb (`get_realm_status`, `list_joined_realms`, `resolve_user`,
   `resolve_group`) THE SYSTEM SHALL still dispatch the underlying
   probe with `elevated=False`.
4. THE SYSTEM SHALL replace every `-password <pw>` value in the
   recorded argv with `<redacted>` so plaintext passwords never
   reach audit captures.
5. WHEN the runner returns a non-zero exit code on a write THE
   SYSTEM SHALL return a `MacosIdentityActionResult` with
   `status=FAILED` and the captured `exit_code` and `stderr`.

---

### Requirement 9: Realm-kind classification

**User Story:** As a maintenance coordinator, I want directory back-
end kinds reported in a normalised enum, so that platform-agnostic
logic does not have to interpret macOS-native shapes.

#### Acceptance Criteria

1. WHEN the host is bound via `dsconfigad` THE SYSTEM SHALL report
   `MacosRealmKind.ACTIVE_DIRECTORY`.
2. WHEN the host is bound via Open Directory only (out of scope for
   Wave 4.5) THE SYSTEM SHALL report
   `MacosRealmKind.OPEN_DIRECTORY` once the feature is extended;
   the current implementation does not enumerate OD binds.
3. WHEN the parsed `dsconfigad -show` output is not parseable as
   either THE SYSTEM SHALL report `MacosRealmKind.UNKNOWN`
   rather than raise.

---

### Requirement 10: Configurable executables

**User Story:** As a test author, I want to inject alternate
executable paths for `dsconfigad`, `dscl`, `dscacheutil`, and
`killall`, so that container / chroot test rigs can swap in stubs.

#### Acceptance Criteria

1. THE `MacosIdentityManagerConfig` dataclass SHALL expose
   `dsconfigad_executable` (default `"dsconfigad"`),
   `dscl_executable` (default `"dscl"`),
   `dscacheutil_executable` (default `"dscacheutil"`), and
   `killall_executable` (default `"killall"`) overrides.
2. EVERY argv built by the manager SHALL use the configured
   executable name as its argv[0].

---

### Requirement 11: OU and force-flag defaults

**User Story:** As a maintenance coordinator that drives many
identical macOS rollouts, I want OU placement and the `-force`
flag controlled at the config layer, so that spec objects stay
clean.

#### Acceptance Criteria

1. THE `MacosIdentityManagerConfig` dataclass SHALL expose
   `default_ou: str | None = None` used when
   `MacosRealmJoinSpec.ou_path` is `None`.
2. THE `MacosIdentityManagerConfig` dataclass SHALL expose
   `force: bool = True` controlling whether `dsconfigad` argv
   includes the `-force` flag on join and leave.
3. WHEN `force=False` THE SYSTEM SHALL NOT append `-force` to
   either argv.

---

### Requirement 12: Query-error surface

**User Story:** As a maintenance coordinator, I want unexpected
read failures to surface as a typed exception rather than silent
empty data, so that broken Directory Services installations do not
look like "unbound" hosts.

#### Acceptance Criteria

1. THE `MacosIdentityQueryError` exception SHALL be raised by
   `get_realm_status` (and, transitively,
   `list_joined_realms`) when `dsconfigad -show` exits non-zero.
2. THE exception SHALL carry the failing argv, exit code, and
   stderr for diagnostics.
3. `resolve_user` and `resolve_group` SHALL NOT raise on `dscl`
   non-zero exit codes; they SHALL return a `found=False` result
   instead.
