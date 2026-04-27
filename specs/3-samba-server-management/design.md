# Design

## Overview

The macOS samba-server-management feature is implemented as a single
module, ``automojo.sysman.macos.sambaservermanagement``, exposing a
``MacosSambaServerManager`` class that wraps macOS's SMB-share
surface (the ``sharing`` CLI, ``launchctl kickstart`` of
``com.apple.smbd``, and the ``dscl . /SharePoints/...`` namespace)
through the ``ICommandRunner`` abstraction.

Follows the standard manager pattern; delegates daemon lifecycle to
``3-service-management`` and local-user management to
``3-user-group-management``.

**See Also:**
- ``automojo-sysman-common/specs/contracts/samba-server-management/contract.md``
  — the cross-OS abstract contract.
- ``automojo-sysman-linux-ubuntu/specs/3-samba-server-management/``
  — Ubuntu's implementation, which uses the Samba project's
  ``smb.conf`` / ``smbpasswd`` / ``pdbedit`` toolchain.
- ``3-service-management`` — ``com.apple.smbd`` LaunchDaemon
  lifecycle.
- ``3-firewall-management`` — opening SMB ports through pf /
  socketfilterfw.
- ``3-user-group-management`` — local POSIX accounts.  macOS SMB
  authentication routes through this directory.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosSambaServerManager                          │
│                                                             │
│  Writes:                                                    │
│    add_share(), remove_share(),                             │
│    set_share_permissions(), reload_config()                 │
│  Delegated writes:                                          │
│    add_samba_user(), remove_samba_user()                    │
│  Reads:                                                     │
│    list_shares(), get_share(), list_samba_users()           │
│                                                             │
│  Helpers:                                                   │
│    _invoke_sharing_list()                                   │
│    _invoke_launchctl_kickstart()                            │
│    _build_dscl_change_command()                             │
└────────────────┬──────────────────────────┬─────────────────┘
                 │                          │
                 ▼                          ▼
      ┌─────────────────────┐   ┌─────────────────────────┐
      │   ICommandRunner    │   │  /SharePoints (DSLocal) │
      └─────────────────────┘   └─────────────────────────┘
```

Every command — including reads — flows through the injected
``ICommandRunner``.

---

## Tool Map

| Verb | macOS Tool |
|------|------------|
| `add_share` | ``sharing -a <path> -S <name> -s 100 -g <guest>`` |
| `remove_share` | ``sharing -r <name>`` |
| `list_shares` | ``sharing -l`` (parsed) |
| `get_share` | ``sharing -l`` filtered by name |
| `set_share_permissions` | ``dscl . -change /SharePoints/<name> ...`` |
| `reload_config` | ``launchctl kickstart -k system/com.apple.smbd`` |
| `add_samba_user` | DELEGATED → ``user-group-management``; FAILED result |
| `remove_samba_user` | DELEGATED → ``user-group-management``; FAILED result |
| `list_samba_users` | ``pdbedit -L -v`` (best-effort; empty list when ``pdbedit`` is absent) |

---

## Data Models

```python
MacosSambaActionStatus = ActionStatus  # alias from featurebase


@dataclass
class MacosSambaManagerConfig(FeatureConfig):
    service_label: str = "com.apple.smbd"
    delegate_user_mgmt: bool = True


@dataclass
class MacosSambaActionResult(FeatureActionResult):
    share_name: str | None = None
    username: str | None = None
    message: str | None = None


@dataclass
class MacosSambaShareSpec:
    name: str
    path: str
    description: str | None = None
    read_only: bool = False
    valid_users: list[str] | None = None
    read_list: list[str] | None = None
    write_list: list[str] | None = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class MacosSambaShareInfo:
    name: str
    path: str
    description: str | None
    read_only: bool
    valid_users: list[str] | None
    read_list: list[str] | None
    write_list: list[str] | None
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: int | None


@dataclass
class MacosSambaShareAcl:
    read_users: list[str]
    write_users: list[str]
    full_access_users: list[str]
    denied_users: list[str]


@dataclass
class MacosSambaUserInfo:
    username: str
    enabled: bool
    last_password_change: str | None
    description: str | None
```

---

## Sharing-tool Output Parsing

The ``sharing -l`` output is multi-line and per-share, e.g.::

    name:           public
    path:           /Users/Shared/public
    afp:            {... afp_disabled etc ...}
    smb:            {... smb_inherit_perms = 0 etc ...}

The parser splits the output into per-share blocks (a block starts
at ``name:`` and ends at the next ``name:`` or EOF), then extracts
the ``path``, the SMB sub-block's ``smb_shared`` flag (``0`` =
share is NOT exposed via SMB and is filtered out), the
``smb_guestaccess`` flag, the ``smb_inherit_perms`` flag, and the
canonical ``directory_mask`` / ``create_mask`` if reported.

---

## User Management Delegation

The pilot does NOT manage SMB user creation / removal directly.
``add_samba_user`` and ``remove_samba_user`` always return
``status=FAILED`` with::

    detail = "add_samba_user delegated to user-group-management"
    message = (
        "macOS SMB authentication uses the local user directory; "
        "create the underlying account through "
        "3-user-group-management before publishing the share."
    )

The recorded ``command`` field is a sanitised placeholder argv
identifying the would-be tool (``["dscl", ".", "-create",
"/Users/<name>"]``) but NEVER the password.

When ``delegate_user_mgmt=False`` the manager would in principle
dispatch ``sysadminctl -addUser <name> -password <password>`` (with
the password on stdin via the runner), but this path is gated off
in the pilot and not exercised by tests.

---

## Dry-Run Behaviour

Dry-run disables every mutating path.  Read-only queries
(``sharing -l``, ``pdbedit -L -v``) run freely even under dry-run
because they drive idempotency decisions.

---

## Correctness Properties

1. **No passphrase on argv.** ``add_samba_user`` is delegated and
   does NOT route the password anywhere; the recorded ``command``
   field is a sanitised placeholder.
2. **Idempotent share definitions.** ``add_share`` returns
   ``SKIPPED`` when ``sharing -l`` reports a share with matching
   ``name`` / ``path`` already.
3. **Reload-after-change is OS-NATIVE on macOS.** The ``sharing``
   tool commits changes to DSLocal synchronously; an explicit
   ``launchctl kickstart`` is only required when the coordinator
   wants to bounce the daemon.

---

## Testing Strategy

- ``_MockCommandRunner`` with pre-seeded ``RunResult`` values keyed
  by argv tuple.
- Parser tests for ``sharing -l`` output.
- Tests that ``add_samba_user`` / ``remove_samba_user`` always
  return ``FAILED`` with the delegation explanation, and that the
  recorded ``command`` field NEVER contains the password.
- Dry-run, elevation, and failure-mapping sweeps.
