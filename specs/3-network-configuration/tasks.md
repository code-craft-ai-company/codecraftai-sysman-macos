# Implementation Plan — macOS Network Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.  This batch lands the contract's
core surface; spec items not implemented in this batch (persistent
routes, Wi-Fi / 802.1x extensions, search-domain editing) are flagged
under "Gaps" with a follow-up note.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.networkconfiguration`
  module

- [x] 2. Define `MacosNetworkActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [x] 3. Define `MacosNetworkConfigurationManagerConfig` dataclass
  (dry_run, elevated)

- [x] 4. Define `MacosNetworkActionResult` dataclass with `interface`,
  `service_name`, `destination`, and `message` fields

- [x] 5. Define `MacosInterfaceInfo` and `MacosRouteInfo`
  dataclasses

- [x] 6. Define `MacosNetworkQueryError` exception

## Parsing Helpers

- [x] 7. Implement `_parse_hardware_ports(text)` — split
  `networksetup -listallhardwareports` output into device → service
  name dict

- [x] 8. Implement `_parse_ifconfig(text, device, service_name)` —
  extract `mac_address`, IPv4 / IPv6 addresses (hex netmask → prefix
  length conversion), `oper_state`, and `mtu`

- [x] 9. Implement `_parse_netstat_routes(text)` — split into IPv4
  `Internet:` and IPv6 `Internet6:` tables, normalise `default` to
  `0.0.0.0/0` / `::/0`, and coerce `link#N` / MAC-shaped gateways
  to `None` (provided as a module-level helper for tests; manager
  prefers `route -n get` for idempotency)

- [x] 10. Implement `_parse_getinfo(text)` — extract `IP address`,
  `Subnet mask`, and `Router` for the `configure_interface`
  idempotency guard

- [x] 11. Implement `_parse_getdnsservers(text)` — recognise the
  literal placeholder line as an empty list

- [x] 12. Implement `_resolve_service_name(device)` — translate a
  BSD device name into its System Configuration service name via
  the cached hardware-port map

## MacosNetworkConfigurationManager — Implemented

- [x] 13. Implement `__init__` accepting runner + optional config;
  initialise the empty hardware-port cache

- [x] 14. Implement `get_interfaces()` — dispatch
  `networksetup -listallhardwareports`, then loop `ifconfig
  <device>` per discovered device

- [x] 15. Implement `configure_interface(name, address, netmask,
  gateway)` — resolve service name, probe
  `networksetup -getinfo <servicename>` for idempotency, dispatch
  `networksetup -setmanual <servicename> <address> <netmask>
  <gateway or "">`

- [x] 16. Implement `enable_interface(name)` — probe
  `-getnetworkserviceenabled`, dispatch
  `networksetup -setnetworkserviceenabled <servicename> on`; skip
  when already enabled

- [x] 17. Implement `disable_interface(name)` — probe
  `-getnetworkserviceenabled`, dispatch
  `networksetup -setnetworkserviceenabled <servicename> off`;
  skip when already disabled

- [x] 18. Implement `add_route(dest, gateway)` — probe
  `route -n get <dest>`, dispatch `route -n add <dest> <gateway>`;
  result `message` notes runtime-only persistence

- [x] 19. Implement `remove_route(dest)` — probe `route -n get
  <dest>`, dispatch `route -n delete <dest>`; skip when absent

- [x] 20. Implement `set_dns_servers(interface, servers)` — probe
  `get_dns_servers()`, dispatch
  `networksetup -setdnsservers <servicename> <servers...>` (or
  `empty` for `servers=[]`)

- [x] 21. Implement `get_dns_servers(interface)` — dispatch
  `networksetup -getdnsservers <servicename>`; map the literal
  "There aren't any DNS Servers set..." to `[]`

- [x] 22. Implement `get_hostname()` — dispatch `scutil --get
  HostName`, strip stdout

- [x] 23. Implement `set_hostname(name)` — probe all three
  `scutil --get` values for idempotency (LocalHostName compared
  after `.` → `-` normalisation), then dispatch the three
  `scutil --set` calls in order; stop at the first failure and
  report the failing surface in `message`

- [x] 24. Implement `_dispatch_write` helper — runner-result
  mapping, elevation decision, `command` capture, optional
  ``success_message`` for `add_route`'s contract-mandated note

## MacosNetworkConfigurationManager — Gaps (specified but not implemented in this batch)

- [ ] 25. `get_interface(name)` single-interface probe (covered by
  `get_interfaces()` for now; standalone verb is a thin wrapper
  around `_read_interface`)

- [ ] 26. `set_search_domains(domains)` — see contract; not in this
  batch

