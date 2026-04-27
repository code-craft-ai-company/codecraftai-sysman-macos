# Implementation Plan — Hardware Inventory (macOS)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature is read-only — no dry-run distinction, no idempotency
guards, no mutating operations.

## Module and Models

- [x] 1. Create the `hardwareinventory` module under
  `automojo.sysman.macos`.

- [x] 2. Define `MacosHardwareInventoryActionStatus` with
  `SUCCESS`, `FAILED`, `SKIPPED` (no `DRY_RUN` per contract).

- [x] 3. Define `MacosHardwareInventoryManagerConfig` inheriting from
  `FeatureConfig` (no extra fields; documents that `dry_run` is
  ignored and `elevated` is reserved).

- [x] 4. Define `MacosHardwareInventoryQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message
  built via `os.linesep.join`.

- [x] 5. Define query-result dataclasses: `MacosSystemInfo`,
  `MacosCpuInfo`, `MacosMemoryInfo`, `MacosMemoryModule`,
  `MacosStorageDeviceInfo`, `MacosNetworkDeviceInfo`,
  `MacosPciDeviceInfo`, `MacosUsbDeviceInfo`, `MacosFirmwareInfo`.

## Manager — Implemented

- [x] 6. Implement `__init__` (inherited; no overrides needed).

- [x] 7. Implement `_read` helper that dispatches a runner call with
  `elevated=False` and raises `MacosHardwareInventoryQueryError`
  on a non-zero exit code.

- [x] 8. Implement `get_system_info` via `system_profiler
  SPHardwareDataType -json`.

- [x] 9. Implement `get_cpu_info` via `sysctl -a machdep.cpu` and
  `sysctl hw.physicalcpu hw.logicalcpu hw.cpufrequency`.

- [x] 10. Implement `get_memory_info` via `system_profiler
  SPMemoryDataType -json` and `sysctl hw.memsize`.

- [x] 11. Implement `list_storage_devices` via `system_profiler
  SPStorageDataType -json`.

- [x] 12. Implement `list_network_devices` via `system_profiler
  SPNetworkDataType -json` and `networksetup
  -listallhardwareports`.

- [x] 13. Implement `list_pci_devices` via `system_profiler
  SPPCIDataType -json`.

- [x] 14. Implement `list_usb_devices` via `system_profiler
  SPUSBDataType -json`, flattening the bus / device tree
  depth-first.

- [x] 15. Implement `get_firmware_info` via `system_profiler
  SPHardwareDataType -json` and `nvram -p`.

## Module-Level Parsers — Implemented

- [x] 16. `_decode_json` shared JSON-decode + raise wrapper.

- [x] 17. `_parse_system_profiler_hardware`,
  `_parse_system_profiler_firmware`, `_merge_nvram_p`.

- [x] 18. `_parse_sysctl_machdep_cpu`, `_merge_sysctl_hw_cpu`.

- [x] 19. `_parse_system_profiler_memory`,
  `_merge_sysctl_memsize`, `_parse_size_string`,
  `_parse_speed_mhz`.

- [x] 20. `_parse_system_profiler_storage`.

- [x] 21. `_parse_networksetup_hardware_ports`,
  `_parse_system_profiler_network`.

- [x] 22. `_parse_system_profiler_pci`.

- [x] 23. `_parse_system_profiler_usb`, `_strip_macos_id`.

## Tests — Implemented

- [x] 24. `test_get_system_info_invokes_system_profiler_and_parses`
- [x] 25. `test_get_cpu_info_parses_sysctl_output`
- [x] 26. `test_get_memory_info_aggregates_modules_and_total`
- [x] 27. `test_list_storage_devices_parses_system_profiler_output`
- [x] 28. `test_list_network_devices_parses_system_profiler_output`
- [x] 29. `test_list_pci_devices_parses_system_profiler_output`
- [x] 30. `test_list_usb_devices_parses_system_profiler_output`
- [x] 31. `test_get_firmware_info_parses_system_profiler_output`
- [x] 32. `test_failed_command_raises_query_error`
- [x] 33. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 34. Apple silicon `architecture` detection.  Currently
  `architecture` is left `None`; on Apple silicon the proper
  source is `sysctl hw.machine` (e.g., `arm64`).  Adding it is a
  one-line change but is left until a real Apple silicon capture
  is available.

- [ ] 35. `secure_boot` detection via `bputil --boot-policy` or
  `nvram -p` on `SecureBootPolicy`.  Currently `secure_boot` is
  always `None`.

- [ ] 36. `cache_l1` / `cache_l2` / `cache_l3` population from
  `sysctl hw.l1icachesize hw.l1dcachesize hw.l2cachesize
  hw.l3cachesize`.  Currently those fields are always `None`.

- [ ] 37. Network `mtu` and `speed` from `ifconfig <bsd>` per
  interface.  Currently `mtu` is always `None` and `speed` is set
  only for explicitly LAN-labelled hardware ports.

- [ ] 38. Storage `device_serial` extraction reliability.  On some
  Macs `device_serial` is absent from `physical_drive`; consider
  falling back to `diskutil info -plist` for serials.

- [ ] 39. `collect_snapshot` aggregator with partial-tolerance
  semantics per the cross-OS contract.

## Tests — Gaps

- [ ] 40. Apple-silicon synthetic-memory shape coverage (no
  `_items` array).  Currently exercised only via a code path the
  tests do not yet seed.

- [ ] 41. Empty-PCI-list test (Apple silicon often surfaces an
  empty `SPPCIDataType`).

- [ ] 42. USB-tree depth > 1 test (hub with downstream devices).

## Follow-ups

- [ ] 43. Profile `system_profiler` cold-start cost.  Each call
  adds ~1-3 seconds on macOS — `collect_snapshot` may need to
  parallelise the eight independent `system_profiler` invocations,
  or fold the eight data types into a single call (`-detailLevel
  basic SPHardwareDataType SPMemoryDataType ...`).

- [ ] 44. **automojo-* reuse check**: `automojo-interop` may
  expose a generic `system_profiler` JSON walker that would
  shrink the parser surface here.

- [ ] 45. Spec-vs-contract drift discovered while writing this
  spec: the contract names the system-identity dataclass
  `SystemIdentity` and lists `machine_id` / `hostname` / `source`
  fields that this pilot does not surface (the macOS analogues are
  `IOPlatformUUID` and `scutil --get LocalHostName`).  Reconcile
  in the next contract revision.
