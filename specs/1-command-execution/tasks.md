# Implementation Plan — macOS Command Execution

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

## Runners Subpackage (Layer 1, delivered batch 14/15)

The runners subpackage at `automojo.sysman.macos.runners` is the canonical Layer 1
command-execution surface for macOS.  It mirrors the Ubuntu runners 1:1 with macOS
prefixes; the higher-level `MacosCommandExecutor` shape described later in this
file is a separate (future) Layer-2 facade and is not part of this batch.

### Models — Implemented

- [x] R1. Create the `automojo.sysman.macos.runners` subpackage
- [x] R2. Define `MacosRunResult` dataclass (superset of cross-OS `RunResult`)
- [x] R3. Define `MacosExecutionContext` dataclass for in-flight commands
- [x] R4. Define `MacosRunRecord` dataclass for persistence sinks
- [x] R5. Define `MacosSshErrorType` enum and `MacosSshReliabilityMetrics` dataclass
- [x] R6. Re-export upstream `RunResult` for caller convenience

### Local runner — Implemented

- [x] R7. Implement `MacosLocalRunnerConfig` (dry_run, sudo_path,
  merge_env_with_process)
- [x] R8. Implement `MacosLocalCommandRunner.__init__` accepting an optional
  config
- [x] R9. Implement `add_aspect`, `remove_aspect`, `aspects`, `get_metrics`,
  `reset_metrics`
- [x] R10. Implement `run(args, *, elevated, cwd, env, stdin, timeout, on_output,
  on_error, capture_output)` -- `subprocess.run`-backed
- [x] R11. Elevation prepends `sudo -n` (and `-E` when env is supplied), per the
  macOS sysman contract
- [x] R12. Env merge with `os.environ` controlled by config
- [x] R13. Dry-run path: synthetic success result, no subprocess invocation
- [x] R14. Timeout path: maps `subprocess.TimeoutExpired` to `timed_out=True`,
  `exit_code=-1`
- [x] R15. Missing-executable path: maps `FileNotFoundError` to `exit_code=127`
- [x] R16. `on_output` / `on_error` streaming callbacks on captured stdout/stderr
- [x] R17. Cumulative metrics tracking (success/failure/timeout/duration)
- [x] R18. Aspect chain: before-execute in registration order, after-execute in
  reverse

### SSH runner — Implemented (skeleton)

- [x] R19. Implement `MacosSshRunnerConfig` with full surface (host, username,
  port, password, key_filename, agent, dry_run, sudo_path, default_timeout)
- [x] R20. Implement `MacosSshCommandRunner.__init__`, aspect registry, metrics
- [x] R21. `run()` supports dry-run; non-dry-run raises typed
  `NotImplementedError` describing the missing transport
- [x] R22. Stub `_dispatch_remote` documented as the future wiring point for
  `automojo-protocol-ssh` or `paramiko`

### Aspects — Implemented

- [x] R23. Define `IMacosExecutionAspect` Protocol (`@runtime_checkable`)
- [x] R24. Implement `MacosPersistenceAspect` + `MacosPersistenceAspectConfig`
  writing JSON-line records to a configurable sink, with optional env logging,
  output logging, and storage-error policy
- [x] R25. Implement `MacosRetryAspect` + `MacosRetryAspectConfig` +
  `MacosRetryDecision` (exit-code + regex pattern matching), with
  `should_retry` and `run_with_retries` helpers
- [x] R26. Implement `MacosTimeoutAspect` + `MacosTimeoutAspectConfig` (default
  + allow_override policy)

### Tests — Implemented

- [x] R27. `test_run_success_returns_zero_exit`
- [x] R28. `test_run_failure_returns_nonzero_exit`
- [x] R29. `test_run_captures_stdout_and_stderr`
- [x] R30. `test_run_with_env_passes_env`
- [x] R31. `test_run_with_cwd_uses_cwd`
- [x] R32. `test_run_with_stdin_passes_stdin`
- [x] R33. `test_run_with_elevated_prepends_sudo`
- [x] R34. `test_run_with_elevated_and_env_appends_dash_e`
- [x] R35. `test_run_with_timeout_returns_timed_out_true`
- [x] R36. `test_run_with_on_output_callback_streams`
- [x] R37. `test_run_without_capture_does_not_collect`
- [x] R38. `test_run_missing_executable_returns_nonzero`
- [x] R39. `test_run_dry_run_skips_subprocess`
- [x] R40. `test_metrics_track_success_and_failure`
- [x] R41. `test_aspect_invocation_order`
- [x] R42. Aspect tests: persistence write, retry exit-code decision, retry
  max-cap, timeout default, timeout caller-override, timeout forced-default
  (6 tests)

## MacosCommandExecutor (Layer 2 facade — not in this batch)

The items below describe a higher-level executor wrapping the runners.  They
remain unchecked because that facade is scheduled for a later batch; the
runners subpackage above is sufficient for the cross-OS contract and for every
Layer 3 feature manager already shipped.

## Module and Models

