# Design

## Overview

The certificate-issuance feature is implemented as a single module,
`automojo.sysman.macos.certificateissuance`, that exposes a
`MacosCertificateIssuanceManager` class.  This spec implements the
certificate-issuance contract defined at
`automojo-sysman-common/specs/contracts/certificate-issuance/contract.md`
for macOS using the `security` keychain CLI plus `openssl` for
CSR generation and self-signing.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` / `tee` / `cmp`, certificate metadata
routes through `openssl x509 -text`, and keychain mutations route
through `security`.

The feature works identically whether commands execute locally
(via `LocalCommandRunner`) or remotely over SSH.

**See Also:**

- [Certificate Trust Management](../3-certificate-trust-management/design.md)
  — owns the *trust* side of the surface (CA anchors).  This spec
  owns the *issuance* side.
- [Command Runner Architecture](../1-command-execution/design.md)
  — the underlying execution model.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosCertificateIssuanceManager      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│       MacosCertificateIssuanceManager                       │
│  automojo.sysman.macos.certificateissuance                  │
│                                                             │
│  request_certificate()    renew_certificate()               │
│  revoke_certificate()     get_certificate()                 │
│  list_certificates()      export_certificate()              │
│  import_certificate()                                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosCertificateIssuanceManager`

**Location:**
`source/packages/automojo/sysman/macos/certificateissuance.py`

| Method | Signature | Description |
|---|---|---|
| `request_certificate` | `(spec) → MacosCertIssuanceActionResult` | Self-sign via openssl + import to keychain |
| `renew_certificate` | `(alias) → MacosCertIssuanceActionResult` | Re-sign existing CSR + re-import |
| `revoke_certificate` | `(alias, reason=None) → MacosCertIssuanceActionResult` | Remove from keychain |
| `get_certificate` | `(alias) → MacosCertificateInfo \| None` | `security find-certificate` + parse |
| `list_certificates` | `() → list[MacosCertificateInfo]` | Enumerate keychain |
| `export_certificate` | `(alias, path, include_private_key=False) → MacosCertIssuanceActionResult` | `security export` to PEM (cert) or PKCS#12 (cert+key) |
| `import_certificate` | `(path, alias) → MacosCertIssuanceActionResult` | `security import` |

### `MacosCertIssuanceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit. |
| `elevated` | `bool` | `False` | When `True`, mutating commands pass `elevated=True`. |
| `keychain` | `str` | `"/Library/Keychains/System.keychain"` | Target keychain. |
| `cert_dir` | `str` | `"/etc/automojo/certs"` | Where openssl-side material (key, csr, cert) is staged. |
| `security_cli` | `str` | `"security"` | Command name for `security`. |
| `openssl_cli` | `str` | `"openssl"` | Command name for `openssl`. |
| `renew_threshold_days` | `int` | `30` | Skip threshold for request / renew. |

---

## Data Models

```python
class MacosCertIssuanceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class MacosRevocationReason(Enum):
    UNSPECIFIED            = "unspecified"
    KEY_COMPROMISE         = "keycompromise"
    CA_COMPROMISE          = "cacompromise"
    AFFILIATION_CHANGED    = "affiliationchanged"
    SUPERSEDED             = "superseded"
    CESSATION_OF_OPERATION = "cessationofoperation"


@dataclass
class MacosCertIssuanceActionResult(FeatureActionResult):
    alias: str | None
    subject: str | None
    fingerprint: str | None


@dataclass
class MacosCertificateInfo:
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str
    serial_number: str
    key_algorithm: str | None
    key_length: int | None
    sans: list[str]
    path: str | None
    is_self_signed: bool


@dataclass
class MacosCertificateRequestSpec:
    alias: str
    common_name: str
    email: str | None = None
    sans: list[str] = []
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = True
    webroot: str | None = None


@dataclass
class MacosCertIssuanceManagerConfig(FeatureConfig):
    keychain: str = "/Library/Keychains/System.keychain"
    cert_dir: str = "/etc/automojo/certs"
    security_cli: str = "security"
    openssl_cli: str = "openssl"
    renew_threshold_days: int = 30


class MacosCertIssuanceQueryError(Exception):
    """Raised when a read fails unexpectedly."""
```

---

## Verb / Tool Mapping

