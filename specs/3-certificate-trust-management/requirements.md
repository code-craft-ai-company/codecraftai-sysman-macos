# Requirements

## Introduction

The certificate-trust-management feature provides the
`codecraftai-sysman-macos` package with the ability to manage the
host's CA trust posture on macOS systems by wrapping the
`security` keychain CLI: installing and removing trust anchors,
listing the current trust set, retrieving structured metadata for an
individual certificate by SHA-256 fingerprint, and (no-op)
"refreshing" the consolidated trust bundle.  This spec implements
the certificate-trust-management contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-trust-management/contract.md`
for macOS.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  The feature does not decide *what*
trust posture the host enforces — it provides the capability to
*apply* posture reliably, idempotently, and with full auditability.
All system commands (`security`, `openssl`, `cat`) are executed
exclusively through the `codecraftai-interfaces` command-runner
abstraction.  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside this module.

The macOS keychain layer is the trust store: `"system-ca"` maps to
`/Library/Keychains/System.keychain` and `"user-ca"` maps to
`~/Library/Keychains/login.keychain-db`.  Trust changes made via
`security add-trusted-cert` and `security delete-certificate` are
picked up immediately by Trust Services on the next
certificate-evaluation call; there is no equivalent of
`update-ca-certificates`.

---

## Requirements

### Requirement 1: Add a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to install a
PEM-encoded certificate as a trust anchor in a named keychain, so
that the host trusts certificates issued under that CA.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `add_trusted_cert(store, path, purpose=None)` THE SYSTEM SHALL
   resolve the abstract `store` name to a keychain path via the
   configured `store_path_map` (defaulting to
   `"system-ca"` -> `/Library/Keychains/System.keychain`,
   `"user-ca"` -> `~/Library/Keychains/login.keychain-db`).
2. WHEN the resolved keychain is `None` (i.e. the store is not
   supported) THE SYSTEM SHALL return
   `MacosCertTrustActionResult` with `status=FAILED` and a `detail`
   describing the unsupported store, and SHALL NOT invoke the
   runner.
3. WHEN the source `path` cannot be read THE SYSTEM SHALL return
   `status=FAILED` with `detail="source path does not exist"`.
4. WHEN the source bytes do not contain a `-----BEGIN CERTIFICATE-----`
   block THE SYSTEM SHALL return `status=FAILED` and SHALL NOT
   invoke `security add-trusted-cert`.
5. WHEN the SHA-256 fingerprint of the supplied certificate matches
   one already present in the keychain (probed via
   `security find-certificate -Z <fingerprint> <keychain>`) THE
   SYSTEM SHALL return `status=SKIPPED` with `fingerprint`
   populated.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   `command=["security", "add-trusted-cert", ...]` and SHALL NOT
   invoke the runner.
7. WHEN `config.elevated=True` and the store is `"system-ca"` THE
   SYSTEM SHALL pass `elevated=True` for the
   `security add-trusted-cert` invocation.
8. WHEN the store is `"user-ca"` THE SYSTEM SHALL pass
   `elevated=False` regardless of `config.elevated` (the login
   keychain is current-user-owned).
9. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`
   with `fingerprint`, `path`, `command`, `exit_code`, `stdout`,
   `stderr` populated.

---

### Requirement 2: Remove a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to remove a
trust anchor by its SHA-256 fingerprint, so that revocation is
unambiguous.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `remove_trusted_cert(store, fingerprint)` THE SYSTEM SHALL
   resolve the abstract `store` to a keychain path.
2. WHEN the store is unsupported THE SYSTEM SHALL return
   `status=FAILED`.
3. WHEN no certificate with the requested fingerprint is present
   THE SYSTEM SHALL return `status=SKIPPED` and SHALL NOT invoke
   `security delete-certificate`.
4. WHEN a matching certificate is present THE SYSTEM SHALL run
   `security delete-certificate -Z <fingerprint> <keychain>`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` and the store is `"system-ca"` THE
   SYSTEM SHALL pass `elevated=True`.
7. WHEN the store is `"user-ca"` THE SYSTEM SHALL pass
   `elevated=False`.

---

### Requirement 3: List Trusted Certificates

**User Story:** As a maintenance coordinator, I want to enumerate
the trusted certificates in a keychain, so that I can audit the
declared trust posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_trusted_certs(store)` and the
   store is unsupported THE SYSTEM SHALL raise
   `MacosCertTrustQueryError`.
2. WHEN the store is supported THE SYSTEM SHALL invoke
   `security find-certificate -a -p <keychain>` and split the
   resulting multi-PEM stream into individual envelopes.
