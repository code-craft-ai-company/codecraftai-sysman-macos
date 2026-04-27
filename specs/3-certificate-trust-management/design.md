# Design

## Overview

The certificate-trust-management feature is implemented as a single
module, `automojo.sysman.macos.certificatetrustmanagement`, that
exposes a `MacosCertificateTrustManager` class.  This spec
implements the certificate-trust-management contract for macOS by
wrapping the `security` keychain CLI shipped with every modern macOS
release.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` (for the source PEM) and through
`security` (for keychain mutations).  Metadata parsing pipes a single
PEM envelope into `openssl x509 -text` via stdin.  The bundle
"refresh" is a no-op SUCCESS because macOS Trust Services pick up
mutations immediately.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into MacosCertificateTrustManager         │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         MacosCertificateTrustManager                        │
│  automojo.sysman.macos.certificatetrustmanagement           │
│                                                             │
│  add_trusted_cert()       remove_trusted_cert()             │
│  list_trusted_certs()     get_cert_details()                │
│  update_trust_store()  (no-op SUCCESS)                      │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosCertificateTrustManager`

**Location:**
`source/packages/automojo/sysman/macos/certificatetrustmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_trusted_cert` | `(store, path, purpose=None) → MacosCertTrustActionResult` | Validate + dispatch `security add-trusted-cert` |
| `remove_trusted_cert` | `(store, fingerprint) → MacosCertTrustActionResult` | Probe + dispatch `security delete-certificate` |
| `list_trusted_certs` | `(store) → list[MacosCertTrustInfo]` | `security find-certificate -a -p` then split + parse |
| `get_cert_details` | `(fingerprint) → MacosCertTrustInfo \| None` | `security find-certificate -Z` per keychain |
| `update_trust_store` | `() → MacosCertTrustActionResult` | No-op SUCCESS |

### `MacosCertTrustManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, system-store writes pass `elevated=True` |
| `store_path_map` | `dict[str, str \| None] \| None` | `None` | Optional override of store-name -> keychain path |
| `default_store` | `str` | `"system-ca"` | Default store for verbs that omit it |
| `security_executable` | `str` | `"security"` | Command name for keychain ops |
| `openssl_executable` | `str` | `"openssl"` | Command name for cert parsing |

The bundled defaults expose `"system-ca"` ->
`/Library/Keychains/System.keychain`, `"user-ca"` ->
`~/Library/Keychains/login.keychain-db`.

---

## Data Models

### Result Types

```python
class MacosCertTrustActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class MacosCertTrustActionResult(FeatureActionResult):
    status: MacosCertTrustActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    store: str | None
    fingerprint: str | None
    path: str | None
```

### Query Result Types

```python
@dataclass
class MacosCertTrustInfo:
    fingerprint: str        # uppercase no-colon hex
    subject: str
    issuer: str
    not_before: str
    not_after: str
    serial_number: str
    store: str
    path: str | None        # always None on macOS (keychain entries
                            # are not file-backed)
    purposes: list[str]
```

### Exceptions

```python
class MacosCertTrustQueryError(Exception):
    """
        Raised when a read operation fails for a reason other than
        the target source simply being absent (e.g. unsupported
        store name).
    """
```

`get_cert_details` returns `None` for an unknown fingerprint
rather than raising — this matches the contract's read-method
invariant.

---

## Trust-Store Mapping

| Abstract store | macOS keychain |
|----------------|----------------|
| `"system-ca"` | `/Library/Keychains/System.keychain` |
| `"user-ca"` | `~/Library/Keychains/login.keychain-db` |
| Caller override | Whatever the override map says |

`security` accepts the keychain path as an argument; tilde
expansion is done by the runner / shell.  When using a remote
runner, callers MAY override `"user-ca"` to an absolute path
because remote shells often do not expand `~` to the calling
user's home.

---

## Correctness Properties

### Property 1: PEM Validation Before Dispatch

*For any* call to `add_trusted_cert` whose source bytes do not
contain a valid PEM CERTIFICATE block, the runner SHALL NOT receive
a `security add-trusted-cert` command and the result SHALL be
`FAILED`.

**Validates: Requirements 1.4, 9.1, 9.2**

### Property 2: Fingerprint-Based Idempotency

*For any* call to `add_trusted_cert` whose certificate's SHA-256
fingerprint matches one already in the keychain (per
`security find-certificate -Z`), the runner SHALL NOT receive a
`security add-trusted-cert` command and the result SHALL be
`SKIPPED`.

**Validates: Requirements 1.5, 6.1**

