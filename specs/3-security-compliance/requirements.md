# Requirements

## Introduction

The security compliance feature provides the `codecraftai-sysman-macos`
package with the ability to run compliance profiles, iterate their findings,
and remediate individual findings on macOS systems.  This spec implements
the security-compliance contract defined at
`codecraftai-sysman-common/specs/contracts/security-compliance/contract.md`
for macOS using Apple's open-source **Security Configuration Framework**
(a.k.a. the `macos_security` project — `baseline-automation/<profile>/`
shell scripts) as the primary compliance engine, plus `csrutil`,
`spctl`, `socketfilterfw`, and `fdesetup` for ad-hoc system checks.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  All system
commands are executed exclusively through the `codecraftai-interfaces`
command-runner abstraction, and all baseline-metadata mutations are
executed through the `codecraftai-interfaces` file-operator abstraction.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the verb set
is honored across OSes but each OS's compliance tooling produces findings
with OS-native shapes.  This macOS spec adopts Apple's
`baseline-automation/<profile>/` scripts as the primary engine; each
baseline ships a `run_compliance.sh` (produces pass/fail per check) and
per-check `remediate.sh` files.  The manager populates the contract's
flexible `MacosComplianceFinding` dataclass from these script outputs.

MDM-installed configuration profiles (visible via `profiles show -type
configuration`) are OUT OF SCOPE for evaluation and remediation — the
manager treats them as informational state only, not as a source of
findings.

---

## Requirements

### Requirement 1: List Available Profiles

**User Story:** As a maintenance coordinator, I want to enumerate every
macOS baseline the manager knows about, so that I can choose one to
evaluate.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_profiles()` THE SYSTEM SHALL scan
   `<baselines_dir>` (default `/usr/local/ccai/baseline-automation/`)
   via the `IFileOperator` and enumerate every subdirectory that
   contains both `run_compliance.sh` and a `checks/` subdirectory.
2. WHEN the directory is enumerated THE SYSTEM SHALL return a
   `list[MacosComplianceProfile]` with one entry per baseline:
   `name`=subdirectory name, `description` read from
   `<subdir>/description.txt` when present else the subdirectory name,
   `checks=[]`.
3. WHEN `<baselines_dir>` does not exist THE SYSTEM SHALL raise a
   `MacosSecurityComplianceError` with a `message` recommending the
   user clone `https://github.com/usnistgov/macos_security` (or the
   internal mirror) into that path.
4. WHEN the coordinator has configured `baselines_dir` in the config
   THE SYSTEM SHALL use that path instead of the default.
5. THE SYSTEM SHALL recognize the baseline names `stig`, `cis_lvl1`,
   `cis_lvl2`, `800-53`, and `all_rules` as the primary profile set
   but MUST NOT reject any subdirectory that meets the structural
   requirement — additional baselines are permitted.

---

### Requirement 2: Get a Single Profile

**User Story:** As a maintenance coordinator, I want to resolve a profile
by name, so that I can inspect its description.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_profile(profile_name)` THE SYSTEM
   SHALL invoke `list_profiles()` internally and return the matching
   entry.
2. WHEN no profile matches THE SYSTEM SHALL return `None`.
3. WHEN `list_profiles()` raises THE SYSTEM SHALL propagate the
   `MacosSecurityComplianceError`.

---

### Requirement 3: Run a Compliance Profile

**User Story:** As a maintenance coordinator, I want to evaluate a profile
end-to-end and receive a structured report, so that I can act on each
finding.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_compliance_profile(profile_name)` THE
   SYSTEM SHALL invoke `bash <baselines_dir>/<profile_name>/run_compliance.sh
   --output-json <results_dir>/<profile>-<ts>.json` via the command
   runner with elevation.
2. WHEN the command exits 0 (fully compliant) OR a baseline-documented
   non-zero "some non-compliance" code (typically 2) THE SYSTEM SHALL
   read the JSON results via the `IFileOperator` and parse them into
   a `MacosComplianceReport`.
3. WHEN the command exits with any other unexpected code THE SYSTEM
   SHALL raise a `MacosSecurityComplianceError`.
4. WHEN the report is parsed each finding SHALL populate
   `finding_id`=`<profile>:<check_id>`, `check_id`=the check JSON's
   id field, `description`=the check's title, `severity` mapped from
   the check's severity string, `compliant` from the outcome field,
   `remediable` from the presence of
   `<baselines_dir>/<profile>/remediate/<check_id>.sh`, and `error`
   from any exception field in the check object.
5. WHEN the profile directory does not exist THE SYSTEM SHALL raise
   `MacosSecurityComplianceError` with
   `message="unknown_profile: <name>"` before invoking the runner.
6. WHEN individual check scripts fail inside the baseline harness THE
   SYSTEM SHALL capture them in the report with `compliant=False` and
   `error=<harness error text>` — such failures MUST NOT raise.
7. WHEN `started_at` and `finished_at` are set THE SYSTEM SHALL use
   UTC datetimes.
8. WHEN dry-run mode is active THE SYSTEM SHALL still execute the
   profile run — compliance evaluation is a read.

---

### Requirement 4: Remediate a Finding

