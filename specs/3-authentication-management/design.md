# Design

## Overview

The authentication management feature is implemented as a single
module, `ccai.sysman.macos.authenticationmanagement`, exposing a
`MacosAuthenticationManager` class that wraps the per-service files
under `/etc/pam.d/` and a pure-Python PAM stack parser through the
`ICommandRunner` abstraction.

macOS authentication is a hybrid of two surfaces:

1. **PAM stack** at `/etc/pam.d/<service>` — used by ``login``,
   ``sudo``, ``su``, ``sshd``, ``cron``, ``screensaver``, and the
   command-line tooling. This is the surface the pilot manages
   directly.
2. **Authorization database** at `/etc/authorization` (a binary
   plist driven by ``security authorizationdb``) — used by
   GUI-driven privilege requests like installer prompts, System
   Settings panes, and per-application entitlements. The pilot
   exposes `authz_path` on the manager config for forward
   compatibility but does NOT manage this surface.

The Open Directory daemon (`opendirectoryd`) and `dscl` are owned by
`3-identity-management`.

All file reads route through the runner via `cat`; all file writes
route through the runner via `tee` with `stdin`. No direct
filesystem access occurs inside this module.

**See Also:**
- `3-user-group-management` for account creation, password expiry,
  and SSH-key management.
- `3-ssh-server-configuration` for sshd-level configuration. The
  `sshd` PAM stack at `/etc/pam.d/sshd` is the seam between the two
  features.
- `3-identity-management` for OpenDirectory binding and Kerberos.

---

## Cross-OS Authentication Model Concept Mapping

The authentication-management contract abstracts over three very
different OS authentication models. macOS sits in the middle —
PAM-shaped on the command-line side, plist-shaped on the GUI side:

| Concept | macOS (this spec) | Ubuntu | RHEL | Windows |
|---|---|---|---|---|
| Stack composition tool | (manual edit) | `pam-auth-update` | `authselect` | LSA registry |
| Per-service file | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` | (none) |
| GUI authorization | `/etc/authorization` plist (out of scope here) | (n/a) | (n/a) | UAC / secpol |
| Profile registry | (n/a) | `/usr/share/pam-configs/<name>` | `/usr/share/authselect/profiles/<name>` | LSA package list |
| Method enable | strip leading `#` from PAM stack line | `pam-auth-update --enable` | `authselect enable-feature` | add to MULTI_SZ |
| Method disable | prepend `#` to PAM stack line | `pam-auth-update --disable` | `authselect disable-feature` | remove from MULTI_SZ |
| Method enumeration | walk `/etc/pam.d/` | `pam-auth-update --list` | `authselect list-features` | registry MULTI_SZ |
| Validation | re-parse | re-parse | `authselect check` | `secedit /validate` |
| Subsystem identifier | PAM service name | PAM service name | PAM service name | LOGON / NETWORK |
| Stack entry shape | `module_type control module_path [args]` | identical | identical | `policy_key = policy_value` |
| Idempotency check | byte-equal of rendered file | byte-equal | byte-equal | secpol section equality |

**Pilot scope:** the macOS manager focuses on direct PAM stack
management. macOS has no `pam-auth-update`-equivalent tool, so
"enable" / "disable" of a method are implemented as the stack-line
mutation pattern documented above.

**Out-of-scope (forward-compat hooks only):**

- The `/etc/authorization` plist surface. Reading and writing this
  file requires the `security authorizationdb read/write
  <right-name>` tool and round-trip XML editing of plist
  fragments. The pilot exposes `authz_path` on
  `MacosAuthenticationManagerConfig` to keep the door open for a
  follow-up.
- OpenDirectory node manipulation. That surface is owned by
  `3-identity-management`.

---

## Architecture

