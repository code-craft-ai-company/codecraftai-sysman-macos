# Implementation Plan — macOS Firewall Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

> **Pilot note:** macOS pf has no first-class zone concept.  This
> implementation maps each contract `zone` to a per-anchor file under
> `/etc/pf.anchors/ccai.<zone>` and sets the active zone by
> rewriting `/etc/pf.conf` so that anchor is loaded first.  See
> Follow-up #34 for the cross-OS contract conformance review.

## Module and Models

- [x] 1. Create the `ccai.sysman.macos.firewallmanagement` module

- [x] 2. Define `MacosFirewallActionStatus` enum (aliased to the
  shared `ActionStatus`)

- [x] 3. Define `MacosFirewallManagerConfig` dataclass (`dry_run`,
  `elevated`, `anchor_file_prefix`, `pf_conf_path`, `services_table`)

- [x] 4. Define `MacosFirewallActionResult`, `MacosZoneInfo`,
  `MacosFirewallState` dataclasses and `MacosFirewallQueryError`
  exception

## MacosFirewallManager — Implemented

- [x] 5. Implement `__init__` accepting `runner` and optional
  `config`.
  *Pilot deviation:* no `file_operator` parameter; anchor / pf-conf
  I/O goes through the runner via `cat` / `tee` / `test -e`.

- [x] 6. Implement the `_mutate_anchor` helper that reads the
  per-zone anchor file via `cat`, computes the desired post-state
  (with or without a given rule line), rewrites the file via `tee`,
  and reloads the anchor via `pfctl -a <anchor> -f <path>`

- [x] 7. Implement `add_service(service, zone)` /
  `remove_service(service, zone)` with bundled-services-table
  resolution and idempotency

- [x] 8. Implement `add_port(port, protocol, zone)` /
  `remove_port(port, protocol, zone)` with protocol validation and
  idempotency

- [x] 9. Implement `add_rich_rule(rule, zone)` /
  `remove_rich_rule(rule, zone)` with verb validation and
  whitespace-normalised idempotency matching

- [x] 10. Implement `set_default_zone(zone)` that ensures
  `/etc/pf.conf` declares `ccai.<zone>` as the FIRST ccai
  anchor, rewrites pf-conf, and reloads via `pfctl -f`.
  *Pilot deviation from the design:* idempotency compares the *first
  ccai zone* in pf-conf to the requested zone rather than full
  rendered-body equality.  This avoids spurious rewrites when the
  user's pf-conf has comment / whitespace differences that would not
  affect pf semantics.

- [x] 11. Implement `reload()` dispatching `pfctl -f /etc/pf.conf`

- [x] 12. Implement `query_zone(zone)` parsing
  `pfctl -a ccai.<zone> -sr` into a `MacosZoneInfo` with
  ports / services back-resolution against the bundled table

- [x] 13. Implement `query_all_zones()` enumerating
  `ccai.*` anchors via `pfctl -sA` and querying each

- [x] 14. Implement `query_state()` reading `pfctl -si` and the
  default zone from `/etc/pf.conf`

- [x] 15. Implement the bundled services table covering
  ssh / http / https / smtp / dns / ftp / telnet / pop3 / imap /
  submission / mysql / postgresql (12 entries).
  *Pilot limitation:* unknown service names FAIL eagerly.  Callers
  needing a richer table can pass `services_table=...` on the config;
  the override completely replaces the bundled defaults.

## MacosFirewallManager — Gaps (specified but not implemented)

- [ ] 16. Dynamic services-table resolution from `/etc/services`
  (the BSD canonical port-name database).  The pilot's bundled
  table is intentionally narrow.

- [ ] 17. pf application-firewall integration via
  `socketfilterfw` — distinct from pf and not exposed by this
  manager.  See sibling Follow-up in `3-security-compliance`.

- [ ] 18. Per-anchor "interfaces" / "sources" surfaces — pf has no
  per-anchor concept of either, so the corresponding fields on
  `MacosZoneInfo` are always empty lists.  Cross-OS callers should
  treat them as advisory.

- [ ] 19. Stateful rule-equivalence matching — pf's quick-match
  semantics mean two functionally equivalent rules can be textually
  distinct.  This pilot only matches on whitespace-normalised string
  equality.  A future enhancement could lex pf rules into a
  canonical form before comparison.

## Tests — Implemented

- [x] 20. `test_add_service_writes_pass_rule_and_reloads_anchor`
- [x] 21. `test_add_service_skipped_when_rule_already_present`
- [x] 22. `test_add_service_unknown_service_fails`
- [x] 23. `test_remove_service_removes_rule_and_reloads`
- [x] 24. `test_remove_service_skipped_when_rule_absent`
- [x] 25. `test_add_port_appends_rule`
- [x] 26. `test_add_port_invalid_protocol_fails`
- [x] 27. `test_remove_port_removes_rule_and_reloads`
- [x] 28. `test_add_rich_rule_appends_normalised_line`
- [x] 29. `test_add_rich_rule_invalid_verb_fails`
- [x] 30. `test_remove_rich_rule_skipped_when_absent`
- [x] 31. `test_rich_rule_idempotency_uses_normalised_match`
- [x] 32. `test_set_default_zone_rewrites_pf_conf_and_reloads`
- [x] 33. `test_set_default_zone_skipped_when_already_first`
- [x] 34. `test_reload_dispatches_pfctl_f`
- [x] 35. `test_query_zone_parses_pass_rules_and_back_resolves_services`
- [x] 36. `test_query_zone_raises_query_error_on_failure`
- [x] 37. `test_query_all_zones_filters_to_ccai_anchors`
- [x] 38. `test_query_state_reports_running_when_pfctl_si_enabled`
- [x] 39. `test_query_state_reports_inactive_when_status_disabled`
- [x] 40. `test_add_service_dry_run_short_circuits`
- [x] 41. `test_writes_elevate_when_configured_reads_do_not`
- [x] 42. `test_services_table_override_replaces_bundled_defaults`

## Tests — Gaps

- [ ] 43. Multi-rule mutation test — adding two services and
  asserting the resulting anchor body contains both rules in
  declaration order.

- [ ] 44. Reload-failure test — the `_mutate_anchor` helper handles
  a non-zero `pfctl -a -f` exit, but the case is not exercised by a
  dedicated test.

- [ ] 45. Pf-conf preservation test — `set_default_zone` should
  preserve non-ccai anchor / scrub directives verbatim.  The
  source code does this but a dedicated assertion is deferred.

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: verify that
  `MacosFirewallActionResult`, `MacosZoneInfo`, and
  `MacosFirewallState` shapes match the abstract contract in
  `codecraftai-sysman-common/specs/contracts/firewall-management/contract.md`.
  Notable deviations: the macOS pilot's `MacosZoneInfo.interfaces`
  and `MacosZoneInfo.sources` are always empty (pf has no per-anchor
  concept of either); the contract row may need to mark these
  optional or document the macOS gap.

- [ ] 47. **Services-table extensibility**: design a hybrid
  resolution strategy that consults the bundled table first, then
  falls back to `/etc/services` via `cat`.  Today an override either
  wholly replaces or wholly accepts the bundled defaults.

- [ ] 48. **Rule canonicalisation**: build a small pf rule lexer so
  idempotency can match on canonical form rather than
  whitespace-normalised string equality.  This unlocks reliable
  rich-rule round-tripping when callers vary token order.

- [ ] 49. **`socketfilterfw` integration**: separate manager (or
  separate verb namespace) for the macOS application-layer firewall.
  Distinct from pf; out of scope for this pilot.
