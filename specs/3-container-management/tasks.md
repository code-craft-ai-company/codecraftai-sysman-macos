# Implementation Plan — macOS Container Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.containermanagement`
  module.

- [x] 2. Define `MacosContainerActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `MacosContainerEngine` enum with `DOCKER` (default)
  and `PODMAN`.

- [x] 4. Define `MacosContainerManagerConfig` dataclass with
  `engine`, `force_pull`, and `default_stop_timeout` extras.

- [x] 5. Define `MacosContainerActionResult` dataclass with
  `engine` / `container_id` / `image` / `target` / `message`
  extras.

- [x] 6. Define `MacosContainerExecResult` dataclass with
  `engine` / `container_id` / `command_argv` / `output` extras.

- [x] 7. Define `MacosImageInfo`, `MacosContainerSpec`,
  `MacosContainerInfo`, `MacosContainerLogs` dataclasses.

- [x] 8. Define `MacosContainerQueryError`,
  `MacosContainerEngineNotFoundError`,
  `MacosContainerNotFoundError` exceptions.

## MacosContainerManager — Implemented

- [x] 9. `__init__` accepting runner + optional config.

- [x] 10. `pull_image(ref)` — `<engine> pull <ref>`; idempotent
  via `<engine> images --filter`.

- [x] 11. `remove_image(ref)` — `<engine> rmi <ref>`; idempotent
  via `<engine> images --filter`.

- [x] 12. `list_images()` — `<engine> images --format '{{json .}}'`.

- [x] 13. `create_container(spec)` — `<engine> create --name <name>
  [<flags...>] <image> [<argv...>]`; idempotent via
  `<engine> inspect <name>`.

- [x] 14. `start_container(container_id)` — `<engine> start <id>`;
  idempotent.

- [x] 15. `stop_container(container_id)` — `<engine> stop -t
  <timeout> <id>`; idempotent.

- [x] 16. `remove_container(container_id)` — `<engine> rm -f <id>`;
  idempotent.

- [x] 17. `list_containers(all=False)` — `<engine> ps [-a]
  --format '{{json .}}'`.

- [x] 18. `inspect_container(container_id)` — `<engine> inspect
  <id>` parsing the JSON ARRAY.

- [x] 19. `get_container_logs(container_id, lines=100)` —
  `<engine> logs --tail <lines> <id>`.

- [x] 20. `exec_in_container(container_id, argv)` — `<engine> exec
  <id> <argv...>`; never skips.

- [x] 21. `_dispatch_container_write` and `_build_result` helpers.

- [x] 22. `_engine_binary` helper that maps
  `MacosContainerEngine.DOCKER` to `docker` and
  `MacosContainerEngine.PODMAN` to `podman`.

## MacosContainerManager — Gaps

- [ ] 23. Strict-read mode for the read verbs that raises
  `MacosContainerQueryError`.

- [ ] 24. Explicit engine-availability probe that raises
  `MacosContainerEngineNotFoundError`.

- [ ] 25. Surface the `DOCKER_DESKTOP` / `COLIMA` engine identifiers
  by probing `docker context inspect`.  Currently both flavors use
  the generic `DOCKER` value.

- [ ] 26. Surface a `force` parameter on `remove_image`.

- [ ] 27. Spec-equivalence comparison in `create_container` (env,
  volumes, ports, networks, restart policy, working dir, user).

- [ ] 28. Registry-login verb (`<engine> login` with stdin
  password).

- [ ] 29. Docker Desktop / colima / podman-machine VM-status verb
  for coordinators that want to ensure the VM is running before
  any container verb.

## Tests — Implemented

- [x] 30. `test_pull_image_invokes_engine_pull`
- [x] 31. `test_pull_image_skipped_when_already_present`
- [x] 32. `test_pull_image_dry_run_short_circuits`
- [x] 33. `test_remove_image_invokes_rmi`
- [x] 34. `test_list_images_parses_json_lines`
- [x] 35. `test_create_container_invokes_create_with_spec`
- [x] 36. `test_create_container_skipped_when_name_present_with_same_spec`
- [x] 37. `test_start_container_skipped_when_running`
- [x] 38. `test_stop_container_skipped_when_stopped`
- [x] 39. `test_remove_container_invokes_rm_force`
- [x] 40. `test_list_containers_with_all_includes_stopped`
- [x] 41. `test_inspect_container_parses_json_array`
- [x] 42. `test_get_container_logs_invokes_logs_with_tail`
- [x] 43. `test_exec_in_container_invokes_exec`
- [x] 44. `test_engine_selection_dispatches_to_podman_when_configured`
- [x] 45. `test_writes_pass_elevated_per_config`
- [x] 46. `test_reads_never_elevate`
- [x] 47. `test_failed_command_surfaces_stderr`
- [x] 48. Module-level parser helpers
  (`_parse_image_lines`, `_parse_container_ps_lines`,
  `_parse_inspect_first`, `_build_create_argv`).

## Tests — Gaps

- [ ] 49. Digest-pinned-reference idempotency test.
- [ ] 50. `force_pull=True` behavior test.
- [ ] 51. Engine-not-found exception test once the explicit probe
  lands.

## Follow-ups

- [ ] 52. **Cross-OS contract conformance review** against
  `automojo-sysman-common/specs/contracts/container-management/contract.md`.

- [ ] 53. **automojo-* reuse check**: confirm no other feature in
  `automojo-sysman-macos` invokes `docker` / `podman` directly.

## Spec deviations from this implementation

- **Engine identifier set is narrower than the contract.**  The
  cross-OS contract enumerates `PODMAN`, `DOCKER`, `CONTAINERD`,
  `DOCKER_DESKTOP`, and `COLIMA`.  This macOS implementation only
  surfaces `DOCKER` and `PODMAN`; Docker Desktop and colima both
  expose the same `docker` binary on PATH so the manager has no
  reason to distinguish at the verb layer.  Surfacing the
  distinction is reserved for follow-up task 25.

- **`create_container` idempotency is image-only.**  Same as Ubuntu:
  the guard compares `Config.Image` only.  A full spec-equivalence
  comparison is reserved for follow-up task 27.

- **`stop_container` returns SKIPPED when the container is absent.**
  Matches the Ubuntu sibling's "no work to do" treatment.

- **No strict-read mode.**  Read verbs return empty / `None`.

- **`get_container_logs.truncated` is always True.**

- **No engine-availability probe on construction.**

- **No VM-lifecycle integration.**  The manager does not start
  Docker Desktop, colima, or `podman machine`.  Coordinators must
  ensure the VM is running ahead of time.
