# Design

## Overview

The security compliance feature is implemented as a single module,
`ccai.sysman.macos.securitycompliance`, that exposes a
`MacosSecurityComplianceManager` class.  This spec implements the
security-compliance contract defined at
`codecraftai-sysman-common/specs/contracts/security-compliance/contract.md`
for macOS using Apple's open-source Security Configuration Framework
(`macos_security` project / `baseline-automation/`) as the primary
engine.

All shell-script invocation is delegated to an `ICommandRunner` and all
directory enumeration, script-existence checks, and baseline-JSON
read/write operations go through an `IFileOperator`.  No direct
filesystem or `subprocess` calls are made inside the module.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the contract
verbs are honored but the `MacosComplianceFinding` dataclass is flexible
by design and is populated from the baseline-automation JSON output.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator (Local or SSH)       │
│  - Configures aspects (optional)                            │
│  - Injects into MacosSecurityComplianceManager              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           MacosSecurityComplianceManager                    │
│  ccai.sysman.macos.securitycompliance                   │
│                                                             │
│  list_profiles()        get_profile()                       │
│  run_compliance_profile()  remediate()                      │
│  remediate_all()                                            │
│  get_baseline()         set_baseline()                      │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write /     │
│                      │         │   exists / listdir   │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
   ┌───────┴────────┐             ┌─────────┴────────┐
   ▼                ▼             ▼                  ▼
┌─────────────┐  ┌──────────────┐ ┌──────────────┐ ┌────────────┐
│LocalCommand │  │SSHCommand    │ │LocalFile     │ │SSHFile     │
│Runner       │  │Runner        │ │Operator      │ │Operator    │
└─────────────┘  └──────────────┘ └──────────────┘ └────────────┘
           │                                │
           └────────────┬───────────────────┘
                        │ optional
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `MacosSecurityComplianceManager`

**Location:** `source/packages/ccai/sysman/macos/securitycompliance.py`

| Method | Signature | Description |
|---|---|---|
| `list_profiles` | `() → list[MacosComplianceProfile]` | Scan `baselines_dir/` |
| `get_profile` | `(profile_name) → MacosComplianceProfile \| None` | Enumerate + filter |
| `run_compliance_profile` | `(profile_name) → MacosComplianceReport` | `bash run_compliance.sh` |
| `remediate` | `(finding_id) → MacosSecurityComplianceActionResult` | `bash remediate/<check>.sh` |
| `remediate_all` | `(report) → list[MacosSecurityComplianceActionResult]` | Iterate + delegate |
| `get_baseline` | `() → MacosBaselineInfo` | Read baseline JSON |
| `set_baseline` | `(spec) → MacosSecurityComplianceActionResult` | Run + remediate + write JSON |

### `MacosSecurityComplianceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, remediations pass `elevated=True` |
| `baselines_dir` | `str` | `"/usr/local/ccai/baseline-automation"` | Apple macos_security checkout |
| `baseline_path` | `str` | `"/var/lib/ccai/compliance/current_baseline.json"` | Baseline JSON |
| `results_dir` | `str` | `"/var/lib/ccai/compliance/results"` | JSON output directory |

---

## Data Models

### Enums

```python
class MacosSecurityComplianceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class MacosComplianceSeverity(Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
```

### Dataclasses

```python
@dataclass
class MacosComplianceCheck:
    check_id: str
    description: str
    check_type: str              # "script" | "profile_introspection"
    severity: MacosComplianceSeverity
    parameters: dict

@dataclass
class MacosComplianceProfile:
    name: str
    description: str
    checks: list[MacosComplianceCheck]

@dataclass
class MacosComplianceFinding:
    finding_id: str
    check_id: str
    description: str
    severity: MacosComplianceSeverity
    compliant: bool
    actual: str | None
    expected: str | None
    remediable: bool
    error: str | None

@dataclass
class MacosComplianceReport:
    profile_name: str
    started_at: datetime
    finished_at: datetime
    findings: list[MacosComplianceFinding]
    total: int
    compliant: int
    non_compliant: int
    errored: int

@dataclass
class MacosBaselineSpec:
    name: str
    settings: dict[str, str]

@dataclass
class MacosBaselineInfo:
    name: str
    settings: dict[str, str]
    source: str | None

@dataclass
class MacosSecurityComplianceActionResult:
    status: MacosSecurityComplianceActionStatus
    finding_id: str | None
    target: str | None
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class MacosSecurityComplianceError(Exception):
    """Framework-level failures."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
    def listdir(self, path: str, elevated: bool = False) -> list[str]: ...
```

