# Implementation Plan — macOS Storage Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.macos.storagemanagement` module.

- [x] 2. Define `MacosStorageActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `MacosStorageManagerConfig` dataclass with
  `default_filesystem` and `default_container_id` extras on top of the
  base `dry_run` / `elevated`.

- [x] 4. Define `MacosStorageActionResult` dataclass with
  `target` / `pool` / `volume` / `message` extras.

- [x] 5. Define `MacosDiskInfo`, `MacosVolumeInfo`, `MacosPoolSpec`,
  `MacosVolumeSpec` dataclasses.

- [x] 6. Define `MacosStorageQueryError` exception (defined; not yet
  raised — see deviation note).

## MacosStorageManager — Implemented

- [x] 7. `__init__` accepting runner + optional config (inherited from
  `BaseFeatureManager`).

- [x] 8. `list_disks()` — `diskutil list -plist physical` piped
  through `plutil -convert json`.

- [x] 9. `list_volumes()` — `diskutil apfs list -plist` (APFS volumes)
  merged with `diskutil list -plist` (non-APFS partitions, excluding
  Apple_APFS containers).

- [x] 10. `create_volume(spec)` — `diskutil apfs addVolume
  <container> <fs> <name> -size <bytes>B`; idempotent via APFS
  volume-name probe; missing-pool returns FAILED unless
  `default_container_id` is set.

- [x] 11. `delete_volume(volume_id)` — `diskutil apfs deleteVolume
  <volume_id>`; idempotent via volume-id probe; data-loss advisory on
  success.

- [x] 12. `resize_volume(volume_id, size_bytes)` — APFS path routes
  through `diskutil apfs resizeContainer <container> <bytes>B`; HFS+
  path uses `diskutil resizeVolume <volume_id> <bytes>B`; idempotent
  via 1-MiB tolerance.

- [x] 13. `create_pool(spec)` — `diskutil apfs createContainer
  <disks...>`; idempotent via Physical-Store membership of any listed
  disk.

- [x] 14. `delete_pool(pool_name)` — `diskutil apfs deleteContainer
  <pool>`; idempotent via container-reference probe; data-loss
  advisory on success.

- [x] 15. `add_to_pool(pool_name, disk_id)` — `diskutil apfs
  resizeContainer <pool> 0 <disk>` (the `0` keeps current size while
  adding the new physical store); idempotent via Physical-Store probe.

- [x] 16. `remove_from_pool(pool_name, disk_id)` — `diskutil apfs
  resizeContainer <pool> 0 -none <disk>`; idempotent via
  Physical-Store probe; data-loss advisory on success.

- [x] 17. `get_volume_info(volume_id)` — indexed lookup over
  `list_volumes()`; returns `None` when not found.

- [x] 18. `get_disk_info(disk_id)` — indexed lookup over
  `list_disks()`; returns `None` when not found.

- [x] 19. `_run_diskutil_json` helper — runs `diskutil ... -plist`
  then pipes the stdout through `plutil -convert json -o - -` via
  ``stdin`` so the JSON parsing layer never sees raw plist.

- [x] 20. `_dispatch_storage_write` and `_build_result` helpers —
  thread `target` / `pool` / `volume` / `message` through
  `BaseFeatureManager._dispatch`.

## MacosStorageManager — Gaps (specified but not implemented)

- [ ] 21. Strict-read mode for the read verbs that raises
  `MacosStorageQueryError` instead of returning empty / `None`.

- [ ] 22. Populate `MacosDiskInfo.model` and
  `MacosDiskInfo.serial` via `diskutil info -plist <disk>`.

- [ ] 23. Populate `MacosDiskInfo.health_status` via `smartctl -i`
  (or `diskutil info`'s `SMARTStatus`).

- [ ] 24. Honour `MacosPoolSpec.resiliency` once Apple exposes a
  resiliency knob through `diskutil`.

- [ ] 25. Support volume mount paths as `volume_id` arguments by
  resolving through `diskutil info -plist <path>`.

## Tests — Implemented

- [x] 26. `test_list_disks_invokes_diskutil_and_parses`
- [x] 27. `test_list_volumes_parses_output`
- [x] 28. `test_create_volume_invokes_diskutil`
- [x] 29. `test_create_volume_skipped_when_already_present`
- [x] 30. `test_create_volume_dry_run_short_circuits`
- [x] 31. `test_delete_volume_invokes_diskutil`
- [x] 32. `test_delete_volume_skipped_when_absent`
- [x] 33. `test_resize_volume_invokes_resizecontainer`
- [x] 34. `test_resize_volume_skipped_when_size_matches`
- [x] 35. `test_create_pool_invokes_diskutil_createcontainer`
- [x] 36. `test_delete_pool_invokes_diskutil_deletecontainer`
- [x] 37. `test_add_to_pool_invokes_resizecontainer_with_zero`
- [x] 38. `test_remove_from_pool_invokes_resizecontainer_with_none`
- [x] 39. `test_get_volume_info_parses_output`
- [x] 40. `test_get_disk_info_parses_output`
- [x] 41. `test_writes_elevate`
- [x] 42. `test_reads_never_elevate`
- [x] 43. `test_failed_command_surfaces_stderr`
- [x] 44. Module-level parser helpers (`_strip_partition_suffix`,
  `_container_in_apfs_payload`, `_disk_in_apfs_container`).

## Tests — Gaps

- [ ] 45. HFS+ branch test for `resize_volume` proving the
  `diskutil resizeVolume` fallback dispatches when the volume has no
  APFS container.

- [ ] 46. Coverage for `default_container_id` substitution when the
  caller's spec omits `pool`.

- [ ] 47. `create_pool` skipped-when-disk-already-in-other-container
  path (currently covered by the helper test only).

- [ ] 48. Property-based test for `_strip_partition_suffix` against
  randomised disk-id strings.

## Follow-ups

- [ ] 49. **Cross-OS contract conformance review**: verify
  `MacosStorageActionResult` field shapes match the cross-OS contract
  at `automojo-sysman-common/specs/contracts/storage-management/contract.md`.

- [ ] 50. **automojo-* reuse check**: confirm that no other feature in
  `automojo-sysman-macos` invokes `diskutil apfs` directly; all such
  calls should go through `MacosStorageManager`.

## Spec deviations from this implementation

- **APFS shared sizing semantics for `resize_volume`.**  The contract
  verb is `resize_volume(volume_id, size_bytes)` but the underlying
  command on APFS is `diskutil apfs resizeContainer
  <container> <bytes>B`.  Every sibling volume's effective ceiling
  moves with the call.  The implementation surfaces the container id
  on `target` and documents the semantic in `design.md`.

- **`MacosPoolSpec.name` is recorded but not consumed.**  `diskutil
  apfs createContainer` does not accept a caller-chosen name —
  containers are identified by the disk# they land on
  (`ContainerReference`).  The name is preserved on the result for
  audit logging.

- **`MacosPoolSpec.resiliency` is ignored.**  APFS container
  resiliency is Apple-managed.  The field exists for cross-OS-contract
  parity and surfaces unchanged on the result.

- **Volume identifier syntax is the bare BSD device id.**  Mount paths
  and `/dev/diskNsM` forms are NOT accepted by the write verbs.
  Coordinators that have a mount path should resolve to a BSD id via
  `diskutil info <path>` themselves.

- **No strict-read mode.**  Read verbs return empty lists / `None`
  when their underlying command fails, mirroring the established
  macOS pattern.

- **`MacosDiskInfo.model` / `serial` / `health_status` are always
  `None`.**  Populated from `diskutil info -plist <disk>` /
  `smartctl -i` in a follow-up batch.

- **`add_to_pool` uses `resizeContainer <pool> 0 <disk>`, not
  `apfs addPhysicalStore`.**  Apple discontinued the standalone
  `addPhysicalStore` verb; `resizeContainer 0 <disk>` is the
  documented modern replacement.

- **`remove_from_pool` uses `resizeContainer <pool> 0 -none <disk>`.**
  This is the `diskutil` man-page idiom for "shrink the container's
  physical-store list by removing this disk while keeping the current
  size".  When APFS rejects the eviction (containers in active use)
  the runner's stderr is surfaced verbatim on a FAILED result.

- **The plutil pipeline is dispatched through the runner.**  Each
  ``diskutil ... -plist`` call is followed by a separate ``plutil
  -convert json -o - -`` invocation with the diskutil stdout passed in
  via ``stdin=``.  This keeps every command observable in the runner's
  call log and lets aspects compose uniformly.

- **Resize tolerance is 1 MiB.**  APFS allocates in 4 KiB blocks; we
  pick a coarser bound to avoid silent SKIPPED on tiny rounding
  deltas.