| Verb | Command |
|------|---------|
| `request_certificate` (self-signed) | `openssl req -newkey rsa:<bits> -nodes -keyout <k> -out <csr> -subj '/CN=<cn>'` then `openssl x509 -req -days <n> -in <csr> -signkey <k> -out <cert>` then `security import <cert> -k <keychain>` |
| `request_certificate` (ACME) | FAILED — Homebrew `certbot` delegation deferred to follow-up wave |
| `renew_certificate` | `openssl x509 -req -days <n> -in <csr> -signkey <k> -out <cert>` then `security import <cert> -k <keychain>` |
| `revoke_certificate` | `security delete-certificate -c <alias> <keychain>` |
| `get_certificate` | `security find-certificate -c <alias> -p <keychain>` then `openssl x509 -text` parse |
| `list_certificates` | `security find-certificate -a -p <keychain>` then PEM-block split |
| `export_certificate` (cert only) | `security export -k <keychain> -t certs -f pemseq -o <path>` |
| `export_certificate` (with key) | `security export -k <keychain> -t identities -f pkcs12 -o <path>` |
| `import_certificate` | `security import <path> -k <keychain>` |

The export-with-private-key path produces a PKCS#12 (PFX-equivalent)
bundle, which is the macOS-native cert+key envelope.  The export
passphrase, when supplied, flows through runner `stdin` via the
`_sanitise_security_passphrases` helper so no secret bytes appear on
argv or in the recorded `command` field.

---

## Correctness Properties

### Property 1: Self-Sign Order

*For any* call to `request_certificate(spec)` with
`spec.is_self_signed=True`, the dispatched commands SHALL be (in
order): `openssl req`, `openssl x509 -req -signkey`, `security
import`.

**Validates: Requirements 1.1, 1.3**

### Property 2: ACME Limitation

*For any* call to `request_certificate` whose pathway implies ACME
issuance, the result SHALL be `FAILED` with a descriptive `detail`,
and the runner SHALL NOT receive any issuance-related command.

**Validates: Requirements 1.2, 11.1**

### Property 3: Renew-Threshold Skip

*For any* call to `request_certificate` or `renew_certificate`
whose existing keychain entry has remaining validity above
`config.renew_threshold_days`, no issuance command SHALL be
dispatched.

**Validates: Requirements 1.4, 2.1, 8.1, 8.2**

### Property 4: Fingerprint-Based Idempotency for Import

*For any* call to `import_certificate` whose source fingerprint
matches one already present in the keychain, the runner SHALL NOT
receive a `security import` command and the result SHALL be
`SKIPPED`.

**Validates: Requirements 7.2, 8.4**

### Property 5: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner
SHALL NOT be invoked with a mutating command.  The result status
SHALL be `DRY_RUN`.

**Validates: Requirements 1.5, 2.5, 3.4, 6.5, 7.3, 9.1**

### Property 6: Elevation Consistency

*For any* mutating verb with `config.elevated=True`, the runner
SHALL be invoked with `elevated=True`.  *For any* read verb,
`elevated=False` SHALL be passed regardless of config.

**Validates: Requirements 1.6, 2.6, 3.5, 6.6, 7.4, 9.2**

### Property 7: Credential Sanitisation

*For any* verb that accepts an export passphrase, the bytes SHALL
flow on `stdin` only.  The recorded `command` field SHALL never
contain the secret.

**Validates: Requirements 6.7, 9.4**

### Property 8: Export Byte-Match Skip

*For any* call to `export_certificate` whose destination path
already byte-matches the source, the runner SHALL NOT receive a
`security export` command and the result SHALL be `SKIPPED`.

**Validates: Requirement 8.5**

### Property 9: Read Pure Parsing

*For any* call to `get_certificate` or `list_certificates`,
fingerprint computation SHALL be performed in pure Python on the
PEM bytes; only the auxiliary metadata SHALL be delegated to
`openssl x509 -text`.  Reads SHALL NOT elevate.

**Validates: Requirements 4.5, 5.3**

### Property 10: Failure Capture

*For any* dispatched command that exits non-zero, the result
SHALL be `FAILED` with `exit_code`, `stdout`, `stderr`, and
`command` populated.

**Validates: Requirement 9.3**

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

---

## Command Mappings

