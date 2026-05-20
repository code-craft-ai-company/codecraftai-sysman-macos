# Requirements

## Introduction

The vpn-client-management feature provides the `automojo-sysman-macos` package
with the ability to install, configure, and lifecycle the **Tailscale** client
and the **Twingate Connector** on macOS hosts. This spec implements the
vpn-client-management contract defined at
`automojo-sysman-common/specs/contracts/vpn-client-management/contract.md`
for macOS using either the Homebrew formula `tailscale` or the signed `.pkg`
from Tailscale, and the signed `.pkg` distribution from Twingate. Service
lifecycle is controlled via `launchctl`.

A maintenance coordinator drives all operations through this feature's public
interfaces. All system commands flow through `ICommandRunner`, with the
`aspects` parameter forwarded on every call. Companion features
[`1-command-execution`](../1-command-execution/requirements.md),
[`2-maintenance-reporting`](../2-maintenance-reporting/requirements.md),
[`3-package-management`](../3-package-management/requirements.md) (`brew`),
and [`3-service-management`](../3-service-management/requirements.md)
(`launchctl`) supply the runner, reporting, package, and service primitives
this feature composes on.

---

## Requirements

### Requirement 1: Install Tailscale via Homebrew or Signed PKG

**User Story:** As a maintenance coordinator, I want to install Tailscale on a
macOS host using either the Homebrew formula or the official signed `.pkg`,
so that the daemon binary and `tailscale` CLI are present.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_tailscale(version=None,
   channel="stable", aspects=…)` AND `config.install_source ==
   InstallSource.HOMEBREW` THE SYSTEM SHALL invoke `brew install tailscale`
   via the runner.
2. WHEN `config.install_source == InstallSource.PKG` THE SYSTEM SHALL fetch
   the signed `.pkg` from the Tailscale download URL via the runner using
   `curl -fsSL -o <path>` and install it via `installer -pkg <path> -target
   /`.
3. WHEN `version` is a concrete string THE SYSTEM SHALL pin the install to
   that version — `brew install tailscale@<version>` (Homebrew) or by
   substituting the version into the download URL (PKG).
4. WHEN Tailscale is already installed at the requested version THE SYSTEM
   SHALL return `MacosVPNActionResult(status=SKIPPED, provider=TAILSCALE)`.
5. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the `installer` command (PKG path). Homebrew runs
   unelevated by convention.
6. IF the install fails THEN THE SYSTEM SHALL return
   `MacosVPNActionResult(status=FAILED, …)`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 2: Install the Twingate Connector via Signed PKG

**User Story:** As a maintenance coordinator, I want to install the Twingate
Connector on a macOS host using the signed `.pkg`, so that the connector
service is registered with `launchd`.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_twingate_connector(version=None,
   aspects=…)` THE SYSTEM SHALL fetch the signed Twingate `.pkg` via
   `curl -fsSL -o <path>` and install it via `installer -pkg <path>
   -target /`.
2. WHEN `version` is a concrete string THE SYSTEM SHALL substitute it into
   the download URL.
3. WHEN the Connector is already installed at the requested version THE
   SYSTEM SHALL return `MacosVPNActionResult(status=SKIPPED,
   provider=TWINGATE)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the `installer` command.
5. IF install fails THEN THE SYSTEM SHALL return
   `MacosVPNActionResult(status=FAILED, …)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 3: Configure a Provider

**User Story:** As a maintenance coordinator, I want to write the Twingate
Connector configuration file, so the daemon has the inputs it needs before
bring-up.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure(provider=VPNProvider.TWINGATE,
   settings=…, aspects=…)` THE SYSTEM SHALL render
   `/Library/Application Support/Twingate/connector.conf` via the runner
   with `elevated=True` WHERE the runner is configured for elevation.
2. WHEN `provider=VPNProvider.TAILSCALE` THE SYSTEM SHALL return
   `MacosVPNActionResult(status=SKIPPED, provider=TAILSCALE,
   detail="tailscale has no on-disk configure step on macos")`.
3. WHEN the rendered content already matches what is on disk THE SYSTEM
   SHALL return `MacosVPNActionResult(status=SKIPPED, …)`.
4. IF the file write fails THEN THE SYSTEM SHALL return
   `MacosVPNActionResult(status=FAILED, …)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 4: Bring the Tunnel Up