- [ ] 27. `get_routes()` — manager exposes the `_parse_netstat_routes`
  parser already; the public verb is deferred

- [ ] 28. Persistent-route launchd plist authoring (`route add` at
  boot) — out of base contract; reserved extension documented in
  design

- [ ] 29. Wi-Fi SSID / WPA configuration via
  `networksetup -setairportnetwork` — out of base contract

- [ ] 30. VPN / 802.1x profile management — out of base contract

- [ ] 31. Per-service IPv6 tuning
  (`networksetup -setv6linklocal`) — out of base contract

## Tests — Implemented

- [x] 32. `test_parse_hardware_ports_extracts_device_to_service_map`
- [x] 33. `test_parse_ifconfig_converts_hex_netmask_to_prefix`
- [x] 34. `test_hex_netmask_to_prefix_handles_common_cases`
- [x] 35. `test_parse_getinfo_extracts_canonical_keys`
- [x] 36. `test_parse_getdnsservers_returns_empty_for_placeholder_line`
- [x] 37. `test_parse_getdnsservers_returns_servers_in_order`
- [x] 38. `test_parse_route_get_gateway_extracts_field`
- [x] 39. `test_parse_netstat_routes_normalises_default_and_link_gateways`
- [x] 40. `test_get_interfaces_builds_map_from_listallhardwareports`
- [x] 41. `test_configure_interface_resolves_bsd_to_service_name`
- [x] 42. `test_configure_interface_empty_string_for_null_gateway`
- [x] 43. `test_configure_interface_skipped_when_getinfo_matches`
- [x] 44. `test_configure_interface_dry_run_does_not_invoke_runner`
- [x] 45. `test_enable_interface_skipped_when_already_enabled`
- [x] 46. `test_disable_interface_invokes_setnetworkserviceenabled_off`
- [x] 47. `test_add_route_dispatches_bsd_route_add`
- [x] 48. `test_add_route_skipped_when_present`
- [x] 49. `test_remove_route_skipped_when_absent`
- [x] 50. `test_set_dns_servers_uses_empty_literal_for_empty_list`
- [x] 51. `test_set_dns_servers_skipped_when_current_matches`
- [x] 52. `test_get_dns_servers_returns_empty_for_placeholder`
- [x] 53. `test_set_hostname_dispatches_three_scutil_calls`
- [x] 54. `test_set_hostname_local_name_normalises_dots_to_dashes`
- [x] 55. `test_set_hostname_skipped_when_all_three_match`
- [x] 56. `test_set_hostname_failed_identifies_failing_surface`
- [x] 57. `test_get_hostname_returns_stripped_stdout`
- [x] 58. `test_writes_pass_elevated_when_configured`
- [x] 59. `test_reads_never_elevate_even_with_elevated_config`
- [x] 60. `test_failed_command_returns_failed_result_with_stderr`
- [x] 61. `test_resolve_service_name_failure_surfaces_as_failed_result`
- [x] 62. `test_get_dns_servers_raises_query_error_on_unexpected_failure`

## Tests — Gaps

- [ ] 63. Property-based tests (hypothesis) covering Properties
  1 through 26

- [ ] 64. Retry-aspect test proving a transient
  `networksetup -setmanual` failure followed by success is
  transparent to the manager

## Follow-ups

- [ ] 65. **Cross-OS contract conformance review**: the macOS
  implementation accepts the BSD device name as the canonical `name`
  argument on `configure_interface` / `enable_interface` /
  `disable_interface` / `set_dns_servers`, and the resolved service
  name is surfaced on the result for callers that need to log or
  feed it back through `networksetup`.  Confirm the contract's
  Interface-Name-Mapping section calls this out and that the contract
  permits result-shape additions (`service_name`, `destination`).

- [ ] 66. **Hostname triple deviation**: the contract specifies a
  single `set_hostname(name)` verb; this implementation writes three
  macOS surfaces (`HostName`, `ComputerName`, `LocalHostName`) so a
  cross-OS coordinator's single call has the same observable effect
  as Linux's `hostnamectl set-hostname`.  The triple is documented
  in the docstring; the contract's macOS row should explicitly
  permit this expansion.

- [ ] 67. **Persistent-route extension plan**: design the launchd
  plist shape required to turn `add_route` into a persistent write,
  and decide the feature-flag name that gates it.

- [ ] 68. **codecraftai-* reuse check**: confirm no other feature in
  `codecraftai-sysman-macos` invokes `networksetup` / `ifconfig` /
  `netstat` / `route` / `scutil` directly; all such calls should
  go through `MacosNetworkConfigurationManager`.
