# Implementation Plan — macOS VPN Client Management

## Module and Types

- [x] 1. Create the `ccai.sysman.macos.vpnclientmanagement` module.

- [x] 2. Re-export `MacosVPNActionStatus`, `MacosVPNActionResult`,
  `MacosVPNClientStatus`, `MacosVPNClientQueryError` from the shared base
  under macOS-prefixed names.

- [x] 3. Define the `InstallSource` enum (`HOMEBREW`, `PKG`).

- [x] 4. Define `MacosVPNClientManagerConfig` extending
  `VPNClientManagerConfig` with `install_source`, `tailscale_plist_path`,
  `twingate_plist_path`, `twingate_config_path`, `pkg_cache_dir` fields.

## MacosVPNClientManager — Implementation

- [x] 5. Implement `__init__(*, runner, config=None)` calling the base.

- [x] 6. Implement `_brew_installed_version(formula, aspects)` using
  `brew list --versions <formula>`; return `None` when absent.

- [x] 7. Implement `_pkgutil_installed_version(pkg_id, aspects)` using
  `pkgutil --pkg-info <id>`; return `None` when absent.

- [x] 8. Implement `install_tailscale(*, version, channel, aspects)` —
  branch on `install_source`; Homebrew path runs `brew install
  tailscale[@<version>]` unelevated; PKG path runs
  `curl -fsSL -o … <url>` then `installer -pkg … -target /` elevated.

- [x] 9. Implement `install_twingate_connector(*, version, aspects)` —
  always PKG path.

- [x] 10. Implement `configure(*, provider, settings, aspects)` —
  Twingate writes config via runner heredoc through `tee`; Tailscale
  returns `SKIPPED`.

- [x] 11. Implement `bring_up(*, provider, authkey, hostname,
  advertise_tags, advertise_routes, exit_node, aspects)` —
  `launchctl bootstrap system <plist>` (when not already loaded) then
  `tailscale up …`; Twingate is just the `launchctl bootstrap`.

- [x] 12. Implement `bring_down(*, provider, aspects)` —
  `tailscale down` for Tailscale; `launchctl bootout system <plist>` for
  Twingate.

- [x] 13. Implement `status(*, provider, aspects)`.

- [x] 14. Implement `uninstall(*, provider, purge, aspects)` —
  branch on installed source for Tailscale (`brew uninstall` vs bundled
  uninstaller script); optional `rm` of state directories.

## Parsers

- [x] 15. Implement `_parse_tailscale_status_json(payload)` returning
  `MacosVPNClientStatus`. Total over all backend states.

- [x] 16. Implement `_parse_twingate_status_text(payload)` returning
  `MacosVPNClientStatus(self_address=None, peer_addresses={})`.

## Tests — Examples

- [x] 17. `test_install_tailscale_brew_runs_brew_install`.
- [x] 18. `test_install_tailscale_pkg_runs_curl_then_installer`.
- [x] 19. `test_install_tailscale_skipped_when_present_brew`.
- [x] 20. `test_install_tailscale_skipped_when_present_pkg`.
- [x] 21. `test_install_tailscale_failed_on_installer_error`.
- [x] 22. `test_install_twingate_connector_runs_curl_then_installer`.
- [x] 23. `test_configure_tailscale_returns_skipped`.
- [x] 24. `test_configure_twingate_writes_conf_via_tee`.
- [x] 25. `test_configure_twingate_skipped_when_content_matches`.
- [x] 26. `test_bring_up_tailscale_loads_plist_then_runs_up`.
- [x] 27. `test_bring_up_tailscale_skipped_when_running_with_matching_state`.
- [x] 28. `test_bring_up_twingate_runs_launchctl_bootstrap`.
- [x] 29. `test_bring_down_tailscale_runs_tailscale_down`.
- [x] 30. `test_bring_down_twingate_runs_launchctl_bootout`.
- [x] 31. `test_status_tailscale_parses_running_json`.
- [x] 32. `test_status_tailscale_returns_installed_false_when_missing`.
- [x] 33. `test_status_twingate_parses_text`.
- [x] 34. `test_uninstall_tailscale_brew_runs_brew_uninstall`.
- [x] 35. `test_uninstall_tailscale_pkg_runs_bundled_uninstaller`.
- [x] 36. `test_dry_run_short_circuits_every_write_verb`.
- [x] 37. `test_writes_pass_elevated_when_configured_excluding_brew`.
- [x] 38. `test_reads_never_elevate`.

## Tests — Property (hypothesis)

- [x] 39. `test_property_install_tailscale_idempotent` — generated
  `(version, channel, install_source)`.
- [x] 40. `test_property_uninstall_idempotent`.
- [x] 41. `test_property_bring_up_idempotent_on_matching_state`.
- [x] 42. `test_property_dry_run_never_invokes_runner`.

## Documentation

- [x] 43. Sphinx-style docstrings on every public method.

- [x] 44. Module-level docstring linking to the contract and the base spec.

## Follow-ups

- [x] 45. Cross-OS contract conformance check.

- [x] 46. Confirm the `pkg_cache_dir` is created on demand (via `mkdir -p`
  through the runner) before `curl -o` is invoked.

- [x] 47. macOS notarisation / Gatekeeper note — confirm both Tailscale and
  Twingate `.pkg` files install cleanly under a default Gatekeeper policy;
  surface a follow-up if a stapled-ticket check is needed.
