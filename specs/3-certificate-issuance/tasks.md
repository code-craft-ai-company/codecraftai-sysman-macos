# Implementation Plan — Certificate Issuance (macOS)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificateissuance` module under
  `source/packages/ccai/sysman/macos/`.

- [x] 2. Define `MacosCertIssuanceActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `MacosRevocationReason` enum with the six contract
  members.

- [x] 4. Define `MacosCertIssuanceManagerConfig` dataclass:
  `dry_run`, `elevated`, `keychain`, `cert_dir`, `security_cli`,
  `openssl_cli`, `renew_threshold_days`.

- [x] 5. Define `MacosCertIssuanceActionResult` dataclass extending
  `FeatureActionResult` with `alias`, `subject`, `fingerprint`.

- [x] 6. Define `MacosCertificateInfo` dataclass.

- [x] 7. Define `MacosCertificateRequestSpec` dataclass.

- [x] 8. Define `MacosCertIssuanceQueryError` exception.

## MacosCertificateIssuanceManager — Implemented

- [x] 9. Implement `__init__`.

- [x] 10. Implement `request_certificate(spec)`:
  - Renew-threshold guard.
  - Self-signed dispatch via openssl + security import.
  - ACME pathway returns FAILED.

- [x] 11. Implement `renew_certificate(alias)`:
  - Renew-threshold guard.
  - Re-sign via openssl + re-import via security.

- [x] 12. Implement `revoke_certificate(alias, reason=None)`:
  - `security delete-certificate -c <alias> <keychain>`.
  - SKIPPED when alias absent.

- [x] 13. Implement `get_certificate(alias)`:
  - `security find-certificate -c <alias> -p <keychain>` then
    `openssl x509 -text` parse.

- [x] 14. Implement `list_certificates()`:
  - `security find-certificate -a -p <keychain>` PEM-block split.

- [x] 15. Implement `export_certificate`:
  - PEM via `security export ... -t certs -f pemseq`.
  - PKCS#12 via `security export ... -t identities -f pkcs12`.
  - Idempotent via `cmp -s`.
  - Passphrase on stdin.

- [x] 16. Implement `import_certificate(path, alias)`:
  - PEM validation in pure Python.
  - Fingerprint-based idempotency via `security find-certificate
    -Z`.

## Helpers

- [x] 17. `_parse_pem_blocks`.

- [x] 18. `_compute_fingerprint` (pure-Python SHA-256).

- [x] 19. `_parse_openssl_x509_text`.

- [x] 20. `_days_until`.

- [x] 21. `_format_subject`.

- [x] 22. `_sanitise_security_passphrases` — redact passphrase
  bytes from a recorded command argv (matches the Windows pilot's
  `_sanitise_script_passwords` pattern).

## Tests — Implemented

- [x] 23. `test_request_certificate_invokes_openssl_with_subject`.

- [x] 24. `test_request_certificate_skipped_when_present_and_not_near_expiry`.

- [x] 25. `test_request_certificate_dry_run_short_circuits`.

- [x] 26. `test_request_certificate_self_signed_uses_openssl`.

- [x] 27. `test_renew_certificate_invokes_openssl`.

- [x] 28. `test_renew_certificate_skipped_when_not_near_expiry`.

- [x] 29. `test_revoke_certificate_invokes_security`.

- [x] 30. `test_revoke_certificate_with_reason`.

- [x] 31. `test_get_certificate_parses_x509_output`.

- [x] 32. `test_list_certificates_returns_all`.

- [x] 33. `test_export_certificate_writes_to_path`.

- [x] 34. `test_export_with_private_key_uses_pem`.

- [x] 35. `test_import_certificate_skipped_when_fingerprint_present`.

- [x] 36. `test_passphrase_redacted_in_recorded_command`.

- [x] 37. `test_writes_elevate`.

- [x] 38. `test_reads_never_elevate`.

- [x] 39. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 40. ACME issuance via Homebrew `certbot` — auto-detect or
  wire through a delegated dispatcher in a follow-up wave.

- [ ] 41. CA-side revocation — currently the pilot only removes the
  local keychain entry.

- [ ] 42. Keychain unlock automation — `security unlock-keychain`
  is not invoked by the manager; callers handle prompts via
  `codecraftai-credentials` outside this manager.

## Follow-ups

- [ ] 43. **Smartcard / Secure Enclave keys** — add support for
  hardware-backed keys via `SecKeyGenerateRandomKey` on the macOS
  Security framework.

- [ ] 44. **Distribution Profile / Provisioning Profile import**
  — Apple-specific profile material lives outside the keychain
  and would need its own verb.