- [ ] 1. Create the `automojo.sysman.macos.commandexecution` module

- [ ] 2. Define `MacosCommandExecutionStatus` enum (SUCCESS / FAILED / SKIPPED /
  DRY_RUN)

- [ ] 3. Define `MacosCommandExecutorConfig` dataclass (dry_run,
  default_timeout_seconds, shell_path)

- [ ] 4. Define `MacosCommandSpec` dataclass (args, elevated, working_dir, env,
  timeout_seconds, label)

- [ ] 5. Define `MacosCommandResult` and `MacosSequenceResult` dataclasses with
  timing fields and elevation echo

## MacosCommandExecutor — Implemented

- [ ] 6. Implement `__init__` accepting an `ICommandRunner` and a
  `MacosCommandExecutorConfig`

- [ ] 7. Implement `run(args, *, elevated, working_dir, env, timeout_seconds)` —
  dry-run short-circuit, empty-args rejection, working-dir check, env merge, runner
  dispatch, timing, result mapping

- [ ] 8. Implement empty-args guard returning `success=False` without invoking the
  runner

- [ ] 9. Implement working-directory precondition using `pathlib.Path.is_dir()`

- [ ] 10. Implement env merge helper producing `{**os.environ, **env}` (copying even
  when `env == {}`)

- [ ] 11. Implement timeout fallback (per-call → config default → `None`) and
  `stderr` injection when runner returns `timed_out=True` with empty `stderr`

- [ ] 12. Implement `run_sequence(commands, *, stop_on_failure)` — empty-list
  short-circuit, dry-run per-spec synthesis, stop-on-failure break, `failed_at`
  computation

- [ ] 13. Implement `run_script(script, *, elevated, working_dir, env,
  timeout_seconds)` — empty-script guard, `tempfile.NamedTemporaryFile`, `chmod 0o755`,
  `try/finally` cleanup via `Path.unlink(missing_ok=True)`

- [ ] 14. Implement OSError guard around tempfile creation + `chmod`, returning
  `success=False` without invoking the runner

## MacosCommandExecutor — Gaps (specified but not implemented)

- [ ] 15. Streaming stdout/stderr callbacks (out of scope for the base contract on
  macOS; document that long `softwareupdate` runs may benefit from a future streaming
  enhancement)

- [ ] 16. PTY allocation for interactive commands (e.g., `security unlock-keychain`)
  — not required by the contract

- [ ] 17. Translating Transparency/Consent/Control denials (stderr
  `Operation not permitted`) into a dedicated typed failure

- [ ] 18. Native `com.apple.security.codesign` invocation support on top of
  `run_script` for signed artefacts

## Tests — Implemented

- [ ] 19. `test_run_success_maps_zero_exit_to_success_true`
- [ ] 20. `test_run_failure_maps_nonzero_to_success_false`
- [ ] 21. `test_run_timeout_flag_surfaces_with_synthetic_stderr`
- [ ] 22. `test_run_empty_args_returns_failure_without_runner_call`
- [ ] 23. `test_run_dry_run_does_not_invoke_runner`
- [ ] 24. `test_working_dir_missing_returns_failure_without_runner_call`
- [ ] 25. `test_working_dir_none_forwards_cwd_none`
- [ ] 26. `test_env_merge_caller_wins_on_conflict`
- [ ] 27. `test_run_sequence_stop_on_failure_stops_at_first_failure`
- [ ] 28. `test_run_sequence_continue_on_failure_runs_all`
- [ ] 29. `test_run_sequence_empty_list_returns_all_succeeded_true`
- [ ] 30. `test_run_script_creates_and_deletes_tempfile`
- [ ] 31. `test_run_script_dry_run_does_not_create_tempfile`
- [ ] 32. `test_run_script_empty_script_returns_failure_without_runner_call`

## Tests — Gaps

- [ ] 33. Test that `config.default_timeout_seconds` fallback applies when per-call
  `timeout_seconds` is `None`

- [ ] 34. Test that aspect composition (Retry flipping fail→success) preserves
  `args`, `elevated`, and `dry_run` on the result

- [ ] 35. Test that `MacosCommandResult.started_at`, `finished_at`, and `duration_ms`
  are `None` in dry-run results and populated otherwise

- [ ] 36. Test that `MacosCommandSpec` fields (`elevated`, `working_dir`, `env`,
  `timeout_seconds`) are forwarded verbatim to the per-command `run` call

## Follow-ups

- [ ] 37. **Cross-OS contract conformance review**: verify that
  `MacosCommandResult` / `MacosSequenceResult` / `MacosCommandSpec` field shapes match
  the cross-OS contract at
  `automojo-sysman-common/specs/contracts/command-execution/contract.md` exactly,
  including the macOS-specific `sudo -n -E` elevation path and the `/bin/bash` default
  shell behaviour.

- [ ] 38. **Shell-version review**: decide whether the default `shell_path` should
  remain `/bin/bash` (macOS 3.2) or migrate to `/opt/homebrew/bin/bash` for scripts
  that rely on modern bash features.