```
+--------------------------------------------------------------+
|            Maintenance Coordinator                           |
|  - get_auth_config(LOGIN) -> MacosAuthConfig                 |
|  - set_auth_config(LOGIN, declared_config) -> result         |
|  - enable_method("pam_smartcard.so") (un-comment)            |
|  - disable_method("pam_deny.so") (comment out)               |
|  - validate_auth_config(SUDO)                                |
+----------------------------+---------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|            MacosAuthenticationManager                        |
|  ccai.sysman.macos.authenticationmanagement              |
|                                                              |
|  Reads:                                                      |
|    get_auth_config(subsystem)                                |
|    list_auth_methods()                                       |
|    validate_auth_config(subsystem)                           |
|                                                              |
|  Writes:                                                     |
|    set_auth_config(subsystem, config)                        |
|    enable_method(name)                                       |
|    disable_method(name)                                      |
+----------------------------+---------------------------------+
                             |  delegates to
                             v
+--------------------------------------------------------------+
|                   ICommandRunner                             |
|  cat /etc/pam.d/<svc>          (read; not elevated)          |
|  ls -1 /etc/pam.d              (read; not elevated)          |
|  tee /etc/pam.d/<svc>          (write; elevated; stdin=...)  |
+--------------------------------------------------------------+
```

The authorization database surface lives outside the box (forward
compat only):

```
        out of scope:
        +------------------------------+
        |  /etc/authorization (plist)  |
        |  manipulated via             |
        |  `security authorizationdb`  |
        +------------------------------+
```

---

## Components

### `MacosAuthenticationManager`

**Location:** `source/packages/ccai/sysman/macos/authenticationmanagement.py`

| Method | Verb kind | Tool | Elevation |
|---|---|---|---|
| `get_auth_config(subsystem)` | read | `cat /etc/pam.d/<svc>` | no |
| `set_auth_config(subsystem, config)` | write | `tee /etc/pam.d/<svc>` | yes |
| `list_auth_methods()` | read | `ls -1 /etc/pam.d` + `cat` | no |
| `enable_method(name)` | write | `cat` + un-comment + `tee` | yes |
| `disable_method(name)` | write | `cat` + comment + `tee` | yes |
| `validate_auth_config(subsystem)` | read | `cat` + parser | no |

---

## Configuration

```python
@dataclass
class MacosAuthenticationManagerConfig(FeatureConfig):
    pam_dir: str = "/etc/pam.d"
    authz_path: str = "/etc/authorization"  # forward-compat only
    canonical_subsystems: list[str] = (
        "login", "sudo", "su", "sshd", "cron",
    )
```

The `canonical_subsystems` list is the set walked by
`list_auth_methods` / `enable_method` / `disable_method`. Custom
deployments can override.

---

## Data Models

```python
class MacosAuthActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class MacosAuthSubsystem:
    kind: str    # 'LOGIN' | 'SUDO' | 'SU' | 'SSH' | 'CRON' | 'OTHER'
    name: str    # 'login' | 'sudo' | 'su' | 'sshd' | 'cron' | <free>


@dataclass
class MacosAuthEntry:
    module_type: str    # 'auth' | 'account' | 'password' | 'session'
    control: str        # 'required' | 'sufficient' | 'optional' | '[...]'
    module_path: str    # e.g. 'pam_unix.so' or 'pam_smartcard.so'
    args: list[str]


@dataclass
class MacosAuthConfig:
    subsystem: str
    entries: list[MacosAuthEntry]


@dataclass
class MacosAuthMethodInfo:
    name: str
    enabled: bool
    description: str | None = None
    module_path: str | None = None


@dataclass
class MacosAuthValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]


@dataclass
class MacosAuthActionResult(FeatureActionResult):
    subsystem: str | None = None
    method: str | None = None
```

---

## Subsystem Mapping

| `MacosAuthSubsystem.kind` | Service file |
|---|---|
| `LOGIN` | `/etc/pam.d/login` |
| `SUDO` | `/etc/pam.d/sudo` |
| `SU` | `/etc/pam.d/su` |
| `SSH` | `/etc/pam.d/sshd` |
| `CRON` | `/etc/pam.d/cron` |
| `OTHER` | `/etc/pam.d/<name>` |