### Property 3: Remove-Absent Skip

*For any* call to `remove_trusted_cert` whose fingerprint is not
present in the keychain, the runner SHALL NOT receive a
`security delete-certificate` command and the result SHALL be
`SKIPPED`.

**Validates: Requirements 2.3, 6.2**

### Property 4: Refresh No-op

*For any* call to `update_trust_store`, the runner SHALL NOT be
invoked and the result SHALL be `SUCCESS` (or `DRY_RUN` when
`dry_run=True`).

**Validates: Requirements 5.1, 5.2, 5.3**

### Property 5: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner
SHALL NOT be invoked with a `security add-trusted-cert` or
`security delete-certificate` command.  The result status SHALL be
`DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 5.2**

### Property 6: Elevation Consistency

*For any* mutating operation against `"system-ca"` with
`config.elevated=True`, the runner SHALL be invoked with
`elevated=True`.  For `"user-ca"`, the runner SHALL ALWAYS be
invoked with `elevated=False`.  For reads, the runner SHALL be
invoked with `elevated=False` regardless of config.

**Validates: Requirements 10.1, 10.2, 10.3, 10.4**

### Property 7: Multi-PEM Stream Parsing

*For any* call to `list_trusted_certs` whose runner returns a
multi-PEM stream from `security find-certificate -a -p`, the
manager SHALL split the stream on
`-----BEGIN CERTIFICATE-----` /
`-----END CERTIFICATE-----` boundaries and process every
envelope independently.  The number of returned
`MacosCertTrustInfo` objects equals the number of well-formed
envelopes in the stream.

**Validates: Requirements 3.2**

### Property 8: Read Pure Parsing

Fingerprint computation runs in pure Python on PEM bytes; only
auxiliary metadata is delegated to `openssl x509 -text`.  Reads
therefore do not depend on any specific `openssl` version's text
format for the fingerprint.

**Validates: Requirements 3.4, 4.3**

### Property 9: Fingerprint Normalisation Round-Trip

*For any* fingerprint argument supplied in colonised, non-colonised,
prefixed, or any-case form, the system SHALL normalise to uppercase
no-colon form before passing to `security`.

**Validates: Requirements 8.1, 8.2, 8.3**

### Property 10: Unsupported-Store Surface

Mutating verbs against an unsupported store name return `FAILED`
without dispatching; `list_trusted_certs` raises
`MacosCertTrustQueryError`.

**Validates: Requirements 1.2, 2.2, 3.1**

---

## Interfaces

### `ICommandRunner`

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

### `RunResult`

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

`security add-trusted-cert -d -k /Library/Keychains/System.keychain`
typically prompts for an admin password unless the runner is already
running as root.  This module passes `elevated=True` to the runner
when the user-supplied config requests it; the runner / aspect chain
is responsible for `sudo` or equivalent.  For `"user-ca"`, no
elevation is needed because the login keychain is owned by the
calling user.

---

## Command Mappings

| Operation | Command |
|-----------|---------|
| Read source PEM | `cat <path>` |
| Probe keychain for fingerprint | `security find-certificate -Z <fp> <keychain>` |
| Add trusted cert | `security add-trusted-cert -d -r trustRoot -k <keychain> <path>` |
| Remove trusted cert | `security delete-certificate -Z <fp> <keychain>` |
| List trusted certs | `security find-certificate -a -p <keychain>` |
| Parse cert metadata | `openssl x509 -noout -subject -issuer -startdate -enddate -serial -text` (PEM on stdin) |
| Refresh consolidated bundle | (none — no-op SUCCESS) |

The `-d` flag scopes `add-trusted-cert` to the system domain (admin
trust); the `-r trustRoot` flag designates the certificate as a
root anchor.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `MacosCertTrustQueryError` | `list_trusted_certs` against an unsupported store. |

All other failures surface as `MacosCertTrustActionResult` with
`status=FAILED`.

### Idempotency Guards

Before adding, the manager runs
`security find-certificate -Z <fp> <keychain>`.  Exit 0 indicates the
fingerprint is already present; the manager returns `SKIPPED` without
running `add-trusted-cert`.

Before removing, the same probe is used; exit 1 means the
fingerprint is absent and the manager returns `SKIPPED` without
running `delete-certificate`.

### PEM Validation

Source bytes are split into CERTIFICATE envelopes in pure Python.
Zero envelopes -> `FAILED`.  The first envelope's body is
base64-decoded; a decoding failure -> `FAILED`.

---

## Module Layout

```
source/packages/automojo/sysman/macos/
├── __init__.py
└── certificatetrustmanagement.py
    ├── MacosCertTrustManagerConfig
    ├── MacosCertTrustActionResult
    ├── MacosCertTrustInfo
    ├── MacosCertTrustQueryError
    ├── MacosCertificateTrustManager
    └── module-level helpers
        - _parse_pem_blocks
        - _compute_fingerprint
        - _normalize_fingerprint
        - _parse_openssl_cert_text