**User Story:** As a maintenance coordinator, I want to authenticate the host
to the tailnet (Tailscale) or network (Twingate) and bring the tunnel up.

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_up(provider=TAILSCALE, authkey=<key>,
   hostname=<name>, advertise_tags=<tags>, …)` THE SYSTEM SHALL invoke
   `launchctl bootstrap system /Library/LaunchDaemons/com.tailscale.tailscaled.plist`
   (when not already loaded), followed by `tailscale up --authkey=<key>
   --hostname=<name> …`.
2. WHEN the coordinator calls `bring_up(provider=TWINGATE, …)` THE SYSTEM
   SHALL invoke `launchctl bootstrap system
   /Library/LaunchDaemons/com.twingate.connector.plist` after `configure`
   has written the token.
3. WHEN `status(...)` already shows `running=True` with matching hostname/
   tags THE SYSTEM SHALL return `MacosVPNActionResult(status=SKIPPED, …)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for `launchctl` and `tailscale up`.
5. IF the command fails THEN THE SYSTEM SHALL return
   `MacosVPNActionResult(status=FAILED, …)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 5: Bring the Tunnel Down

**User Story:** As a maintenance coordinator, I want to take the host off the
overlay without uninstalling.

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_down(provider=TAILSCALE, aspects=…)`
   THE SYSTEM SHALL invoke `tailscale down`.
2. WHEN the coordinator calls `bring_down(provider=TWINGATE, aspects=…)`
   THE SYSTEM SHALL invoke `launchctl bootout system
   /Library/LaunchDaemons/com.twingate.connector.plist`.
3. WHEN `status` already shows `running=False` THE SYSTEM SHALL return
   `MacosVPNActionResult(status=SKIPPED, …)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 6: Report Client Status

**User Story:** As a maintenance coordinator, I want to read the current state
of the VPN client without invoking writes.

#### Acceptance Criteria

1. WHEN the coordinator calls `status(provider=TAILSCALE, aspects=…)` THE
   SYSTEM SHALL invoke `tailscale status --json` without elevation and parse
   the JSON into a `MacosVPNClientStatus`.
2. WHEN backend state is `Running` THE SYSTEM SHALL populate `running=True`,
   `self_address`, `hostname`, `tailnet`, `peer_addresses`, and `version`.
3. WHEN the coordinator calls `status(provider=TWINGATE, …)` THE SYSTEM SHALL
   invoke `twingate status` and parse the text output into
   `MacosVPNClientStatus(self_address=None, peer_addresses={})`.
4. WHEN the daemon binary is not installed THE SYSTEM SHALL return
   `MacosVPNClientStatus(installed=False, running=False, …)`.
5. IF the status command fails for any other reason THEN THE SYSTEM SHALL
   raise `MacosVPNClientQueryError`.

---

### Requirement 7: Uninstall

**User Story:** As a maintenance coordinator, I want to uninstall a VPN
client and optionally purge its state.

#### Acceptance Criteria

1. WHEN the coordinator calls `uninstall(provider=TAILSCALE, purge=False,
   aspects=…)` AND the manager was installed via Homebrew THE SYSTEM SHALL
   invoke `brew uninstall tailscale`.
2. WHEN the manager was installed via signed PKG THE SYSTEM SHALL run the
   bundled uninstaller script at
   `/Library/Application Support/Tailscale/uninstall.sh` via the runner.
3. WHEN `purge=True` THE SYSTEM SHALL additionally remove
   `/Library/Tailscale` and `~/Library/Group Containers/<tailscale-group>`.
4. WHEN the coordinator calls `uninstall(provider=TWINGATE, …)` THE SYSTEM
   SHALL apply the equivalent steps for the Twingate Connector.
5. WHEN the package is already absent THE SYSTEM SHALL return
   `MacosVPNActionResult(status=SKIPPED, …)`.
6. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for `installer`, `launchctl`, and any state-directory
   `rm` step.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `MacosVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 8: Aspects Forwarding

**User Story:** As a maintenance coordinator, I want every behaviour-bearing
operation to take an `aspects` parameter.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept an `aspects: Aspects` parameter on every write
   and read verb.
2. THE SYSTEM SHALL forward `aspects` to the runner verbatim.
3. THE SYSTEM SHALL NOT apply any `@retry` or behaviour-modifying decorator.