---

## Key Method Designs

### `get_auth_config(subsystem)`

1. Resolve the basename from the subsystem reference.
2. Run `cat /etc/pam.d/<svc>` with `elevated=False`.
3. Map a "No such file" stderr to
   `MacosAuthenticationSubsystemNotFoundError`.
4. Map any other non-zero exit to `MacosAuthenticationQueryError`.
5. Parse with `_parse_pam_stack`.
6. Return `MacosAuthConfig(subsystem=<basename>,
   entries=<parsed>)`.

### `set_auth_config(subsystem, config)`

1. Read current file via `_read_file`.
2. Render desired via `_render_pam_stack(config.entries)`.
3. If equal -> SKIPPED.
4. If `dry_run` -> DRY_RUN.
5. Else `tee` with `elevated=True`, `stdin=<rendered>`.

### `list_auth_methods()`

1. Run `ls -1 /etc/pam.d` to enumerate service files (filter to the
   `canonical_subsystems` list when configured, otherwise list all).
2. For each subsystem, `cat` the file.
3. Walk every line. For each `module_path` token, accumulate the
   subsystems where it appears and whether any reference is
   un-commented.
4. Build one `MacosAuthMethodInfo` per unique module name with
   `enabled=True` if any reference is un-commented.

### `enable_method(name)` / `disable_method(name)`

Symmetric. The probing step uses `list_auth_methods()`.

`enable_method`:
1. Probe; SKIPPED if already enabled.
2. For each subsystem that references the module on a comment-only
   line, strip the leading `#` from those lines and re-write the
   file via `tee`.
3. If the module is not present anywhere, raise
   `MacosAuthenticationSubsystemNotFoundError`.

`disable_method`:
1. Probe; SKIPPED if already disabled or absent.
2. For each subsystem that references the module on an active line,
   prepend a `#` to those lines and re-write the file via `tee`.

### `validate_auth_config(subsystem)`

1. Read.
2. Re-parse.
3. Walk entries checking module_type / control / module_path.
4. Return `MacosAuthValidationResult`.

---

## PAM Stack Parser / Renderer

Identical surface to the Ubuntu pilot's PAM parser (this is a shared
syntax). The macOS pilot keeps a private copy in this module rather
than importing across packages — the syntax is small enough that
duplication is preferable to a cross-package dependency, and the two
managers may diverge over time as macOS-specific tokens emerge
(notably `pam_smartcard.so` and `pam_self.so`, which are macOS-only).

Parser rules:

- Lines that start with `#` (after leading whitespace) are
  comments — skipped for the structured representation but
  observed by `list_auth_methods` for the comment-state check.
- Blank lines are skipped.
- `[bracketed]` controls are collected as one token.
- The first token is `module_type`, the second is `control`, the
  third is `module_path`, and the remainder are `args`.
- `@include <basename>` directives produce an `@include` entry with
  the include target captured as `module_path` and `control=""`.

Renderer rules: emit each entry as
`<module_type> <control> <module_path> <args>` with single-space
separators; `@include` entries are emitted as `@include <module>`;
file ends with a single trailing newline.

---

## Authorization Database (Out of Scope)

The authorization database at `/etc/authorization` is consulted by
`Authorization Services` for GUI-driven privilege requests. It is a
plist (XML or binary) with named "rights" and "rules" that the
system uses to decide whether a given operation requires a password,
admin authentication, or no authentication. The canonical access
tools are:

```
security authorizationdb read <right>
security authorizationdb write <right> <plist-fragment>
security authorizationdb remove <right>
```

A complete authz-management feature would:
- Enumerate every right and rule (`security authorizationdb read
  *`).
