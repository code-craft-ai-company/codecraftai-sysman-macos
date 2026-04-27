# Tasks

## Pilot Scope

The pilot exposes eight verbs (`add_zone`, `remove_zone`,
`list_zones`, `add_record`, `remove_record`, `list_records`,
`reload_zone`, `get_zone_status`) through a single
`MacosDnsServerManager`.  Every verb is dispatched through the
injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` (~280 lines) describing the
      eleven requirements that drive the implementation.
- [x] Author `design.md` describing the architecture, the
      cross-OS DNS-server-model concept-mapping table, the
      macOS BIND-vs-Server.app path divergence, the
      sentinel-bracketed block convention, the SOA serial
      bumping algorithm, the validate-before-apply discipline,
      the elevation policy, and the documented contract drift.
- [x] Author `dnsservermanagement.py` with:
    - [x] `MacosDnsActionStatus` (alias of `ActionStatus`).
    - [x] `MacosDnsManagerConfig` dataclass.
    - [x] `MacosDnsActionResult` dataclass.
    - [x] `MacosDnsZoneSpec` dataclass.
    - [x] `MacosDnsZoneInfo` dataclass.
    - [x] `MacosDnsRecordSpec` dataclass.
    - [x] `MacosDnsRecordInfo` dataclass.
    - [x] `MacosDnsZoneStatus` dataclass.
    - [x] `MacosDnsQueryError` exception.
    - [x] `MacosDnsServerManager` class with the eight verbs.
    - [x] Module-level renderer `_render_zone_block`.
    - [x] Module-level renderer `_render_initial_zone_file`.
    - [x] Module-level renderer `_render_record_line`.
    - [x] Module-level helper `_bump_soa_serial`.
    - [x] Module-level parser `_parse_zone_records`.
    - [x] Module-level parser `_parse_managed_zone_blocks`.
    - [x] Module-level parser `_parse_named_checkconf_p`.
    - [x] Module-level parser `_parse_rndc_zonestatus`.
- [x] Author the test module with at least 14 tests covering:
    - [x] `add_zone` argv shape.
    - [x] `add_zone` SKIPPED when sentinel block + zone file present.
    - [x] `add_zone` DRY_RUN does not invoke the runner.
    - [x] `remove_zone` argv shape.
    - [x] `remove_zone` SKIPPED when absent.
    - [x] `list_zones` parser handles managed + external blocks.
    - [x] `list_zones` raises on `named-checkconf -p` failure.
    - [x] `add_record` argv shape and serial bump.
    - [x] `add_record` SKIPPED when present.
    - [x] `remove_record` argv shape.
    - [x] `list_records` parses A / AAAA / CNAME / TXT.
    - [x] `reload_zone` invokes `rndc reload`.
    - [x] `get_zone_status` parses `rndc zonestatus`.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] SOA serial increases on record changes.
    - [x] Record-line renderer emits priority for MX records.
    - [x] Round-trip render-then-parse for records.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **DNSSEC zone signing.** Adopt the RHEL implementation's
  `dnssec-policy` surface and KSK / ZSK rollover.
- **TSIG / transfer-key management.**
- **RPZ (response-policy zones).**
- **Views / split-horizon.**
- **Dynamic-update ACL beyond the boolean toggle.**
- **DoT / DoH listener configuration.**
- **`get_server_status`.** Daemon-level status delegated to
  `3-service-management`.
- **mDNSResponder integration.** Out of scope.
- **Server.app auto-detection.** Operators must override the
  path defaults explicitly.

---

## Acceptance Snapshot

`requirements.md` covers eleven requirements.  `design.md`
documents the BIND-9 zone-data model, the cross-OS concept
mapping, the macOS BIND-vs-Server.app path divergence, the
SOA serial bumping algorithm, the validate-before-apply
discipline, and the spec-vs-contract drift.

---

## Test Bar

- Minimum 14 tests; current count at completion: 17.
- Every test uses `_MockCommandRunner` — no live BIND
  invocations.
- The mock seeds `RunResult` values keyed by argv tuple so
  the same runner can stand in for the entire flow of an
  idempotent verb.