---

## Command Mappings

| Operation | Command |
|---|---|
| Run profile | `bash <baselines_dir>/<profile>/run_compliance.sh --output-json <results_dir>/<profile>-<ts>.json` |
| Remediate one check | `bash <baselines_dir>/<profile>/remediate/<check_id>.sh` |
| Per-check re-check | `bash <baselines_dir>/<profile>/checks/<check_id>.sh` |

### Read file (via `IFileOperator`)

| Operation | Path |
|---|---|
| List baselines | `listdir(<baselines_dir>)` |
| Description lookup | `read_text("<baselines_dir>/<name>/description.txt")` |
| Read results JSON | `read_text("<results_dir>/<profile>-<ts>.json")` |
| Read baseline JSON | `read_text(<baseline_path>)` |
| Remediate-script existence | `exists("<baselines_dir>/<profile>/remediate/<check>.sh")` |

### Write file (via `IFileOperator`)

| Operation | Path |
|---|---|
| Write baseline JSON | `<baseline_path>` |

---

## Output Parsing

### `run_compliance.sh --output-json` parsing

The baseline-automation harness emits a JSON document with the shape:

```json
{
  "profile": "stig",
  "timestamp": "2026-04-24T13:15:02Z",
  "checks": [
    {"id": "sysprefs_loginwindow_loginwindowtext_enforce",
     "title": "Configure Login Window to Display Policy",
     "severity": "medium",
     "outcome": "pass",
     "observed": "Policy text configured",
     "expected": "Any non-empty policy"}
  ]
}
```

The parser walks `checks` and constructs one `MacosComplianceFinding` per
check:

- `finding_id` = `<profile>:<check.id>`.
- `check_id` = `check.id`.
- `description` = `check.title`.
- `severity` = map `check.severity` → `MacosComplianceSeverity` (case-insensitive).
- `compliant` = `check.outcome == "pass"`.
- `actual` = `check.observed`.
- `expected` = `check.expected`.
- `remediable` = True iff
  `<baselines_dir>/<profile>/remediate/<check.id>.sh` exists AND
  `check_type != "profile_introspection"`.
- `error` = `check.error` when `outcome` is `error`; else `None`.

Counts are derived from the findings list.

### Per-check idempotency re-check

`<baselines_dir>/<profile>/checks/<check_id>.sh` exits `0` for compliant
and non-zero for non-compliant.  The manager invokes this script once
before issuing the remediate dispatch; exit 0 triggers the `SKIPPED /
already_compliant` path.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `remediate` | Check already compliant | Per-check `check_id.sh` exit code |
| `set_baseline` | Current baseline equals requested spec | `get_baseline()` |

`run_compliance_profile` has no guard; `remediate_all` skips
`compliant=True` directly.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `MacosSecurityComplianceError` | `list_profiles` baseline-dir missing; `run_compliance_profile` unexpected exit; unknown profile; corrupt baseline JSON |

Per-check failures are captured inside the report (`compliant=False`,
`error=...`) rather than raised.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: List-Profiles Scans Baselines Dir

*For any* call to `list_profiles()`, the manager should enumerate `<baselines_dir>` via the file operator without elevation.

**Validates: Requirements 1.1**

### Property 2: List-Profiles Structural Filter

*For any* directory entry in `<baselines_dir>`, `list_profiles()` should include it only if `run_compliance.sh` AND `checks/` both exist.

