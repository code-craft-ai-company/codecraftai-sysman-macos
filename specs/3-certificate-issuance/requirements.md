# Requirements

## Introduction

The certificate-issuance feature provides the
`codecraftai-sysman-macos` package with the ability to *issue*,
*renew*, *revoke*, *export*, *import*, *get*, and *list* X.509
certificates on macOS hosts.  This spec implements the
certificate-issuance contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-issuance/contract.md`
for macOS.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  All system commands (`security`,
`openssl`, `cat`, `tee`, `cmp`) are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction.  No `subprocess`,
no direct `open`, and no direct `pathlib` calls are made inside this
module.

The macOS pilot uses `openssl` for CSR generation / self-signing and
the `security` command for keychain import / export / lookup.  ACME
issuance via Let's Encrypt has no first-party macOS client; for ACME
the recommendation is to install `certbot` via Homebrew and
delegate to the Ubuntu-style flow.  This pilot does NOT auto-detect
or wire `certbot` on macOS — it implements the openssl-and-keychain
pathway only.  The contract's ACME issuer enum value SHALL surface
as `FAILED` with a descriptive `detail` until the Homebrew certbot
follow-up wave lands.

CA-side revocation is also out of scope for the macOS pilot.
`revoke_certificate` removes the local keychain entry via
`security delete-certificate` (the OS-level revocation) and returns
`FAILED` with a descriptive detail when the caller asks for CA-side
revocation under a non-keychain issuer.

This feature complements — and does not duplicate — the trust-store
surface owned by `3-certificate-trust-management` (installing /
removing CA anchors).

---

## Requirements

### Requirement 1: Request a Certificate

**User Story:** As a maintenance coordinator, I want to request a
new X.509 certificate keyed by a caller-chosen alias, so that the
host can serve TLS with a configured issuer.

#### Acceptance Criteria

1. WHEN the coordinator calls `request_certificate(spec)` AND
   `spec.is_self_signed` is `True` (or unset) THE SYSTEM SHALL
   invoke `openssl req -newkey rsa:<bits> -nodes -keyout <keyout>
   -out <csrout> -subj '/CN=<common_name>'` followed by
   `openssl x509 -req -days <validity_days> -in <csrout> -signkey
   <keyout> -out <certout>`.
2. WHEN the coordinator calls `request_certificate(spec)` AND the
   spec's issuance pathway is ACME THE SYSTEM SHALL return
   `status=FAILED` with `detail` indicating that ACME is not
   supported on the macOS pilot.
3. AFTER a successful self-signed issuance THE SYSTEM SHALL invoke
   `security import <certout> -k <keychain>` to register the new
   certificate in the configured keychain.
4. WHEN the alias is already present in the configured keychain
   AND its remaining validity is greater than
   `config.renew_threshold_days` THE SYSTEM SHALL return
   `status=SKIPPED` and SHALL NOT invoke the runner.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   the would-have-been `command` populated.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
7. THE SYSTEM SHALL never place key passphrases on argv.

---

### Requirement 2: Renew a Certificate

**User Story:** As a maintenance coordinator, I want to renew an
existing certificate by alias.

#### Acceptance Criteria

1. WHEN the alias's existing certificate's remaining validity is
   greater than `config.renew_threshold_days` THE SYSTEM SHALL
   return `status=SKIPPED`.
2. WHEN the alias is not known THE SYSTEM SHALL return
   `status=FAILED` with `detail="alias not found"`.
3. WHEN renewal proceeds THE SYSTEM SHALL invoke `openssl x509
   -req -days <days> -in <csrout> -signkey <keyout> -out <certout>`
   to regenerate a self-signed cert against the existing key+CSR.
4. AFTER renewal THE SYSTEM SHALL re-import the updated certificate
   via `security import <certout> -k <keychain>`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 3: Revoke a Certificate

**User Story:** As a maintenance coordinator, I want to remove a
certificate from the local keychain.

#### Acceptance Criteria

1. WHEN the coordinator calls `revoke_certificate(alias, reason=None)`
   THE SYSTEM SHALL invoke `security delete-certificate -c <alias>
   <keychain>` to remove the entry from the configured keychain.
2. WHEN the alias is not present in the keychain THE SYSTEM SHALL
   return `status=SKIPPED`.
3. THE SYSTEM SHALL accept an optional `MacosRevocationReason` enum
   on the call; the value is recorded on the result's `detail`
   field but does NOT alter the dispatched argv (macOS does not
   distinguish reasons at the local level).
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` for the keychain mutation.

---

### Requirement 4: Get a Certificate