| Operation | Command |
|-----------|---------|
| CSR generate | `openssl req -newkey rsa:<bits> -nodes -keyout <k> -out <csr> -subj '/CN=<cn>'` |
| Self-sign | `openssl x509 -req -days <n> -in <csr> -signkey <k> -out <cert>` |
| Inspect | `openssl x509 -in <p> -noout -text -fingerprint -sha256 -dates -subject -issuer -serial` |
| Find by alias | `security find-certificate -c <alias> -p <keychain>` |
| Find all | `security find-certificate -a -p <keychain>` |
| Import | `security import <path> -k <keychain>` |
| Delete | `security delete-certificate -c <alias> <keychain>` |
| Export PEM | `security export -k <keychain> -t certs -f pemseq -o <path>` |
| Export PKCS#12 | `security export -k <keychain> -t identities -f pkcs12 -o <path>` |
| Read source | `cat <path>` |
| Write | `tee <dest>` (content on stdin) |
| Compare | `cmp -s <src> <dest>` |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `MacosCertIssuanceQueryError` | A read fails unexpectedly. |

All other failures surface as a `MacosCertIssuanceActionResult`
with `status=FAILED`.

### Idempotency Guards

Before requesting / renewing, the manager parses the existing
keychain entry's `notAfter` (via `openssl x509 -enddate` against the
PEM produced by `security find-certificate`).  Above threshold ->
`SKIPPED`.

Before importing, the manager fingerprints the source PEM and
compares against the keychain via `security find-certificate -Z
<fp>`.  Match -> `SKIPPED`.

Before exporting, the manager runs `cmp -s` between source and
destination paths.  Match -> `SKIPPED`.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── certificateissuance.py
    ├── MacosCertIssuanceActionStatus       (alias to ActionStatus)
    ├── MacosRevocationReason               (enum)
    ├── MacosCertIssuanceActionResult       (dataclass)
    ├── MacosCertificateInfo                (dataclass)
    ├── MacosCertificateRequestSpec         (dataclass)
    ├── MacosCertIssuanceManagerConfig      (dataclass)
    ├── MacosCertIssuanceQueryError         (exception)
    ├── MacosCertificateIssuanceManager     (manager class)
    └── module-level helpers
        - _parse_pem_blocks
        - _compute_fingerprint
        - _parse_openssl_x509_text
        - _days_until
        - _format_subject
        - _sanitise_security_passphrases

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_certificateissuance.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run`
  call and returns pre-seeded `RunResult` values keyed by argv
  tuple.
- Tests verify the *arguments* passed to the runner, not system
  state.
- Each public verb has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- A passphrase-redaction test covers the export-with-key flow.
- No test requires root access, live `security`, live `openssl`,
  or a live keychain.

### Test Coverage Map

| Property | Test name |
|----------|-----------|
| 1 | `test_request_certificate_self_signed_uses_openssl` |
| 3 | `test_request_certificate_skipped_when_present_and_not_near_expiry`, `test_renew_certificate_skipped_when_not_near_expiry` |
| 4 | `test_import_certificate_skipped_when_fingerprint_present` |
| 5 | `test_request_certificate_dry_run_short_circuits` |
| 6 | `test_writes_elevate`, `test_reads_never_elevate` |
| 7 | `test_passphrase_redacted_in_recorded_command` |
| 8 | covered by `test_export_certificate_writes_to_path` skip branch |
| 9 | `test_get_certificate_parses_x509_output`, `test_list_certificates_returns_all` |
| 10 | `test_failed_command_surfaces_stderr` |

---

## Spec-vs-Contract Drift

The contract enumerates `Credential` parameters on every write
verb.  The macOS pilot does not yet thread `automojo-credentials`
through the public surface — keychain unlock prompts, when needed,
flow as separate `security unlock-keychain` calls outside this
manager.  Threading is deferred to the cross-package follow-up.

The contract also enumerates ACME issuance.  macOS lacks a
first-party ACME client; the recommended path is to install
`certbot` via Homebrew and delegate, but auto-detection /
delegation is out of scope for the pilot.  Calls that imply ACME
return `FAILED` with a descriptive detail.

CA-side revocation is similarly out of scope; the pilot's
`revoke_certificate` removes the local keychain entry only.