**Validates: Requirements 1.1, 1.2**

### Property 3: List-Profiles Description Lookup

*For any* baseline with a `description.txt`, `list_profiles()` should use its contents as `description`; otherwise the baseline's name.

**Validates: Requirements 1.2**

### Property 4: List-Profiles Missing Dir Raises

*For any* call where `<baselines_dir>` does not exist, `list_profiles()` should raise `MacosSecurityComplianceError` with a macos_security clone hint.

**Validates: Requirements 1.3**

### Property 5: Get-Profile Returns None For Unknown

*For any* unknown profile name, `get_profile()` should return `None`.

**Validates: Requirements 2.2**

### Property 6: Run-Profile Command Construction

*For any* known profile, `run_compliance_profile()` should invoke `bash <baselines_dir>/<profile>/run_compliance.sh --output-json ...` with elevation.

**Validates: Requirements 3.1**

### Property 7: Run-Profile Accepts Exit 0 And 2

*For any* exit code of 0 or 2, the manager should parse the JSON rather than raise.

**Validates: Requirements 3.2**

### Property 8: Run-Profile Unknown Profile Raises Pre-Runner

*For any* profile name whose directory does not exist, `run_compliance_profile()` should raise without invoking the runner.

**Validates: Requirements 3.5**

### Property 9: Run-Profile Finding ID Format

*For any* check result, the produced `finding_id` should equal `<profile>:<check_id>`.

**Validates: Requirements 3.4**

### Property 10: Run-Profile Severity Mapping

*For any* check with `severity` set, the manager should produce a finding with the matching `MacosComplianceSeverity` (case-insensitive).

**Validates: Requirements 3.4**

### Property 11: Run-Profile Remediable Flag

*For any* check whose remediate script exists AND whose check_type is NOT `profile_introspection`, the finding's `remediable` should be `True`; otherwise `False`.

**Validates: Requirements 3.4, 8.3**

### Property 12: Run-Profile Per-Check Errors Captured Not Raised

*For any* check whose `outcome` is `error`, the finding should be `compliant=False` with the error text captured; the method should NOT raise.

**Validates: Requirements 3.6**

### Property 13: Run-Profile UTC Timestamps

*For any* successful report, `started_at` and `finished_at` should be UTC datetimes.

**Validates: Requirements 3.7**

### Property 14: Run-Profile Dry-Run Is A Read

*For any* run with `dry_run=True`, the manager should still execute `run_compliance.sh`.

**Validates: Requirements 3.8**

### Property 15: Remediate Parses Finding ID

*For any* `finding_id` of the form `<profile>:<check>`, `remediate()` should dispatch `bash <baselines_dir>/<profile>/remediate/<check>.sh`.

**Validates: Requirements 4.1**

### Property 16: Remediate Missing Script Short-Circuit

*For any* check whose remediate script does not exist, `remediate()` should return `FAILED` with `message="not_remediable"` without invoking the runner.

**Validates: Requirements 4.2**

### Property 17: Remediate Idempotency Via Check Script

*For any* check that already passes when its per-check `checks/<id>.sh` is invoked, `remediate()` should return `SKIPPED` with `message="already_compliant"`.

**Validates: Requirements 4.3**

### Property 18: Remediate Dry-Run

*For any* `remediate()` call with `dry_run=True`, the remediate script should not be invoked and the result should be `DRY_RUN`.

**Validates: Requirements 4.6**

### Property 19: Remediate-All Skips Compliant

*For any* finding with `compliant=True`, `remediate_all()` should not include a result for it.

**Validates: Requirements 5.2**

### Property 20: Remediate-All Emits Not-Remediable Failures

*For any* finding with `compliant=False, remediable=False`, `remediate_all()` should include a `FAILED message="not_remediable"` result.

**Validates: Requirements 5.3**

### Property 21: Remediate-All Order Preserved

*For any* call to `remediate_all(report)`, results should appear in the same order as `report.findings`.

**Validates: Requirements 5.5**

