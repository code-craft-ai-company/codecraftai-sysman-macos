# Implementation Plan — macOS Security Compliance

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

> **Pilot note:** macOS lacks a unified compliance tool comparable to
> OpenSCAP.  This implementation is a primitive-aggregating skeleton,
> NOT a full compliance framework.  A future enhancement could
> integrate the [macOS Security Compliance Project (mSCP)](https://github.com/usnistgov/macos_security)
> baselines — see Follow-up #36.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.securitycompliance` module

- [x] 2. Define `MacosSecurityComplianceActionStatus` enum (aliased
  to the shared `ActionStatus`)

- [x] 3. Define `MacosSecurityComplianceManagerConfig` dataclass
  (`dry_run`, `elevated`, `output_dir`).
  *Pilot deviation from the Ubuntu sibling:* the macOS pilot does NOT
  expose a `datastream_path` field — there is no datastream concept
  on macOS.

- [x] 4. Define `MacosSecurityComplianceActionResult`,
  `MacosComplianceProfile`, `MacosComplianceFinding`,
  `MacosComplianceReport`, `MacosBaselineInfo` dataclasses

- [x] 5. Define `MacosSecurityComplianceQueryError` exception

## MacosSecurityComplianceManager — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional
  `config`.
  *Pilot deviation:* no file-operator parameter; baseline I/O goes
  through the runner via `cat` / `tee`.

- [x] 7. Implement `list_profiles()` returning the bundled catalogue
  (`cis_macos_baseline`, `apple_security_baseline`).
  *Pilot deviation:* the catalogue is static — macOS does not expose
  a runtime profile-discovery surface comparable to `oscap info`.

- [x] 8. Implement `get_baseline()` reading
  `<output_dir>/current_baseline.json` via `cat`

- [x] 9. Implement `set_baseline(spec)` writing the baseline JSON
  via `tee` with content-equality idempotency

- [x] 10. Implement `run_compliance_profile(profile_name)` dispatching
  the registered checks for the profile and assembling a
  `MacosComplianceReport`

- [x] 11. Implement the per-primitive checks:
  * `_check_sip` (csrutil status)
  * `_check_gatekeeper` (spctl --status)
  * `_check_filevault` (fdesetup status)
  * `_check_pf` (pfctl -si)
  * `_check_boot_policy` (bputil --display)

- [x] 12. Implement the per-primitive parsers:
  `_csrutil_status_is_enabled`, `_spctl_status_is_enabled`,
  `_fdesetup_status_is_on`, `_pfctl_status_is_enabled`,
  `_bputil_is_full_security`

- [x] 13. Implement `remediate(finding_id)` as a synchronous FAILED
  stub.
  *Pilot deviation from the cross-OS contract:* the macOS pilot
  intentionally does not attempt automated remediation.  Several
  settings (reduced-security boot policy, SIP toggling) require
  user-interactive Recovery mode.  The contract surface is preserved
  (the verb exists and returns a typed result) but every call yields
  `FAILED` with `stderr="remediation_unsupported"`.

## MacosSecurityComplianceManager — Gaps (specified but not implemented)

- [ ] 14. mSCP integration — generate baseline rules from the
  macOS Security Compliance Project YAML and feed them through
  `_run_check`.  Out of scope for the pilot.

- [ ] 15. Per-finding XML / structured-output reading — the pilot
  emits findings derived from textual primitive output only.

- [ ] 16. `auditd` (mac-native audit) integration — neither
  `auditd_running` nor `auditd_rules_match` is implemented.

- [ ] 17. `firewall_application_layer_enabled` (Application
  Firewall, distinct from pf) — not yet a registered check.

## Tests — Implemented

- [x] 18. `test_list_profiles_returns_bundled_catalogue`
- [x] 19. `test_run_compliance_profile_dispatches_each_primitive`
- [x] 20. `test_run_compliance_profile_marks_failures_when_primitive_disabled`
- [x] 21. `test_run_compliance_profile_marks_bputil_notapplicable_on_intel`
- [x] 22. `test_run_compliance_profile_unknown_profile_raises`
- [x] 23. `test_run_compliance_profile_marks_error_on_primitive_failure`
- [x] 24. `test_remediate_returns_failed_unsupported`
- [x] 25. `test_get_baseline_returns_none_when_file_missing`
- [x] 26. `test_get_baseline_decodes_existing_json`
- [x] 27. `test_set_baseline_writes_via_tee_when_content_differs`
- [x] 28. `test_set_baseline_skipped_when_content_matches`
- [x] 29. `test_set_baseline_dry_run_short_circuits`
- [x] 30. `test_set_baseline_elevates_when_configured`
- [x] 31. `test_run_compliance_reads_never_elevate`
- [x] 32. `test_csrutil_parser_handles_extended_status_block`

## Tests — Gaps

- [ ] 33. Per-primitive happy / sad / unsupported parser tests
  (currently exercised end-to-end via `run_compliance_profile`).

- [ ] 34. `get_baseline` resilience to malformed JSON / wrong
  top-level type — covered by source code (returns `None`) but not
  by a dedicated test.

## Follow-ups

- [ ] 35. **Cross-OS contract conformance review**: verify that
  `MacosComplianceReport`, `MacosComplianceFinding`,
  `MacosBaselineInfo`, and `MacosSecurityComplianceActionResult`
  shapes match the abstract contract in
  `automojo-sysman-common/specs/contracts/security-compliance/contract.md`.
  Several contract verbs (`get_compliance_history`,
  `compare_baselines`) are not implemented and should be either
  removed from the macOS contract row or added as follow-up tasks.

- [ ] 36. **mSCP integration** *(critical follow-up)*: replace the
  static catalogue with mSCP-derived rule sets so the manager can
  honour CIS / DISA STIG / NIST 800-53 baselines authoritatively.
  This is the single largest gap between the pilot and a
  production-grade macOS compliance manager.

- [ ] 37. **Recovery-mode remediation broker**: design a side-channel
  (e.g., a launchd-priority job that runs from Recovery on next
  reboot) that can apply settings the pilot's `remediate` stub
  rejects.  Out of scope for sysman; belongs in a sibling
  `automojo-sysman-macos-recovery` package.

- [ ] 38. **Application Firewall coverage**: register a check that
  reads `socketfilterfw --getglobalstate` to surface the macOS
  application-layer firewall (distinct from pf).
