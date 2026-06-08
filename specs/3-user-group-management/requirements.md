# Requirements

## Introduction

The user and group management feature provides the `codecraftai-sysman-macos` package with
the ability to manage local users and local groups on macOS systems.  This spec
implements the user-group-management contract defined at
`codecraftai-sysman-common/specs/contracts/user-group-management/contract.md` for macOS
using the native Directory Services tooling (`sysadminctl`, `dscl`, `dseditgroup`,
`pwpolicy`) and the POSIX identity utility `id`.  It includes creating, deleting, and
modifying users and groups, managing group membership, setting passwords, and locking
and unlocking accounts in the local Directory Services node.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
users or groups should exist — it provides the capability to *manipulate them* reliably,
idempotently, and with full auditability.  All system commands are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction, enabling dry-run
simulation, auditing, and test mocking without requiring a live system.  Passwords
passed to `set_password` are forwarded to `sysadminctl` via argv only when required by
the tool itself; the resulting `MacosUserGroupActionResult` argv is sanitised so the
password never appears in the persisted `command` field.

---

## Requirements

### Requirement 1: Create a Local User

**User Story:** As a maintenance coordinator, I want to create a local user on a macOS
system from a supplied `MacosUserSpec`, so that the account becomes available for
interactive or service use with the UID, shell, home directory, and full-name
description the spec requires.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosUserSpec` THE SYSTEM SHALL construct a
   `sysadminctl -addUser <name>` invocation augmented with `-fullName "<comment>"`,
   `-UID <uid>`, `-shell <shell>`, `-home <home>`, and `-password <password>` for every
   spec field that is not `None`, append the admin credential pair `-adminUser <admin>
   -adminPassword <pw>` when the configuration provides one, and dispatch the argv
   through the command runner with `elevated=True` when configured.
2. WHEN `MacosUserSpec.system_account` is `True` THE SYSTEM SHALL NOT add any special
   flag (macOS does not distinguish system accounts at creation time) and SHALL record
   a detail on the result noting that macOS system accounts are identified purely by
   their UID being below `501`.
3. WHEN a user with the same name already exists (detected via
   `dscl . -read /Users/<name>`) THE SYSTEM SHALL skip execution and return a
   `MacosUserGroupActionResult` with `status=SKIPPED` and `kind="user"`.
4. WHEN the `sysadminctl` command succeeds THE SYSTEM SHALL return a
   `MacosUserGroupActionResult` with `status=SUCCESS`, `kind="user"`, and the captured
   `exit_code`, `stdout`, and `stderr`.
5. WHEN the `sysadminctl` command fails THE SYSTEM SHALL return a
   `MacosUserGroupActionResult` with `status=FAILED` and the captured exit code and
   stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN` and a sanitised argv (with the
   password redacted to `"<redacted>"`) in `command`.

---

### Requirement 2: Delete a Local User

**User Story:** As a maintenance coordinator, I want to delete a local user, optionally
erasing their home directory, so that dismissed accounts leave no orphaned resources.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_user(name, remove_home=True)` THE SYSTEM SHALL
   dispatch `sysadminctl -deleteUser <name> -secure` through the command runner with
   `elevated=True` when configured.
2. WHEN the coordinator calls `delete_user(name, remove_home=False)` THE SYSTEM SHALL
   dispatch `sysadminctl -deleteUser <name> -keepHome` through the command runner.
3. WHEN the target user does not exist (detected via `dscl . -read /Users/<name>`) THE
   SYSTEM SHALL skip execution and return a `MacosUserGroupActionResult` with
   `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED` and the exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Modify a Local User

**User Story:** As a maintenance coordinator, I want to modify a subset of an existing
user's properties, so that shell, home, primary group, comment, or UID can be updated
without recreating the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `modify_user(name, changes)` THE SYSTEM SHALL issue one
   `dscl . -change /Users/<name> <property> <old> <new>` invocation per non-`None`
   field of `MacosUserModSpec`, mapping `shell` → `UserShell`, `home` →
   `NFSHomeDirectory`, `primary_group` → `PrimaryGroupID` (resolved via
   `dseditgroup -o read <group>` first), `comment` → `RealName`, and `uid` →
   `UniqueID`.  Each invocation is dispatched with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError` before any command is dispatched.
3. WHEN all non-`None` fields already match the current `MacosUserInfo` THE SYSTEM
   SHALL skip execution and return a `MacosUserGroupActionResult` with
   `status=SKIPPED`.