### Property 22: Get-Baseline Missing File Returns Empty

*For any* call where the baseline file does not exist, the result should be a `MacosBaselineInfo` with empty `name`, empty `settings`, and `source=None`.

**Validates: Requirements 6.2**

### Property 23: Get-Baseline Corrupt JSON Raises

*For any* malformed baseline JSON, `get_baseline()` should raise `MacosSecurityComplianceError` with `message` starting `"baseline_corrupt:"`.

**Validates: Requirements 6.4**

### Property 24: Set-Baseline Idempotency

*For any* `set_baseline(spec)` where `get_baseline()` already returns an equal spec, the result should be `SKIPPED`.

**Validates: Requirements 7.1**

### Property 25: Set-Baseline Ordering

*For any* `set_baseline(spec)` with non-matching current state, the manager should run `run_compliance_profile(spec.name)`, then `remediate_all(report)`, and finally write the baseline JSON.

**Validates: Requirements 7.2**

### Property 26: Set-Baseline Step-Failure Identification

*For any* `set_baseline(spec)` whose profile-run or any remediation step fails, the manager should return `FAILED` with `message` identifying the failing step AND MUST NOT write the baseline file.

**Validates: Requirements 7.3**

### Property 27: Set-Baseline Target Field

*For any* successful `set_baseline(spec)`, the result's `target` should equal the baseline file path.

**Validates: Requirements 7.4**

### Property 28: MDM-Profile Findings Not Remediable

*For any* finding whose `check_type` is `profile_introspection`, the manager should set `remediable=False` even when a remediate script exists.

**Validates: Requirements 8.3**

### Property 29: Unsupported Check Type In Report

*For any* check with `check_type` outside the supported set, the report should include it with `compliant=False, error="unsupported_check_type:<type>", remediable=False`; no raise.

**Validates: Requirements 8.2**

### Property 30: Elevation For Remediation And Profile Run

*For any* `run_compliance_profile`, `remediate`, or `set_baseline` invocation when `elevated=True` is configured, the runner should be called with `elevated=True`.

**Validates: Requirements 9.1**

### Property 31: No Elevation For Profile Listing And Baseline Reads

*For any* `list_profiles`, `get_profile`, or `get_baseline` call, the file operator should not be invoked with `elevated=True`.

**Validates: Requirements 9.2**

### Property 32: Dry-Run Short-Circuit For Remediation

*For any* remediation or baseline-write operation with `dry_run=True`, neither the remediate script nor the baseline JSON write should execute, and the returned status should be `DRY_RUN`.

**Validates: Requirements 4.6, 5.4, 7.5, 10.1, 10.4**

---

## Module Layout

```
source/packages/ccai/sysman/macos/
├── __init__.py
└── securitycompliance.py      ← MacosSecurityComplianceManager, config,
                                  models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/macos/
    └── test_securitycompliance.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.macos.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.macos.fileops import LocalFileOperator, LocalFileOperatorConfig
from ccai.sysman.macos.securitycompliance import (
    MacosSecurityComplianceManager,
    MacosSecurityComplianceManagerConfig,
    MacosBaselineSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = MacosSecurityComplianceManagerConfig(elevated=True, dry_run=False)
sc_mgr = MacosSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

profiles = sc_mgr.list_profiles()
for p in profiles:
    print(f"{p.name}: {p.description}")

report = sc_mgr.run_compliance_profile(profile_name="stig")
print(f"{report.compliant}/{report.total} compliant")

results = sc_mgr.remediate_all(report=report)
for r in results:
    print(r.status, r.finding_id, r.message)
```

### SSH Execution with Aspects