source/testroots/testbase/
└── automojo/tests/sysman/macos/
    └── test_certificatetrustmanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call
  and returns pre-seeded `RunResult` values keyed by argv tuple.
- Tests verify the *arguments* passed to the runner, not system
  state.
- Each public method has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- `add_trusted_cert` has additional tests for PEM-validation
  failure and elevated-write parameters.
- `update_trust_store` has a SUCCESS-no-op test that asserts the
  runner is NOT touched.
- No test requires a live keychain, live `security`, or live
  `openssl`.

### Test Coverage Map

| Property | Test |
|----------|------|
| 1 | `test_add_trusted_cert_invalid_pem_returns_failed` |
| 2 | `test_add_trusted_cert_skipped_when_fingerprint_present` |
| 3 | `test_remove_trusted_cert_skipped_when_absent` |
| 4 | `test_update_trust_store_returns_success_noop` |
| 5 | `test_add_trusted_cert_dry_run_short_circuits` |
| 6 | `test_writes_pass_elevated_true_for_system_store`, `test_user_store_writes_do_not_elevate`, `test_reads_never_elevate` |
| 7 | `test_list_trusted_certs_parses_output` |
| 8 | `test_get_cert_details_returns_full_info`, `test_list_trusted_certs_parses_output` |
| 9 | covered indirectly through fingerprint round-trip in `test_remove_trusted_cert_invokes_security` |
| 10 | `test_unsupported_store_returns_failed`, `test_unsupported_store_in_list_raises` |

---

## Usage Examples

### Local Execution (Install Internal CA)

```python
from automojo.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.macos.certificatetrustmanagement import (
    MacosCertificateTrustManager,
    MacosCertTrustManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
manager = MacosCertificateTrustManager(
    runner=runner,
    config=MacosCertTrustManagerConfig(elevated=True),
)

add_result = manager.add_trusted_cert(
    store="system-ca",
    path="/opt/pki/internal-root-ca.pem",
)
print(f"Add: {add_result.status} -> {add_result.fingerprint}")

# update_trust_store is a no-op on macOS.
manager.update_trust_store()
```

### User Keychain (No Elevation)

```python
manager = MacosCertificateTrustManager(
    runner=runner,
    config=MacosCertTrustManagerConfig(elevated=True),
)

# Even though config.elevated=True, the user-ca write does NOT
# elevate.  The login keychain is current-user-owned.
result = manager.add_trusted_cert(
    store="user-ca",
    path="/Users/me/personal-root.pem",
)
```

### SSH Execution

```python
from automojo.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig

runner = SSHCommandRunner(SSHRunnerConfig(
    host="mac.example.com",
    username="admin",
))
manager = MacosCertificateTrustManager(
    runner=runner,
    config=MacosCertTrustManagerConfig(
        elevated=True,
        store_path_map={
            "user-ca": "/Users/admin/Library/Keychains/login.keychain-db",
        },
    ),
)
manager.add_trusted_cert(
    store="user-ca",
    path="/opt/pki/corp-issuing-ca.pem",
)
```

---

## Spec-vs-Contract Drift

The contract enumerates `set_store_enabled` and `verify_certificate`
verbs.  macOS keychains do not expose enable / disable toggles —
keychains are either present (and consulted) or removed entirely;
`set_store_enabled` is therefore not implemented.  `verify_certificate`
overlaps with `automojo-credentials` and is deferred per the RHEL
spec's task 21 follow-up.  Both verbs are intentionally NOT
implemented in this wave.

The contract's `TrustPurpose` enum is not implemented as a typed
Python enum here; the `purpose` parameter on `add_trusted_cert` is a
free-form `str | None` recorded on the result.  macOS Trust Services
do support per-purpose trust settings (via `security
add-trusted-cert -p ssl|smime|...`) but the wave-1.4 pilot defaults
to root-anchor trust (`-r trustRoot`); per-purpose policy is a
follow-up.

The contract's `path` field on `CertMetadata` is always `None` on
macOS because keychain entries are stored inside the keychain
binary file, not as separate disk-resident certificates.