**User Story:** As a maintenance coordinator, I want to fetch
structured metadata for a single certificate by alias.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_certificate(alias)` THE SYSTEM
   SHALL invoke `security find-certificate -c <alias> -p
   <keychain>` to retrieve the PEM and parse it via
   `openssl x509 -text`.
2. WHEN no certificate is registered under the alias THE SYSTEM
   SHALL return `None`.
3. WHEN parsing fails THE SYSTEM SHALL raise
   `MacosCertIssuanceQueryError`.
4. WHEN the call succeeds THE SYSTEM SHALL return a
   :class:`MacosCertificateInfo` populated with `alias`, `subject`,
   `issuer`, `not_before`, `not_after`, `fingerprint`,
   `serial_number`, `key_algorithm`, `key_length`, `sans`, `path`,
   `is_self_signed`.
5. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 5: List Certificates

**User Story:** As a maintenance coordinator, I want to enumerate
every certificate in the configured keychain.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_certificates()` THE SYSTEM SHALL
   invoke `security find-certificate -a -p <keychain>` and split
   the multi-PEM stream into individual envelopes.
2. WHEN parsing of a given envelope fails THE SYSTEM SHALL skip
   that envelope and continue.
3. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 6: Export a Certificate

**User Story:** As a maintenance coordinator, I want to export a
certificate (and optionally its key) to a filesystem path.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `export_certificate(alias, path, include_private_key=False)` AND
   `include_private_key` is `False` THE SYSTEM SHALL invoke
   `security export -k <keychain> -t certs -f pemseq -o <path>`
   filtered to the requested alias.
2. WHEN `include_private_key=True` THE SYSTEM SHALL invoke
   `security export -k <keychain> -t identities -f pkcs12 -o
   <path>` (PFX/PKCS12 is the macOS native form for cert+key
   bundles).  Passphrase prompts are accepted via runner stdin
   when supplied.
3. WHEN the alias is unknown THE SYSTEM SHALL return
   `status=FAILED` with `detail="alias not found"`.
4. WHEN the destination path already exists with byte-identical
   content (compared via `cmp -s`) THE SYSTEM SHALL return
   `status=SKIPPED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
7. THE SYSTEM SHALL never include export passphrases in the
   recorded `command` field; passphrases SHALL flow on stdin via
   the `_sanitise_security_passphrases` pattern.

---

### Requirement 7: Import a Certificate

**User Story:** As a maintenance coordinator, I want to import an
externally-issued certificate under a chosen alias.

#### Acceptance Criteria

1. WHEN the coordinator calls `import_certificate(path, alias)` THE
   SYSTEM SHALL invoke `security import <path> -k <keychain>`.
2. WHEN a certificate with the same fingerprint is already present
   in the keychain THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 8: Idempotency

**User Story:** As a maintenance coordinator, I want repeated
calls with identical posture to converge to the same result
without extraneous reissuance.

#### Acceptance Criteria

1. `request_certificate` SHALL return `SKIPPED` when the alias's
   current keychain entry has remaining validity above
   `config.renew_threshold_days`.
2. `renew_certificate` SHALL return `SKIPPED` under the same
   threshold.
3. `revoke_certificate` SHALL return `SKIPPED` when the alias is
   already absent.
4. `import_certificate` SHALL return `SKIPPED` when a certificate
   with the same SHA-256 fingerprint is already present in the
   keychain.
5. `export_certificate` SHALL return `SKIPPED` when the destination
   path already byte-matches the source.

---

### Requirement 9: Elevation, Dry-Run, Error Reporting

**User Story:** Standard cross-feature semantics.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch mutating
   commands elevated.  Reads SHALL NOT elevate.
3. WHEN any dispatched command fails THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
4. THE SYSTEM SHALL NEVER include passphrase / credential bytes in
   the recorded `command` field.

---

### Requirement 10: Configuration Surface

**User Story:** As a maintenance coordinator, I want every macOS-
specific knob (binary names, keychain path, cert directory, renew
threshold) to live on a single config dataclass.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose
   `MacosCertIssuanceManagerConfig(FeatureConfig)` with fields:
   `keychain: str = "/Library/Keychains/System.keychain"`,
   `cert_dir: str = "/etc/ccai/certs"`,
   `security_cli: str = "security"`,
   `openssl_cli: str = "openssl"`,
   `renew_threshold_days: int = 30`.
2. THE SYSTEM SHALL inherit `dry_run` and `elevated` from the base
   `FeatureConfig`.

---

### Requirement 11: ACME Limitation

**User Story:** As a maintenance coordinator using macOS, I need to
know that macOS-native ACME is unsupported in the pilot.

#### Acceptance Criteria

1. WHEN the spec implies ACME issuance THE SYSTEM SHALL return
   `status=FAILED` with a descriptive detail.
2. THE SYSTEM SHALL document the recommended Homebrew `certbot`
   delegation path in the module docstring.
