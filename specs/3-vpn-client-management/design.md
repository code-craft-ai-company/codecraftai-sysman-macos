# Design

## Overview

The vpn-client-management feature is implemented as a single module,
`automojo.sysman.macos.vpnclientmanagement`, that exposes a
`MacosVPNClientManager` class subclassing `VPNClientManagementBase` from
`automojo-sysman-common`. The manager installs and lifecycles **Tailscale**
via either the Homebrew formula or the signed `.pkg`, and the **Twingate
Connector** via the signed `.pkg`. Service lifecycle is controlled with
`launchctl bootstrap` / `bootout` against the `/Library/LaunchDaemons/`
plist files.

All commands route through `ICommandRunner` — `brew`, `curl`, `installer`,
`launchctl`, the `tailscale` and `twingate` CLIs, and `tee` for the
Twingate config file. No direct subprocess use, no direct filesystem writes.

**See Also:**
- [vpn-client-management contract](../../../automojo-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../automojo-sysman-common/specs/vpn-client-management-base/design.md)
- [1-command-execution](../1-command-execution/design.md)
- [2-maintenance-reporting](../2-maintenance-reporting/design.md)
- [3-package-management](../3-package-management/design.md) — `brew`
- [3-service-management](../3-service-management/design.md) — `launchctl`
- [Ubuntu sibling impl](../../../automojo-sysman-linux-ubuntu/specs/3-vpn-client-management/design.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects                                       │
│  - Injects runner + MacosVPNClientManagerConfig             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            MacosVPNClientManager                            │
│  automojo.sysman.macos.vpnclientmanagement                  │
│  inherits VPNClientManagementBase                           │
│                                                             │
│  install_tailscale()  install_twingate_connector()          │
│  configure()          bring_up()                            │
│  bring_down()         status()                              │
│  uninstall()                                                │
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
┌─────────────────┐ ┌────────────┐ ┌──────────────────┐
│ brew / curl +   │ │ launchctl  │ │ tailscale /      │
│ installer       │ │ bootstrap/ │ │ twingate CLIs    │
│                 │ │ bootout    │ │                  │
└────────┬────────┘ └─────┬──────┘ └────────┬─────────┘
         │                │                 │
         └────────────────┼─────────────────┘
                          ▼
                   ICommandRunner
```

---

## Components

### `MacosVPNClientManager`

**Location:** `source/packages/automojo/sysman/macos/vpnclientmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_tailscale` | `(*, version, channel, aspects)` | `brew install tailscale` OR `curl` + `installer` (PKG path) |
| `install_twingate_connector` | `(*, version, aspects)` | `curl` + `installer` for signed Twingate `.pkg` |
| `configure` | `(*, provider, settings, aspects)` | Write Twingate `connector.conf`; SKIPPED for Tailscale |
| `bring_up` | `(*, provider, authkey, hostname, …, aspects)` | `launchctl bootstrap` + `tailscale up` / `launchctl bootstrap` for Twingate |
| `bring_down` | `(*, provider, aspects)` | `tailscale down` / `launchctl bootout` for Twingate |
| `status` | `(*, provider, aspects)` | `tailscale status --json` / `twingate status` |
| `uninstall` | `(*, provider, purge, aspects)` | `brew uninstall` OR bundled uninstaller; state cleanup |

### `MacosVPNClientManagerConfig`

Extends `VPNClientManagerConfig`:

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `install_source` | `InstallSource` | `HOMEBREW` | `HOMEBREW` or `PKG` for Tailscale |
| `tailscale_plist_path` | `str` | `"/Library/LaunchDaemons/com.tailscale.tailscaled.plist"` | launchd plist |
| `twingate_plist_path` | `str` | `"/Library/LaunchDaemons/com.twingate.connector.plist"` | launchd plist |
| `twingate_config_path` | `str` | `"/Library/Application Support/Twingate/connector.conf"` | Twingate config |
| `pkg_cache_dir` | `str` | `"/var/tmp/automojo/vpn-pkgs"` | Where downloaded `.pkg` files are staged |

### `InstallSource` (enum)

| Value | Meaning |
|-------|---------|
| `HOMEBREW` | Install via `brew install tailscale` |
| `PKG` | Install via signed `.pkg` + `installer` |

---

## Data Models

Re-export shared contract types under Macos-prefixed aliases:

```python
MacosVPNActionStatus     = VPNActionStatus
MacosVPNActionResult     = VPNActionResult
MacosVPNClientStatus     = VPNClientStatus
MacosVPNClientQueryError = VPNClientQueryError
```

---

## Provider-Backend Mapping

| Provider | Install command | Daemon plist | Bring-up command | Bring-down command | Status command |
|---|---|---|---|---|---|
| Tailscale (Homebrew) | `brew install tailscale[@<version>]` | `com.tailscale.tailscaled.plist` | `launchctl bootstrap system <plist>` + `tailscale up …` | `tailscale down` | `tailscale status --json` |
| Tailscale (PKG) | `curl -fsSL -o <pkg> <url>` + `installer -pkg <pkg> -target /` | same | same | same | same |
| Twingate | `curl -fsSL -o <pkg> <url>` + `installer -pkg <pkg> -target /` | `com.twingate.connector.plist` | `launchctl bootstrap system <plist>` | `launchctl bootout system <plist>` | `twingate status` |

---

## Command Mappings

### Installation

| Step | Command |
|---|---|
| Homebrew install | `brew install tailscale[@<version>]` (no elevation) |
| PKG download | `curl -fsSL -o <pkg_cache_dir>/<name>.pkg <url>` (no elevation; runner-side path validated) |
| PKG install | `installer -pkg <pkg_cache_dir>/<name>.pkg -target /` (elevated) |

### Configuration

| Provider | Path | Mechanism |
|---|---|---|
| Tailscale | (none) | Returned `SKIPPED` |
| Twingate | `<twingate_config_path>` | Render via runner heredoc piped through `tee` |

### Service Lifecycle

| Action | Command |
|---|---|
| Load Tailscale | `launchctl bootstrap system <tailscale_plist_path>` |
| Unload Tailscale | `launchctl bootout system <tailscale_plist_path>` |
| Load Twingate | `launchctl bootstrap system <twingate_plist_path>` |
| Unload Twingate | `launchctl bootout system <twingate_plist_path>` |
| Check loaded | `launchctl print system/<label>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Tailscale status | `tailscale status --json` |
| Twingate status | `twingate status` |
| Installed check (brew) | `brew list --versions tailscale` |
| Installed check (pkg) | `pkgutil --pkg-info com.tailscale.tailscaled` |

---

## Idempotency Guards

| Verb | Guard | Mechanism |
|---|---|---|
| `install_tailscale` | Package present at requested version | `brew list --versions tailscale` or `pkgutil --pkg-info` |
| `install_twingate_connector` | Same | `pkgutil --pkg-info com.twingate.connector` |
| `configure` (Twingate) | Rendered content matches on-disk | `cat <path>` via runner |
| `bring_up` | `status()` already shows `running=True` and matching hostname/tags | Pre-call status query |
| `bring_down` | `status()` already shows `running=False` | Pre-call status query |
| `uninstall` | Package already absent | `pkgutil --pkg-info` non-zero |

---

## Elevation Policy

- `installer`, `launchctl bootstrap`/`bootout`, `tailscale up`, and state-
  directory `rm` operations pass `elevated=True` when `config.elevated=True`.
- `brew` operations never elevate (Homebrew refuses to run as root).
- Reads (`tailscale status`, `pkgutil`, `brew list`) never elevate.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── vpnclientmanagement.py     ← MacosVPNClientManager, config, aliases

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_vpnclientmanagement.py
```

---

## See Also

- [vpn-client-management contract](../../../automojo-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../automojo-sysman-common/specs/vpn-client-management-base/design.md)
- [3-firewall-management](../3-firewall-management/design.md) — sibling sysman feature pattern
- [3-service-management](../3-service-management/design.md) — `launchctl` reused

---

## Testing Strategy

All tests use a `MockCommandRunner` keyed on the command-args prefix. No
test requires a live Homebrew, Tailscale, or Twingate installation.

**Example-based tests:**

- One command-construction test per write verb per provider per install
  source (`HOMEBREW` and `PKG` for Tailscale).
- One idempotency test per write verb.
- One dry-run test per write verb.
- One parser test per provider — `tailscale status --json` and
  `twingate status` mock outputs.
- One elevation test proving writes pass `elevated=True` (where applicable
  — Homebrew does not) and reads do not.
- One install-source switch test verifying behaviour with both
  `InstallSource.HOMEBREW` and `InstallSource.PKG`.

**Property tests (hypothesis):**

- `install_tailscale` idempotency — for any generated
  `(version, channel, install_source)` permutation, second call is
  `SKIPPED`.
- `uninstall` idempotency for both providers.
- `bring_up` idempotency on matching state.
- Dry-run safety across generated inputs.