```python
from ccai.sysman.macos.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.macos.fileops import SSHFileOperator, SSHFileOperatorConfig
from ccai.sysman.macos.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.macos.storage import SQLiteStorage
from ccai.sysman.macos.securitycompliance import (
    MacosSecurityComplianceManager,
    MacosSecurityComplianceManagerConfig,
    MacosBaselineSpec,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="mac-node-01", username="admin"))
file_op = SSHFileOperator(SSHFileOperatorConfig(host="mac-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=1800.0)))

sc_mgr = MacosSecurityComplianceManager(
    runner=runner,
    file_operator=file_op,
    config=MacosSecurityComplianceManagerConfig(elevated=True),
)

spec = MacosBaselineSpec(name="stig", settings={"screensaver.idleTime": "1200"})
result = sc_mgr.set_baseline(spec=spec)
print(result.status, result.message)
```

### Dry-Run Mode

```python
config = MacosSecurityComplianceManagerConfig(elevated=True, dry_run=True)
sc_mgr = MacosSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

report = sc_mgr.run_compliance_profile(profile_name="stig")
results = sc_mgr.remediate_all(report=report)
for r in results:
    assert r.status == MacosSecurityComplianceActionStatus.DRY_RUN
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` and `MockFileOperator`.
- `listdir` mock responses drive `list_profiles` structural-filter
  tests.
- Baseline JSON fixtures drive `run_compliance_profile` parser tests
  including `pass`, `fail`, `error` outcomes and the
  `profile_introspection` check type.
- `baselines_dir` and `baseline_path` are overridden in tests to
  synthetic locations.
- Dry-run tests assert zero remediate-script calls and zero baseline
  JSON writes.
- A dedicated test covers the `profile_introspection` non-remediable
  override.
- A dedicated test covers `remediate_all` ordering and the
  not-remediable pass-through.
- No test requires a live macos_security checkout.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (profile names, check IDs,
  severity strings, JSON outcomes).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: security-compliance (macos), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | macOS implementation | Notes |
|---|---|---|
| `list_profiles` | Scan `<baselines_dir>` | Structural filter. |
| `get_profile` | Enumerate + filter | `None` on miss. |
| `run_compliance_profile` | `bash run_compliance.sh --output-json` | Exit 0 or 2 parses. |
| `remediate` | `bash remediate/<check>.sh` | Check-script idempotency guard. |
| `remediate_all` | Iterate + delegate | Order preserved. |
| `get_baseline` | JSON read via file operator | Missing → empty. |
| `set_baseline` | Run + remediate_all + write JSON | Fails pre-write on any step. |

---

## Extensions beyond the contract

- **Apple Security Configuration Framework as the engine.**  The
  baseline-automation project (`macos_security`) ships per-profile
  `run_compliance.sh` plus per-check `checks/<id>.sh` and
  `remediate/<id>.sh` scripts.  The manager treats each subdirectory
  as a compliance profile.
- **Baseline persistence location.**  Baseline JSON lives at
  `/var/lib/ccai/compliance/current_baseline.json`, managed via
  `IFileOperator`.
- **Results directory.**  Each profile run writes a JSON result under
  `/var/lib/ccai/compliance/results/` for auditability.

## Out of Scope

- **MDM configuration-profile installation** (`profiles install`) —
  only MDM can alter MDM profiles; the manager never issues install
  or remove commands against MDM profiles.  Findings derived from
  `profiles show` are informational only and have `remediable=False`.
- **Apple Endpoint Security framework integration** — separate
  feature; not handled here.
- **Signed / evidence-collection report output** — not implemented.
- **Attestation via Secure Enclave** — not implemented.
- **Continuous-monitoring / agent-mode** — the manager is invoked
  ad-hoc by the coordinator.
- **Remediation rollback** — the baseline-automation harness does not
  offer rollback.
- **Cross-OS parity with Ubuntu OpenSCAP or Windows `secedit`
  findings** — findings are intentionally OS-shaped by the contract.
- **Feature-gap disclosure**: security-compliance is the single most
  OS-divergent feature.  Each OS's compliance tooling produces
  findings with native shapes.  The `MacosComplianceFinding` dataclass
  is deliberately flexible to absorb that.  Cross-OS callers MUST
  treat findings as advisory and MUST NOT rely on a universal
  `check_type` enumeration beyond what the contract names.
