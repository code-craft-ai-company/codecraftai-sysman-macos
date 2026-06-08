# Implementation Plan — Certificate Trust Management (macOS)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificatetrustmanagement` module under
  `source/packages/ccai/sysman/macos/`.

- [x] 2. Define `MacosCertTrustActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `MacosCertTrustManagerConfig` dataclass:
  `dry_run`, `elevated`, `store_path_map`, `default_store`,
  `security_executable`, `openssl_executable`.

- [x] 4. Define `MacosCertTrustActionResult` dataclass extending
  `FeatureActionResult` with `store`, `fingerprint`, `path`.

- [x] 5. Define `MacosCertTrustInfo` dataclass with
  `fingerprint`, `subject`, `issuer`, `not_before`, `not_after`,
  `serial_number`, `store`, `path`, `purposes`.

- [x] 6. Define `MacosCertTrustQueryError` exception.

## MacosCertificateTrustManager — Implemented

- [x] 7. Implement `__init__`.

- [x] 8. Implement `add_trusted_cert(store, path, purpose=None)`:
  - Resolves store -> keychain.
  - Reads source via `cat`.
  - Validates PEM in pure Python.
  - Computes SHA-256 fingerprint in pure Python (uppercase
    no-colon).
  - Probes keychain via `security find-certificate -Z` for
    idempotency.
  - Honours dry-run.
  - Dispatches `security add-trusted-cert -d -r trustRoot -k
    <keychain> <path>`.
  - Honours `elevated` for `system-ca`; never elevates for
    `user-ca`.

- [x] 9. Implement `remove_trusted_cert(store, fingerprint)`:
  - Probes keychain via `security find-certificate -Z` for
    idempotency.
  - Honours dry-run.
  - Dispatches `security delete-certificate -Z <fp> <keychain>`.

- [x] 10. Implement `list_trusted_certs(store)`:
  - Raises `MacosCertTrustQueryError` for unsupported store.
  - Dispatches `security find-certificate -a -p <keychain>`.
  - Splits multi-PEM stream into envelopes.
  - Pipes each envelope through `openssl x509 -text` for metadata.
  - Reads never elevate.

- [x] 11. Implement `get_cert_details(fingerprint)`:
  - Iterates every supported keychain, dispatches
    `security find-certificate -Z <fp> -p <keychain>`.
  - Returns `None` when no match.
  - Reads never elevate.
  - Accepts colonised / non-colonised / prefixed input.

- [x] 12. Implement `update_trust_store()`:
  - Returns `SUCCESS` no-op (keychain reflects changes
    immediately).
  - Honours dry-run for symmetry.

## Helpers

- [x] 13. `_parse_pem_blocks` — split content into CERTIFICATE
  envelopes (handles multi-PEM streams from
  `security find-certificate -a -p`).

- [x] 14. `_compute_fingerprint` — pure-Python SHA-256, formatted
  per OS convention (uppercase no-colon for macOS).

- [x] 15. `_normalize_fingerprint` — accept various input shapes,
  return canonical macOS uppercase no-colon form.

- [x] 16. `_parse_openssl_cert_text` — extract subject / issuer /
  validity / serial / EKU.

## Tests — Implemented

- [x] 17. `test_add_trusted_cert_invokes_security`.

- [x] 18. `test_add_trusted_cert_skipped_when_fingerprint_present`.

- [x] 19. `test_add_trusted_cert_dry_run_short_circuits`.

- [x] 20. `test_add_trusted_cert_invalid_pem_returns_failed`.

- [x] 21. `test_remove_trusted_cert_invokes_security`.

- [x] 22. `test_remove_trusted_cert_skipped_when_absent`.

- [x] 23. `test_list_trusted_certs_parses_output`.

- [x] 24. `test_get_cert_details_returns_full_info`.

- [x] 25. `test_get_cert_details_unknown_fingerprint_returns_none`.

- [x] 26. `test_update_trust_store_returns_success_noop`.

- [x] 27. `test_unsupported_store_returns_failed`.

- [x] 28. `test_unsupported_store_in_list_raises`.

- [x] 29. `test_writes_pass_elevated_true_for_system_store`.

- [x] 30. `test_user_store_writes_do_not_elevate`.

- [x] 31. `test_reads_never_elevate`.

- [x] 32. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 33. `set_store_enabled` — keychains have no
  enable / disable toggle.

- [ ] 34. `verify_certificate` — overlaps with
  `codecraftai-credentials`; defer per RHEL spec's task 21 follow-up.

- [ ] 35. `TrustPurpose` enum — defaulting to root-anchor trust
  (`-r trustRoot`).  Per-purpose policy
  (`security add-trusted-cert -p ...`) is a follow-up.

## Follow-ups

- [ ] 36. **Login keychain unlock helper** — wrap
  `security unlock-keychain -p <password> <keychain>` for batch
  workflows.  Currently the runner / aspect chain is responsible
  for keychain unlock.

- [ ] 37. **System Roots inspection** — Apple's
  `/System/Library/Keychains/SystemRootCertificates.keychain` is
  read-only and not currently mapped; add a virtual
  `"system-roots"` store for read-only enumeration.

- [ ] 38. **Per-purpose trust policy** — currently every anchor is
  installed with `-r trustRoot`.  Map `TrustPurpose.TLS_SERVER`
  -> `-p ssl`, `TrustPurpose.EMAIL` -> `-p smime`, etc.