3. WHEN the runner returns a non-zero exit THE SYSTEM SHALL return
   an empty list (the keychain may be empty or unreadable).
4. WHEN parsing a PEM envelope succeeds THE SYSTEM SHALL pipe the
   single envelope through `openssl x509 -text` (via stdin) to
   parse subject / issuer / validity / serial / EKU.
5. THE SYSTEM SHALL return a `list[MacosCertTrustInfo]` with each
   entry's `store` set to the supplied store name.
6. WHEN `list_trusted_certs` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 4: Get Certificate Details

**User Story:** As a maintenance coordinator, I want to retrieve
structured metadata for a single certificate by its fingerprint,
so that I can make decisions about replacement or expiration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cert_details(fingerprint)` THE
   SYSTEM SHALL search every supported keychain via
   `security find-certificate -Z <fingerprint> -p <keychain>`.
2. WHEN no keychain returns a match THE SYSTEM SHALL return `None`.
3. WHEN a match is found THE SYSTEM SHALL pipe the resulting PEM
   through `openssl x509 -text` and return a populated
   `MacosCertTrustInfo`.
4. THE SYSTEM SHALL accept the fingerprint argument in colonised or
   non-colonised form, case-insensitive, and normalise to
   uppercase no-colon (the macOS native form) before invoking
   `security`.
5. WHEN `get_cert_details` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 5: Update the Trust Store (No-op)

**User Story:** As a maintenance coordinator, I want a uniform
`update_trust_store` verb that callers can invoke after
add / remove sequences regardless of OS.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_trust_store()` THE SYSTEM
   SHALL return `status=SUCCESS` with `command=None` and SHALL NOT
   invoke the runner.  (macOS Trust Services pick up keychain
   mutations immediately.)
2. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   with `command=None` and SHALL NOT invoke the runner.
3. THE SYSTEM SHALL never return `FAILED` or `SKIPPED` from
   `update_trust_store` on macOS.

---

### Requirement 6: Idempotency Across Add / Remove

#### Acceptance Criteria

1. WHEN `add_trusted_cert` is called with a fingerprint already
   present in the target keychain THE SYSTEM SHALL return
   `status=SKIPPED` and SHALL NOT invoke
   `security add-trusted-cert`.
2. WHEN `remove_trusted_cert` is called with a fingerprint not
   present THE SYSTEM SHALL return `status=SKIPPED` and SHALL NOT
   invoke `security delete-certificate`.

---

### Requirement 7: Trust-Store Mapping

#### Acceptance Criteria

1. THE SYSTEM SHALL default `"system-ca"` to
   `/Library/Keychains/System.keychain`.
2. THE SYSTEM SHALL default `"user-ca"` to
   `~/Library/Keychains/login.keychain-db`.
3. THE SYSTEM SHALL accept a caller-supplied `store_path_map`
   override that adds or replaces store names.
4. WHEN the override maps a name to `None` THE SYSTEM SHALL treat
   that name as unsupported.

---

### Requirement 8: Fingerprint Normalisation

#### Acceptance Criteria

1. THE SYSTEM SHALL accept fingerprints in colonised
   (`"AB:CD:..."`), non-colonised (`"abcd..."`), and prefixed
   (`"SHA256:..."` / `"SHA256 Fingerprint=..."`) forms.
2. THE SYSTEM SHALL normalise input to uppercase no-colon form
   before passing to `security`.
3. THE SYSTEM SHALL emit fingerprints on result objects in
   uppercase no-colon form.

---

### Requirement 9: PEM Validation

#### Acceptance Criteria

1. WHEN the source bytes do not contain a CERTIFICATE envelope
   THE SYSTEM SHALL return `status=FAILED` and dispatch zero
   `security` commands.
2. WHEN the PEM body fails to base64-decode THE SYSTEM SHALL
   return `status=FAILED`.
3. WHEN the source contains multiple CERTIFICATE blocks THE SYSTEM
   SHALL fingerprint the first block; all blocks are forwarded to
   `security add-trusted-cert` verbatim via the source path.

---

### Requirement 10: Elevation Policy

#### Acceptance Criteria

1. WHEN `config.elevated=True` and the targeted store is
   `"system-ca"` THE SYSTEM SHALL pass `elevated=True` for write
   verbs.
2. WHEN the targeted store is `"user-ca"` THE SYSTEM SHALL never
   elevate.
3. WHEN performing reads THE SYSTEM SHALL pass `elevated=False`
   regardless of config.
4. WHEN `config.elevated=False` THE SYSTEM SHALL pass
   `elevated=False` on every write path.
5. THE SYSTEM SHALL NOT prompt for or stash credentials; the
   runner / aspect chain is responsible for elevation.