**User Story:** As a maintenance coordinator, I want to remediate a
specific finding identified in a report, so that the system comes into
compliance.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate(finding_id)` THE SYSTEM SHALL
   split the finding ID on `:` into `<profile>:<check_id>` and
   invoke `bash <baselines_dir>/<profile>/remediate/<check_id>.sh`
   via the command runner with elevation.
2. WHEN the remediate script does not exist THE SYSTEM SHALL return a
   `MacosSecurityComplianceActionResult` with status `FAILED` and
   `message="not_remediable"` without invoking the runner.
3. WHEN the system is ALREADY compliant for the check THE SYSTEM
   SHALL return a `MacosSecurityComplianceActionResult` with status
   `SKIPPED` and `message="already_compliant"` — idempotency is
   evaluated by running the per-check check script
   (`<baselines_dir>/<profile>/checks/<check_id>.sh`) first.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `MacosSecurityComplianceActionResult` with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a
   `MacosSecurityComplianceActionResult` with status `FAILED`, the
   exit code, captured stderr, and the `command`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   remediate script and SHALL return a result with status `DRY_RUN`.

---

### Requirement 5: Batch Remediate

**User Story:** As a maintenance coordinator, I want to apply every
remediable finding in a report in one call.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate_all(report)` THE SYSTEM SHALL
   iterate `report.findings` in list order and SHALL invoke
   `remediate(finding_id)` for every finding whose `remediable=True`
   AND `compliant=False`.
2. WHEN a finding has `compliant=True` THE SYSTEM SHALL skip it
   silently (no result returned).
3. WHEN a finding has `remediable=False` AND `compliant=False` THE
   SYSTEM SHALL include a result with status `FAILED` and
   `message="not_remediable"`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `DRY_RUN` for
   every attempted remediation.
5. THE SYSTEM SHALL return one result per attempted remediation in
   the order findings were processed.

---

### Requirement 6: Get Baseline

**User Story:** As a maintenance coordinator, I want to read the most
recently applied baseline metadata so that audits can report on active
policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_baseline()` THE SYSTEM SHALL invoke
   `IFileOperator.read_text('/var/lib/ccai/compliance/current_baseline.json')`.
2. WHEN the file does not exist THE SYSTEM SHALL return a
   `MacosBaselineInfo` with empty `name`, empty `settings`, and
   `source=None`.
3. WHEN the file exists THE SYSTEM SHALL parse JSON with keys `name`
   and `settings` and SHALL return a `MacosBaselineInfo` populated
   from those fields with `source=<file path>`.
4. WHEN the JSON is malformed THE SYSTEM SHALL raise a
   `MacosSecurityComplianceError` with
   `message="baseline_corrupt: <reason>"`.

---

### Requirement 7: Set Baseline

**User Story:** As a maintenance coordinator, I want to persist the active
baseline and apply it, so that a known policy is enforced and recorded.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_baseline(spec)` AND `get_baseline()`
   already returns a baseline equal to `spec` THE SYSTEM SHALL return
   a result with status `SKIPPED`.
2. WHEN the baselines differ THE SYSTEM SHALL first run
   `run_compliance_profile(spec.name)`, then `remediate_all(report)`,
   and finally write the updated baseline JSON via the
   `IFileOperator`.
3. WHEN the profile run OR any remediation fails THE SYSTEM SHALL
   return `FAILED` with a `message` identifying the failing step,
   AND SHALL NOT write the baseline file.
4. WHEN the baseline write succeeds THE SYSTEM SHALL return
   `SUCCESS` with `target=/var/lib/ccai/compliance/current_baseline.json`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any
   mutation and SHALL return `DRY_RUN`.

---

### Requirement 8: Check-Type Support

**User Story:** As a maintenance coordinator, I want to know which check
types the manager understands.

#### Acceptance Criteria

1. THE SYSTEM SHALL support the `check_type` discriminators: `script`
   (the baseline-automation default) and `profile_introspection`
   (derived from `profiles show` output when the baseline specifies
   an MDM-profile check).
2. WHEN `run_compliance_profile` encounters a check with a
   `check_type` outside this set THE SYSTEM SHALL include the check
   in the report with `compliant=False`, `error="unsupported_check_type:
   <type>"`, `remediable=False` — no raise.
3. THE SYSTEM SHALL NOT remediate `profile_introspection` findings —
   only MDM can alter MDM profiles; such findings are surfaced with
   `remediable=False` even when a remediate script exists.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all compliance
evaluations and remediations to run with the privileges required by the
baseline scripts.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL
   pass `elevated=True` to the command runner for
   `run_compliance_profile`, `remediate`, and `set_baseline`.
2. WHEN invoking `list_profiles` / `get_profile` / `get_baseline` THE
   SYSTEM SHALL NOT pass `elevated=True` — those reads target
   `/usr/local/ccai/baseline-automation/` and
   `/var/lib/ccai/compliance/` which are readable without
   elevation when configured correctly.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN elevation is not configured THE SYSTEM SHALL default to no
   elevation and allow the runner to surface the resulting permission
   error.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode that
simulates remediation without mutating the system.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL
   NOT invoke any remediate script or baseline JSON write.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have been
   executed.
3. WHEN dry-run is active THE SYSTEM SHALL still execute
   `run_compliance_profile` (reads).
4. WHEN dry-run is active THE SYSTEM SHALL return status `DRY_RUN` on
   every result returned from `remediate`, `remediate_all`, or
   `set_baseline`.
