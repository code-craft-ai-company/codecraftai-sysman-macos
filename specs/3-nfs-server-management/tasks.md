# Implementation Plan — macOS NFS Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create
  ``source/packages/ccai/sysman/macos/nfsservermanagement.py``.
- [ ] 2. Define module-level constants (``_CAT``, ``_TEE``,
  ``_NFSD``, ``_SHOWMOUNT``, default exports path, default service
  label, default options, wildcard-expansion tokens).
- [ ] 3. Define ``MacosNfsActionStatus`` alias of ``ActionStatus``.
- [ ] 4. Define ``MacosNfsManagerConfig`` dataclass extending
  ``FeatureConfig`` with ``exports_file`` and ``service_label``.
- [ ] 5. Define ``MacosNfsActionResult`` extending
  ``FeatureActionResult`` with ``path``, ``share_name``,
  ``message``.
- [ ] 6. Define ``MacosNfsClientSpec``, ``MacosNfsExportSpec``,
  ``MacosNfsExportInfo``, ``MacosNfsExportStatus`` dataclasses.
- [ ] 7. Define ``MacosNfsQueryError`` with ``command`` /
  ``exit_code`` / ``stderr`` / ``os.linesep``-joined message.

## Helpers — file I/O

- [ ] 8. Implement ``_read_file(path)`` invoking ``cat`` without
  elevation and returning ``str | None``.

## Helpers — Darwin parser / renderer

- [ ] 9. Implement ``_parse_exports_file(content) →
  list[MacosNfsExportInfo]`` honouring the BSD-derived
  ``<path> -<opt> -<opt> <client>`` grammar including
  ``-network <addr> -mask <mask>`` qualifiers.
- [ ] 10. Implement ``_render_export_line(spec) → str`` rendering
  the canonical Darwin shape and expanding ``host="*"`` to
  ``-network 0.0.0.0 -mask 0.0.0.0``.
- [ ] 11. Implement ``_render_export_line_for_info(info) → str``
  reusing the spec renderer for parsed entries.
- [ ] 12. Implement ``_match_existing_export(existing, spec) → str``
  returning ``"exact"`` / ``"path-only"`` / ``"absent"``.
- [ ] 13. Implement ``_upsert_export(content, spec) → str``.
- [ ] 14. Implement ``_remove_export_lines(content, path) → str``.
- [ ] 15. Implement ``_set_options_for_path(content, path, options)
  → str | None``.
- [ ] 16. Implement ``_parse_showmount_e_output(text) →
  dict[str, list[str]]`` mapping path to client list.

## Manager — lifecycle

- [ ] 17. Implement ``MacosNfsServerManager`` class extending
  ``BaseFeatureManager`` with ``result_class``, ``status_class``,
  ``feature_name``.
- [ ] 18. Implement ``__init__(runner, config=None)``.

## Manager — write verbs

- [ ] 19. Implement ``add_export(spec)`` per design.md.
  - _Requirements: 1, 7, 11.1_
- [ ] 20. Implement ``remove_export(path)`` per design.md.
  - _Requirements: 2, 11.2_
- [ ] 21. Implement ``reload_exports()`` per design.md.
  - _Requirements: 4, 11.4_
- [ ] 22. Implement ``set_export_options(path, options)`` per
  design.md.
  - _Requirements: 6, 11.3_

## Manager — read verbs

- [ ] 23. Implement ``list_exports()`` per design.md.
  - _Requirements: 3_
- [ ] 24. Implement ``get_export_status(path)`` per design.md.
  - _Requirements: 5_

## Manager — internal helpers

- [ ] 25. Implement ``_invoke_nfsd_update_reload()`` chained after
  successful primary mutations.
- [ ] 26. Implement ``_invoke_showmount_e()`` for active-state
  reads.
- [ ] 27. Implement ``_build_result(...)`` and
  ``_wrap_runner_result(...)``.

## Tests

- [ ] 28. Create
  ``source/testroots/testbase/ccai/tests/sysman/macos/test_nfsservermanagement.py``.
- [ ] 29. Add ``test_add_export_invokes_nfsd``.
- [ ] 30. Add ``test_add_export_skipped_when_present``.
- [ ] 31. Add ``test_add_export_dry_run``.
- [ ] 32. Add ``test_remove_export_invokes_nfsd``.
- [ ] 33. Add ``test_remove_export_skipped_when_absent``.
- [ ] 34. Add ``test_list_exports_parses_showmount_output``.
- [ ] 35. Add ``test_reload_exports_invokes_nfsd``.
- [ ] 36. Add ``test_get_export_status_parses_showmount_output``.
- [ ] 37. Add ``test_set_export_options_invokes_nfsd``.
- [ ] 38. Add ``test_writes_elevate``.
- [ ] 39. Add ``test_reads_never_elevate``.
- [ ] 40. Add ``test_failed_command_surfaces_stderr``.
- [ ] 41. Add ``test_export_line_uses_dash_options_syntax``.
- [ ] 42. Add ``test_wildcard_host_expands_to_network_mask``.
- [ ] 43. Add ``test_reload_failure_propagates``.

## Follow-ups

- [ ] 44. **Per-client option scoping.** Detect divergent client
  options on a single spec and emit one Darwin export-line per
  client to preserve scoping.
- [ ] 45. **``nfsd checkexports`` validation.** Add a
  ``test_exports_file()`` read verb that validates the file before
  commit.
- [ ] 46. **CIDR auto-translation.** Detect ``host`` strings of the
  form ``a.b.c.d/N`` and translate to
  ``-network a.b.c.d -mask <derived>`` automatically.
