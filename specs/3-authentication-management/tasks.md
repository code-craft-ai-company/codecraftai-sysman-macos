# Tasks

## Implementation Plan

### 1. Module Skeleton

- [ ] Create
  `source/packages/automojo/sysman/macos/authenticationmanagement.py`.
- [ ] Add module-level constants for `cat`, `tee`, `ls`, the
  default `/etc/pam.d` directory, and the default
  `/etc/authorization` plist path.
- [ ] Import `ActionStatus`, `BaseFeatureManager`,
  `FeatureActionResult`, `FeatureConfig` from
  `automojo.sysman.macos.featurebase`.

---

### 2. Data Models

- [ ] Alias `MacosAuthActionStatus = ActionStatus`.
- [ ] Define `MacosAuthSubsystem` dataclass with `kind` and `name`.
- [ ] Define module-level constants `MACOS_AUTH_LOGIN`,
  `MACOS_AUTH_SUDO`, `MACOS_AUTH_SU`, `MACOS_AUTH_SSH`,
  `MACOS_AUTH_CRON`.
- [ ] Define `MacosAuthEntry`, `MacosAuthConfig`,
  `MacosAuthMethodInfo`, `MacosAuthValidationResult` dataclasses.
- [ ] Define `MacosAuthActionResult` extending
  `FeatureActionResult` with `subsystem`, `method`, `message`.
- [ ] Define `MacosAuthenticationManagerConfig` extending
  `FeatureConfig` with `pam_dir`, `authz_path`,
  `canonical_subsystems`.

---

### 3. Exceptions

- [ ] Define `MacosAuthenticationQueryError` with `command` /
  `exit_code` / `stderr` constructor.
- [ ] Define `MacosAuthenticationConfigError`.
- [ ] Define `MacosAuthenticationSubsystemNotFoundError`.

---

### 4. Parser / Renderer

- [ ] Implement `_parse_pam_stack(content)` and
  `_tokenize_pam_line(line)`.
- [ ] Implement `_render_pam_stack(entries)`.
- [ ] Implement `_walk_pam_lines(content)` returning each line's
  `(comment_state, tokens)` pair — used by `enable_method` /
  `disable_method` to mutate per-line.

---

### 5. Manager — Read Verbs

- [ ] Implement `get_auth_config(subsystem)`.
- [ ] Implement `list_auth_methods()` walking
  `canonical_subsystems`.
- [ ] Implement `validate_auth_config(subsystem)`.

---

### 6. Manager — Write Verbs

- [ ] Implement `set_auth_config(subsystem, config)`.
- [ ] Implement `enable_method(name)` (un-comment occurrences).
- [ ] Implement `disable_method(name)` (comment occurrences).

---

### 7. Manager — Internal Helpers

- [ ] `_typed_config()` narrows the base config.
- [ ] `_resolve_subsystem(subsystem)`.
- [ ] `_pam_path(basename)`.
- [ ] `_read_file(path)`.
- [ ] `_build_result(...)`.
- [ ] `_wrap_runner_result(...)`.

---

### 8. Tests

- [ ] Create
  `source/testroots/testbase/automojo/tests/sysman/macos/test_authenticationmanagement.py`.
- [ ] Add the `_MockCommandRunner` shape.
- [ ] `test_get_auth_config_parses_cat_output`.
- [ ] `test_get_auth_config_raises_subsystem_not_found_when_missing`.
- [ ] `test_set_auth_config_invokes_tee`.
- [ ] `test_set_auth_config_skipped_when_match`.
- [ ] `test_set_auth_config_dry_run`.
- [ ] `test_list_auth_methods_parses_pam_stack_output`.
- [ ] `test_enable_method_invokes_tee`.
- [ ] `test_enable_method_skipped_when_enabled`.
- [ ] `test_enable_method_raises_when_not_found`.
- [ ] `test_disable_method_invokes_tee`.
- [ ] `test_disable_method_skipped_when_disabled`.
- [ ] `test_validate_auth_config_returns_result`.
- [ ] `test_writes_elevate`.
- [ ] `test_reads_never_elevate`.
- [ ] `test_failed_command_surfaces_stderr`.
- [ ] `test_authz_database_documented_as_oos`.
- [ ] `test_pam_stack_parser_handles_includes`.

---

### 9. Quality Bar

- [ ] No emojis anywhere in source / specs / tests.
- [ ] All public types prefixed `Macos`.
- [ ] Sphinx docstrings on every public method.
- [ ] Explicit `return` statements; no compound returns.
- [ ] No mutable default values.
- [ ] Named-parameter passing throughout.
- [ ] Multi-line error messages built via the list / join pattern.
