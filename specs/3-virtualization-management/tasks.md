# Implementation Plan — Virtualization Management

All tasks below are unchecked at start; tick them in order.

## Module and Models

- [ ] 1. Create
  `source/packages/ccai/sysman/macos/virtualizationmanagement.py`
  with the module docstring covering backend selection, virsh
  discovery, connection-URI rules, elevation policy, and stop
  semantics.
  - _Requirements: All_

- [ ] 2. Define `MacosVirtualizationActionStatus` (alias of
  `ActionStatus`).
  - _Requirements: 12_

- [ ] 3. Define `MacosVirtBackend` enum with `LIBVIRT` (default) and
  `HYPERKIT`.
  - _Requirements: 1_

- [ ] 4. Define `MacosVmState` enum.
  - _Requirements: 4, 8_

- [ ] 5. Define `MacosVirtualizationManagerConfig` adding `backend`,
  `virsh_path`, `virt_install_path`, `default_connection`.
  - _Requirements: 1, 2, 3_

- [ ] 6. Define `MacosVirtualizationActionResult` adding `vm_name`,
  `snapshot_name`, `message`.
  - _Requirements: 12_

- [ ] 7. Define `MacosVmSpec`, `MacosVmInfo`, `MacosVmDiskSpec`,
  `MacosSnapshotInfo` dataclasses.
  - _Requirements: 6, 9, 11_

- [ ] 8. Define `MacosVirtualizationQueryError`.
  - _Requirements: 4, 5, 11_

## Helpers

- [ ] 9. Implement `_typed_config()` returning a typed config.

- [ ] 10. Implement `_backend_supported()` and `_guard_backend()` for
  the HYPERKIT short-circuit path.

- [ ] 11. Implement `_resolve_virsh()` and `_resolve_virt_install()`
  caching the `which` probe results.

- [ ] 12. Implement `_guard_virsh()` and `_guard_virt_install()` for
  the missing-binary FAILED short-circuit.

- [ ] 13. Implement `_virsh_argv(args)` building the `virsh` argv
  with the optional `--connect <uri>` prefix.

- [ ] 14. Implement `_build_virt_install_argv(spec)` rendering the
  full `virt-install` argv from the spec.

- [ ] 15. Implement `_build_disk_argument(spec)` resolving
  `disk_path` vs `disk_size_gb` to a `--disk` value.

- [ ] 16. Implement `_dispatch_virt_write(...)` wrapping `_dispatch`
  with `vm_name` / `snapshot_name`.

- [ ] 17. Implement `_build_result(...)` for non-runner result
  construction.

- [ ] 18. Implement `_read_dominfo(name)` probing `virsh dominfo`.

- [ ] 19. Implement `_disk_path_in_xml(vm, path)` and
  `_disk_attached(vm, disk_id)`.

- [ ] 20. Implement `_snapshot_exists(vm, snapshot_name)`.

## Read Verbs

- [ ] 21. Implement `list_vms()` per Requirement 4.

- [ ] 22. Implement `get_vm_info(name)` per Requirement 5.

- [ ] 23. Implement `list_snapshots(vm)` per Requirement 11.4.

## Write Verbs

- [ ] 24. Implement `create_vm(spec)` per Requirement 6.

- [ ] 25. Implement `delete_vm(name)` per Requirement 7.

- [ ] 26. Implement `start_vm` / `stop_vm` / `pause_vm` /
  `resume_vm` per Requirement 8.

- [ ] 27. Implement `attach_disk` / `detach_disk` per Requirements 9
  and 10.

- [ ] 28. Implement `snapshot_vm(vm, snapshot_name)` and
  `restore_snapshot(vm, snapshot_name)` per Requirement 11.

## Parsers

- [ ] 29. Implement `_parse_virsh_list_names(stdout)`.

- [ ] 30. Implement `_parse_virsh_dominfo(stdout, name)`.

- [ ] 31. Implement `_parse_virsh_snapshot_list(stdout)`.

- [ ] 32. Implement `_normalise_libvirt_state(state_text)`.

## Tests

- [ ] 33. Create
  `source/testroots/testbase/ccai/tests/sysman/macos/test_virtualizationmanagement.py`.

- [ ] 34. Tests for `list_vms` / `list_snapshots` parsers and
  unknown-VM `get_vm_info`.

- [ ] 35. Tests for every write verb's command construction, dry-run,
  and idempotency SKIPPED branch.

- [ ] 36. Test asserting `force=True` -> `virsh destroy` and
  `force=False` -> `virsh shutdown`.

- [ ] 37. Tests for the data-loss advisory on `delete_vm`,
  `detach_disk`, and `restore_snapshot`.

- [ ] 38. Elevation tests proving writes pass `elevated=True` and
  reads pass `elevated=False`.

- [ ] 39. Failure-surfacing test (non-zero runner exit -> FAILED with
  stderr).

- [ ] 40. HyperKit-deprecation test asserting every verb FAILs with
  `message="hyperkit_deprecated"`.

- [ ] 41. virsh-discovery tests:
  - `which virsh` resolves to a path -> verb proceeds normally.
  - `which virsh` reports nothing -> verb FAILs with
    `message="virsh_not_found"`.

## Follow-ups

- [ ] 42. **Apple Virtualization framework** — out of scope today;
  track in a future macOS-only feature backed by a Swift bridge.

- [ ] 43. **`qemu:///session` parity** — when libvirt runs as the
  local user the URI defaults change.  Document a future helper for
  per-user libvirt sockets.

- [ ] 44. **Hypervisor.framework direct binding** — out of scope; a
  PyObjC bridge would be the entry point.

- [ ] 45. **OVA / OVF import** — track separately in a new
  `vm-image-management` feature.
