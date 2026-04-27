# Implementation Plan — Process Management (macOS)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature mixes read and write verbs.  Writes are dry-run-aware and
idempotent where the verb's nature permits; reads never elevate.

## Module and Models

- [x] 1. Create the
  `automojo.sysman.macos.processmanagement` module.

- [x] 2. Define `MacosProcessActionStatus` (alias of the package's
  shared `ActionStatus`: `SUCCESS / FAILED / SKIPPED / DRY_RUN`).

- [x] 3. Define `MacosProcessManagerConfig` inheriting from
  `FeatureConfig`, adding `default_signal: str = "TERM"` and
  `wait_poll_interval_seconds: float = 0.2`.

- [x] 4. Define `MacosProcessQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message built
  via `os.linesep.join`.

- [x] 5. Define data dataclasses: `MacosProcessInfo`,
  `MacosProcessSpec`, `MacosProcessActionResult`,
  `MacosProcessWaitResult`.

## Manager — Implemented

- [x] 6. Implement `__init__` (inherited; no overrides needed).

- [x] 7. Implement `list_processes(owner=None)` via
  `ps -A -o <format>` (or `ps -U <owner> -o <format>`) — note BSD
  `-U`, not Linux `-u`.

- [x] 8. Implement `get_process(pid)` via
  `ps -p <pid> -o <format>`.  Treat exit-code-non-zero with empty
  stderr as the "unknown PID" path returning `None`.

- [x] 9. Implement `find_processes_by_name(name)` via `pgrep -l`.
  Treat exit code 1 as the "no matches" path returning `[]`.  Note
  BSD `pgrep` lacks the Linux `-a` flag.

- [x] 10. Implement `kill_process(pid, signal=None)` with
  idempotency guard via `get_process`, dry-run short-circuit,
  default-signal fallback, and signal-name validation.

- [x] 11. Implement `kill_process_tree(pid, signal=None)` —
  `pkill -<sig> -P <pid>` followed by `kill -<sig> <pid>`.  Tolerate
  `pkill` exit 1 (no children); annotate detail on other non-zero.

- [x] 12. Implement `start_process(spec)` via
  `nohup sh -c '<shell-quoted argv> &'` (because macOS lacks
  `setsid`) with `cwd` / `env` forwarded and a best-effort `pgrep`
  PID lookup on success.

- [x] 13. Implement `set_process_priority(pid, priority)` via
  `renice <priority> -p <pid>` with idempotency guard via
  `get_process(pid).nice == priority`.

- [x] 14. Implement `wait_for_process(pid, timeout_seconds=None)`
  as a poll loop on `kill -0 <pid>` with `time.sleep` between
  probes; return `timed_out=True` on timeout.

## Module-Level Parsers — Implemented

- [x] 15. `_parse_ps_output` (12-column BSD `ps` row + trailing
  `args`).  The parser drops any row whose first token is
  non-numeric so the header row from BSD `ps` is ignored without
  needing a `--no-headers` switch (which BSD `ps` does not support).

- [x] 16. `_parse_pgrep_output` (`<pid> <command>` rows).

- [x] 17. `_safe_int`, `_safe_float` token coercion helpers.

- [x] 18. `_shell_escape_argv` — single-quote-escapes argv for
  `nohup sh -c`.

- [x] 19. `_best_effort_pid_lookup` — re-queries `pgrep` after
  `start_process` to populate the result `pid`.

## Tests — Implemented

- [x] 20. `test_list_processes_invokes_ps_and_parses`
- [x] 21. `test_get_process_parses_single_record`
- [x] 22. `test_get_process_returns_none_for_unknown_pid`
- [x] 23. `test_kill_process_invokes_kill`
- [x] 24. `test_kill_process_skipped_when_pid_not_found`
- [x] 25. `test_kill_process_dry_run_short_circuits`
- [x] 26. `test_kill_process_tree_kills_children_first`
- [x] 27. `test_start_process_invokes_nohup_sh`
- [x] 28. `test_start_process_dry_run_short_circuits`
- [x] 29. `test_set_process_priority_invokes_renice`
- [x] 30. `test_set_process_priority_skipped_when_already_matches`
- [x] 31. `test_find_processes_by_name_parses_pgrep_output`
- [x] 32. `test_failed_command_returns_failed_or_raises_on_read`
- [x] 33. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 34. **True async-spawn for `start_process`.**  The pilot wraps
  the spawn in `nohup sh -c '... &'` but the runner blocks until the
  shell returns.  A `LocalCommandRunner.spawn(...)` extension is
  needed for genuine background spawning.  Best-effort PID recovery
  via `pgrep` is the placeholder.

- [ ] 35. **Per-user `start_process` privilege drop.**  `spec.user`
  is recorded on the result but does not currently switch the spawn
  user.  A future pass should issue `sudo -u <user> -- <argv>` (or
  `launchctl asuser <uid>`) when `spec.user` is non-`None`.

- [ ] 36. **`launchctl asuser` integration.**  macOS uses
  `launchctl asuser <uid>` to launch processes in another user's
  Aqua session; this is the preferred mechanism for spawning GUI
  apps as a logged-in user.  Out of scope for the pilot.

- [ ] 37. **Recursive `kill_process_tree`.**  Today the verb reaps
  direct children only via `pkill -P`.  Walking the full descendant
  tree is a follow-up.

- [ ] 38. **`sysctl kern.proc.*` integration.**  macOS exposes
  per-process state via `sysctl kern.proc.pid.<pid>` for callers
  that need richer data than BSD `ps -o` can provide.  This pilot
  intentionally relies on `ps` only so that the parser surface
  matches the Linux pilots; richer fields are a follow-up.

- [ ] 39. **Wait-for-exit code recovery.**  `wait_for_process`
  returns `exit_code=None` because only the parent process can
  recover the child's exit code via `waitpid`.

- [ ] 40. **`ProcessFilter` parameter set.**  The cross-OS contract
  defines a `ProcessFilter` dataclass with `owner`, `name_pattern`,
  `min_cpu_percent`, and `min_memory_bytes`.  This pilot exposes
  `owner` only via a keyword argument; the rest are filtered in
  pure Python by the caller.

## Tests — Gaps

- [ ] 41. Per-signal command-construction tests for the full
  `{TERM, KILL, HUP, INT, QUIT, USR1, USR2}` set.

- [ ] 42. `wait_for_process` timeout test using a fake
  `time.monotonic` / `time.sleep` fixture and a runner that always
  reports the PID alive.

- [ ] 43. `start_process` PID-recovery test — assert that on a
  successful spawn the result `pid` is populated from a follow-up
  `pgrep -l` call.

## Spec-vs-Contract Drift

- [ ] 44. Spec-vs-contract drift discovered while authoring this
  spec:

  - Same `ProcessInfo` field rename drift as the Ubuntu spec
    (`name` / `command_line` vs. `command` / `argv`).
  - macOS' BSD `pgrep` cannot reproduce the Linux pilot's argv
    output, so `find_processes_by_name` returns
    `MacosProcessInfo` records whose `argv` field carries only the
    command name rather than the full argv.  Reconciliation may
    add a `MacosProcessManager.find_processes_with_argv(name)`
    extension that combines `pgrep` PIDs with per-PID `ps -o args`
    follow-ups.
  - `setsid` is unavailable on macOS; `nohup ... &` is the closest
    equivalent for `start_process` but does not give the spawned
    process a brand-new session id.  A future contract revision
    should mention the difference.
  - The contract uses a `ProcessSignal` enum; this pilot accepts
    plain `str` signal names.

## Follow-ups

- [ ] 45. **Cross-OS contract conformance review**: align field
  names per the drift list above so a future generic
  `ProcessManager` facade can wrap `MacosProcessManager` uniformly
  with the Linux pilots.

- [ ] 46. **automojo-* reuse check**: confirm that no other macOS
  Layer 3 feature re-implements `ps` parsing that should delegate
  to `MacosProcessManager.list_processes` /
  `MacosProcessManager.get_process`.