- Round-trip plist fragments via the system's `plutil -convert
  xml1` / `plutil -convert binary1` tooling.
- Enforce idempotency by parsing the existing right's plist into a
  structured Python dict, comparing, and writing only on change.

The pilot does NOT implement any of this. The
`MacosAuthenticationManagerConfig.authz_path` field is reserved for
the follow-up; no verb in this pilot reads from or writes to
`/etc/authorization`.

---

## Dry-Run Behaviour

Every mutating entry point follows the same shape:

- Read the live state (always — dry-run does not block reads).
- Compute desired state and compare for SKIPPED.
- If a write would be required AND `config.dry_run=True` -> return
  `DRY_RUN` with `command` populated.

---

## Correctness Properties

1. **Idempotency.** Each write returns `SKIPPED` when the live state
   already matches the requested state, verified by re-reading
   through the runner.
2. **No silent failure.** Every non-zero runner exit surfaces as
   `status=FAILED` with `exit_code` and `stderr` populated.
3. **Dry-run safety.** No mutating command is dispatched when
   `dry_run=True`; read-only reconciliation queries always run.
4. **Subsystem typing.** Unknown subsystem strings surface as
   `MacosAuthenticationSubsystemNotFoundError` rather than silent
   no-ops.
5. **Stack ordering.** The PAM stack render preserves entry order
   verbatim — order is semantically significant in PAM.
6. **Comment state preserved on disable.** Disabling a method
   prepends a `#`; the original line content is preserved so the
   un-comment path can restore it.

---

## Testing Strategy

A local `_MockCommandRunner` dataclass mirrors the pattern used in
`test_sshserverconfiguration.py`. Tests seed `RunResult` values
keyed by argv tuple and assert:

- argv shape per write verb,
- elevation flag per verb,
- `SKIPPED` when current == desired,
- `DRY_RUN` short-circuits the runner,
- stderr surfaces in `FAILED` results,
- the PAM parser handles `@include`,
- the authz database is documented as out of scope (presence of the
  `authz_path` config field with no read / write call).

No live system or root access required.

---

## Module Layout

```
source/packages/ccai/sysman/macos/
└── authenticationmanagement.py        ← MacosAuthenticationManager + models

source/testroots/testbase/
└── ccai/tests/sysman/macos/
    └── test_authenticationmanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This feature |
|---|---|---|
| sshd PAM stack | `3-ssh-server-configuration` | This feature owns `/etc/pam.d/sshd`; the SSH feature owns `/etc/ssh/sshd_config`. |
| Per-user account state | `3-user-group-management` | This feature handles the policy file; user-group handles individual accounts. |
| OpenDirectory / Kerberos | `3-identity-management` | `3-identity-management` may invoke `enable_method("pam_opendirectory.so")` after directory binding. |
| Compliance snapshot | `3-security-compliance` | This feature exposes read methods; compliance reads and compares against a baseline. |
| Authorization database | (out of scope, future feature) | Forward-compat hook via `authz_path` only. |

---

## macOS-Specific PAM Modules

macOS ships PAM modules that have no exact Linux equivalent. The
manager handles them as opaque module names — the parser treats
them like any other PAM stack reference — but operators benefit
from knowing which modules are macOS-specific:

| Module | Purpose |
|---|---|
| `pam_smartcard.so` | Smart-card authentication. Often listed as `sufficient` in `/etc/pam.d/sudo` and `/etc/pam.d/login` to allow smart-card unlock without password. |
| `pam_opendirectory.so` | OpenDirectory-backed authentication. Replaces `pam_unix.so` on macOS for directory-aware credential lookup. |
| `pam_self.so` | Allows password-less self-authentication (used by `/etc/pam.d/su` to permit `su <self>` without prompting). |
| `pam_securityserver.so` | Bridges PAM to macOS's `Authorization Services` for GUI-driven credentialing. |
| `pam_sacl.so` | Service ACL enforcement — checks that the requesting user is in the ACL for the requested service (used by `/etc/pam.d/screensaver`). |
| `pam_group.so` | macOS port of the Linux module of the same name; checks group membership before allowing access. |
| `pam_nologin.so` | Equivalent to the Linux module; refuses logins when `/etc/nologin` exists. |
| `pam_deny.so` | Standard "always-deny" terminator. |
| `pam_permit.so` | Standard "always-permit" terminator. |
| `pam_unix.so` | Legacy local-password authentication. Most macOS releases route through `pam_opendirectory.so` instead. |
| `pam_motd.so` | Standard "message of the day" session module. |

The manager's `enable_method` / `disable_method` verbs accept any
of these module names verbatim. The parser's `_VALID_MODULE_TYPES`
list covers the standard PAM module-type tokens (`auth`, `account`,
`password`, `session`, plus the leading-dash variants used by
`-session pam_motd.so` lines).

---

## Cross-Subsystem Behaviour of `enable_method` / `disable_method`

Unlike Ubuntu's `pam-auth-update --enable`, which mutates a
profile registry that re-composes every subsystem, the macOS
manager mutates each subsystem in place. The implementation
walks the configured `canonical_subsystems` list and:

1. **`enable_method(name)`:** strips the leading `#` from any
   commented occurrences. If the module is not present anywhere,
   raises `MacosAuthenticationSubsystemNotFoundError`. The pilot
   does NOT add a new line to a stack that doesn't reference the
   module — adding a new module is an operation that requires
   knowing where in the stack to place it (PAM order matters), so
   that surface is left to `set_auth_config` instead.
