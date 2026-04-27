# Tasks

## Pilot Scope

The pilot exposes seven verbs (`add_forwarder`,
`remove_forwarder`, `list_forwarders`, `enable_forwarder`,
`disable_forwarder`, `test_forwarder`, `get_forwarder_status`)
through a single `MacosRemoteLogForwardingManager`.  Every verb
is dispatched through the injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` describing the ten requirements
      that drive the implementation, including the prominent
      unified-logging limitation.
- [x] Author `design.md` describing the architecture, the
      sentinel-bracketed block layout, the unified-logging
      limitation, the recommended third-party shippers, and the
      documented contract drift.
- [x] Author `remotelogforwarding.py` with:
    - [x] `MacosForwarderActionStatus` (alias of `ActionStatus`).
    - [x] `MacosForwarderManagerConfig` dataclass.
    - [x] `MacosForwarderActionResult` dataclass.
    - [x] `MacosForwarderSpec` dataclass.
    - [x] `MacosForwarderInfo` dataclass.
    - [x] `MacosForwarderStatus` dataclass.
    - [x] `MacosForwarderTestResult` dataclass.
    - [x] `MacosForwarderQueryError` exception.
    - [x] `MacosRemoteLogForwardingManager` class with the
          seven verbs.
    - [x] Module docstring documenting the unified-logging
          limitation prominently.
    - [x] Module-level renderer `_render_block`.
    - [x] Module-level helpers `_upsert_block`, `_remove_block`,
          `_toggle_block_enabled`, `_extract_blocks`.
- [x] Author the test module with at least 14 tests covering:
    - [x] `add_forwarder` argv shape.
    - [x] `add_forwarder` SKIPPED when present with matching body.
    - [x] `add_forwarder` DRY_RUN does not invoke the runner.
    - [x] `remove_forwarder` argv shape.
    - [x] `remove_forwarder` SKIPPED when absent.
    - [x] `list_forwarders` parser handles mixed enabled / disabled.
    - [x] `enable_forwarder` argv shape.
    - [x] `enable_forwarder` SKIPPED when already enabled.
    - [x] `disable_forwarder` argv shape.
    - [x] `test_forwarder` invokes `logger`.
    - [x] `get_forwarder_status` parses on-disk state.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] macOS unified-logging limitation documented in module
          docstring.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **`os_log` predicate-driven shipper.** Out of scope; covered
  by Filebeat / NXLog / Fluent Bit recommendations.
- **TLS / TCP forwarding.** Apple's `syslogd` is UDP-only.
- **`launchctl bootstrap` / `bootout` lifecycle.** Manager only
  kickstarts the existing service.
- **`asl_log_message` predicate filters.** Out of scope.

---

## Acceptance Snapshot

`requirements.md` covers ten requirements.  `design.md`
documents the legacy syslog model, the unified-logging
limitation, the sentinel-bracketed block layout, and the
spec-vs-contract drift.  The implementation file passes the
following hand-checks:

1. Every public method has a Sphinx docstring with
   `:param`, `:returns`, and (where applicable) `:raises`.
2. Every renderer / parser is a module-level pure function with
   keyword-only parameters.
3. No nested function calls in the dispatch path.
4. No mutable default values.
5. No truthy / falsy shortcut tests.
6. Every error string is built via `os.linesep.join(lines)`.
7. The module docstring includes the unified-logging
   limitation prominently.

---

## Test Bar

- Minimum 14 tests; current count at completion: 16.
- Every test uses `_MockCommandRunner` — no live `syslogd`
  invocations.