4. WHEN every `dscl` invocation succeeds THE SYSTEM SHALL return a
   `MacosUserGroupActionResult` with `status=SUCCESS`.
5. WHEN any single `dscl` invocation fails THE SYSTEM SHALL stop dispatching further
   fields and return a `MacosUserGroupActionResult` with `status=FAILED`, the failing
   step's exit code and stderr, and a `detail` identifying which property failed.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN` containing the argv of the
   first property that would be changed.
7. WHEN `move_home=True` THE SYSTEM SHALL additionally dispatch a `ditto <old_home>
   <new_home>` invocation followed by `rm -rf <old_home>` through the runner; any
   failure of those copy / cleanup steps SHALL be reported as `FAILED`.

---

### Requirement 4: Query a Single User

**User Story:** As a maintenance coordinator, I want to query a single local user's
record, so that downstream logic can branch on existence and properties without having
to parse directory output itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user(name)` THE SYSTEM SHALL dispatch
   `dscl . -read /Users/<name>` through the command runner without elevation.
2. WHEN `dscl` exits `0` THE SYSTEM SHALL parse the key/value block into a
   `MacosUserInfo` with `name`, `uid`, `gid`, `comment`, `home`, and `shell`; then
   dispatch `pwpolicy -u <name> -getpolicy` and parse the `isDisabled` flag into the
   `locked` field.
3. WHEN `dscl` exits non-zero with stderr matching `eDSRecordNotFound` or `DS error`
   THE SYSTEM SHALL return `None`.
4. WHEN `dscl` exits with any other non-zero code THE SYSTEM SHALL raise a
   `MacosUserGroupQueryError` with the exit code and stderr.

---

### Requirement 5: List All Local Users

**User Story:** As a maintenance coordinator, I want to list every local user in the
standard local UID range, so that reporting and audit workflows enumerate the set of
maintenance targets.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_users()` THE SYSTEM SHALL dispatch
   `dscl . -list /Users UniqueID` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line as `<name> <uid>` and
   filter to entries whose UID is `>= 500`, which is the conventional macOS
   local-account boundary; the boundary is overridable via configuration.
3. WHEN the coordinator wants full per-user information THE SYSTEM SHALL loop the
   filtered list through `get_user(name)` and return the resulting list of
   `MacosUserInfo` records.
4. WHEN the listing command fails THE SYSTEM SHALL raise a `MacosUserGroupQueryError`.

---

### Requirement 6: Set a User's Password

**User Story:** As a maintenance coordinator, I want to set a user's password without
leaking it into audit logs, so that credentials remain confidential.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_password(name, password)` THE SYSTEM SHALL dispatch
   `sysadminctl -resetPasswordFor <name> -newPassword <password> -adminUser <admin>
   -adminPassword <adminpw>` through the command runner with `elevated=True` when
   configured.
2. WHEN the `MacosUserGroupActionResult` is constructed THE SYSTEM SHALL store a
   sanitised copy of the argv in `command` with both the new password and the admin
   password replaced by the literal string `"<redacted>"`; the plaintext passwords
   SHALL NOT appear in `command`, `stdout`, or `stderr`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError` before any command is dispatched.
4. WHEN `sysadminctl` fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN` and the sanitised argv.

---

### Requirement 7: Lock a User Account

**User Story:** As a maintenance coordinator, I want to lock a user account so that the
user cannot log in without deleting the account.

#### Acceptance Criteria

1. WHEN the coordinator calls `lock_account(name)` THE SYSTEM SHALL dispatch
   `pwpolicy -u <name> -disableuser` through the command runner with `elevated=True`
   when configured.
2. WHEN the current `MacosUserInfo.locked` is already `True` THE SYSTEM SHALL skip
   execution and return a `MacosUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED` and the captured exit code and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Unlock a User Account

**User Story:** As a maintenance coordinator, I want to unlock a previously locked
account so that the user can resume interactive use.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_account(name)` THE SYSTEM SHALL dispatch
   `pwpolicy -u <name> -enableuser` through the command runner with `elevated=True`
   when configured.