2. **`disable_method(name)`:** prepends a `#` to any active
   occurrences. If the module is absent or every reference is
   already comment-prefixed, returns `SKIPPED`.

Per-file mutation is staged through `tee` invocations — one per
modified subsystem. If any mutation fails the manager returns
`FAILED` from the failed mutation; earlier successful writes are
NOT rolled back. The follow-up rollback path is tracked in
`tasks.md`.

---

## Result Field Population

For every verb the result's `subsystem` and `method` fields are
populated as follows:

| Verb | `subsystem` | `method` |
|---|---|---|
| `set_auth_config` | resolved basename | `None` |
| `enable_method` | `None` | supplied module name |
| `disable_method` | `None` | supplied module name |

The `command` field always carries the argv that was (or would
have been) executed. For `enable_method` / `disable_method`, the
command on `SKIPPED` and `DRY_RUN` results is the synthetic
`["pam-edit", "--enable", <name>]` /
`["pam-edit", "--disable", <name>]` placeholder so callers see a
recognisable signal. The actual `tee` invocations are recorded on
the `command` field of the SUCCESS / FAILED result; if multiple
subsystems were mutated, only the last `tee` argv is recorded
(the prior writes succeeded by definition or the verb would have
returned FAILED earlier).

---

## Drift From the Cross-OS Contract

- **`apply_changes` verb.** The contract defines an `apply_changes`
  verb for OSes with a staging / commit model. macOS PAM edits are
  immediate — the next `getpwnam_r` / authentication call composes
  the stack from the current files. The macOS manager omits
  `apply_changes`. Coordinators that target multiple OSes should
  treat `apply_changes` as a no-op call on macOS.
- **`get_method_status` verb.** macOS has no per-method probe API;
  the manager exposes the data via `list_auth_methods()` only.
- **`profile`, `features`, and `directives` fields on
  `MacosAuthConfig`.** macOS PAM does not have profile templates
  or directive maps. The pilot collapses everything into the
  `entries` list. Cross-OS callers should construct `entries` from
  their structured directive map.
- **Authorization database surface.** The contract names
  `/etc/authorization` as a recognised macOS authentication
  surface. The pilot does NOT manage it; only the path is
  exposed on the manager config for forward compatibility.
- **`enable_method` / `disable_method` semantics.** Ubuntu and
  RHEL implement these against a profile registry; macOS has no
  registry. The macOS implementation rewrites the PAM stack
  in-place by toggling the comment prefix on lines that
  reference the supplied module name. This is a softer surface
  than the `pam-auth-update --enable` flow on Ubuntu and is
  documented as such.