2. WHEN the current `MacosUserInfo.locked` is already `False` THE SYSTEM SHALL skip
   execution and return a `MacosUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Create a Local Group

**User Story:** As a maintenance coordinator, I want to create a local group from a
supplied `MacosGroupSpec`, so that downstream user memberships can target it.

#### Acceptance Criteria

1. WHEN the coordinator provides a `MacosGroupSpec` THE SYSTEM SHALL construct a
   `dseditgroup -o create -n /Local/Default` invocation augmented with `-i <gid>` when
   the GID is set, followed by the group name as the final positional argument, and
   dispatch the argv with `elevated=True` when configured.
2. WHEN a group with the same name already exists (detected via
   `dseditgroup -o read -n /Local/Default <name>`) THE SYSTEM SHALL skip execution and
   return a `MacosUserGroupActionResult` with `status=SKIPPED` and `kind="group"`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Delete a Local Group

**User Story:** As a maintenance coordinator, I want to delete a local group so that
stale group entries do not accumulate after their last referring user is removed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` THE SYSTEM SHALL dispatch
   `dseditgroup -o delete -n /Local/Default <name>` through the command runner with
   `elevated=True` when configured.
2. WHEN the target group does not exist THE SYSTEM SHALL skip execution and return a
   `MacosUserGroupActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Query a Single Group

**User Story:** As a maintenance coordinator, I want to query a single group's record,
so that membership checks can branch on current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group(name)` THE SYSTEM SHALL dispatch
   `dseditgroup -o read -n /Local/Default <name>` through the command runner without
   elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the property-list-style output into
   a `MacosGroupInfo` with `name`, `gid` (from `PrimaryGroupID`), and `members` (from
   the `GroupMembership` and/or `GroupMembers` fields, with UUID members resolved to
   usernames via an additional `dscl . -search /Users GeneratedUID <uuid>` call when
   needed).
3. WHEN the group does not exist (`dseditgroup` exits non-zero with "Record was not
   found") THE SYSTEM SHALL return `None`.
4. WHEN `dseditgroup` exits with any other non-zero code THE SYSTEM SHALL raise a
   `MacosUserGroupQueryError`.

---

### Requirement 12: List All Local Groups

**User Story:** As a maintenance coordinator, I want to list every local group, so that
reporting workflows can enumerate groups for cross-reference.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL dispatch
   `dscl . -list /Groups PrimaryGroupID` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line as `<name> <gid>` and
   loop the list through `get_group(name)` to materialise full `MacosGroupInfo` records.
3. WHEN the listing command fails THE SYSTEM SHALL raise a
   `MacosUserGroupQueryError`.

---

### Requirement 13: Add a User to a Group

**User Story:** As a maintenance coordinator, I want to add a user to a supplementary
group, so that the user inherits the permissions associated with the group.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_group(user, group)` THE SYSTEM SHALL dispatch
   `dseditgroup -o edit -a <user> -t user -n /Local/Default <group>` through the
   command runner with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `MacosGroupNotFoundError` before any command is dispatched.
4. WHEN the user is already a member of the group THE SYSTEM SHALL skip execution and
   return a `MacosUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 14: Remove a User from a Group

**User Story:** As a maintenance coordinator, I want to remove a user from a
supplementary group, so that the user loses the permissions associated with the group.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_group(user, group)` THE SYSTEM SHALL
   dispatch `dseditgroup -o edit -d <user> -t user -n /Local/Default <group>` through
   the command runner with `elevated=True` when configured.
2. WHEN the target user does not exist THE SYSTEM SHALL raise
   `MacosUserNotFoundError` before any command is dispatched.
3. WHEN the target group does not exist THE SYSTEM SHALL raise
   `MacosGroupNotFoundError` before any command is dispatched.
4. WHEN the user is not a member of the group THE SYSTEM SHALL skip execution and
   return a `MacosUserGroupActionResult` with `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `MacosUserGroupActionResult`
   with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `MacosUserGroupActionResult` with
   `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `MacosUserGroupActionResult` with `status=DRY_RUN`.

---

### Requirement 15: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all user and group write commands
to run with root privilege via `sudo`, so that write operations succeed on a standard
macOS system where the process is not running as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for every write (create_user, delete_user,
   modify_user, set_password, lock_account, unlock_account, create_group, delete_group,
   add_to_group, remove_from_group).
2. WHEN performing read-only operations (get_user, list_users, get_group, list_groups)
   THE SYSTEM SHALL NOT pass `elevated=True`, regardless of configuration.
3. WHEN the process is already running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner to surface the resulting permission error
   as a `MacosUserGroupActionResult` with `status=FAILED` for writes or as a
   `MacosUserGroupQueryError` for reads.
