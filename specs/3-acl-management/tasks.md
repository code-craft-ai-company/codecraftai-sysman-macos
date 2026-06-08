# Tasks

## Source Module

- [x] Create `aclmanagement.py` under
      `source/packages/ccai/sysman/macos/`.
- [x] Module docstring covering the verb catalogue, file-I/O
      policy, elevation policy, idempotency policy, and ACL-model
      commentary.
- [x] Module-level binary / flag constants (`_LS`, `_CHMOD`,
      `_LS_FLAGS`, `_CHMOD_ADD_ACE`, `_CHMOD_REMOVE_ACE`,
      `_CHMOD_CLEAR_ACL`).
- [x] Alias `MacosAclActionStatus` to the package-shared
      `ActionStatus`.
- [x] `MacosAcePrincipalKind` enum with `USER`, `GROUP`.
- [x] `MacosAcePermission` enum with `ALLOW`, `DENY`.
- [x] `MacosAclManagerConfig` dataclass extending `FeatureConfig`
      with `ls_path` and `chmod_path` fields.
- [x] `MacosAclActionResult` dataclass extending
      `FeatureActionResult` with `path`, `principal`, `message`
      fields.
- [x] `MacosAceInfo` dataclass for parsed read-shape ACEs (with
      `index`, `principal_kind`, `principal`, `permission`,
      `rights`, `inheritable`).
- [x] `MacosAceSpec` dataclass for caller-supplied write-shape
      ACEs.
- [x] `MacosAclInfo` dataclass for the per-path snapshot.
      `default_aces` is always empty on macOS.
- [x] `MacosAclQueryError` exception with `command`, `exit_code`,
      `stderr` fields and a multi-line message body.

## Manager Class

- [x] `MacosAclManager(BaseFeatureManager)` with class variables
      `result_class`, `status_class`, `feature_name`.
- [x] `get_acl(path)` -> `MacosAclInfo`.  Dispatches `ls -lde
      <path>`; raises `MacosAclQueryError` on non-zero.
- [x] `list_aces(path)` -> `list[MacosAceInfo]`.  Re-uses
      `get_acl`; emits parsed ACEs in source order.
- [x] `set_acl(path, acl_spec)` -> `MacosAclActionResult`.
      Dispatches `chmod -N <path>` then `chmod +a "<rendered>"
      <path>` per ACE.  Skipped when the parsed current ACL
      matches.  Dry-run short-circuits.
- [x] `add_ace(path, principal, rights=None)` ->
      `MacosAclActionResult`.  Dispatches `chmod +a "<rendered>"
      <path>`.  Skipped when the ACE is already present.  Dry-run
      short-circuits.
- [x] `remove_ace(path, principal)` -> `MacosAclActionResult`.
      Dispatches `chmod -a "<rendered>" <path>` per matching ACE.
      Skipped when no ACE matches the principal.  Dry-run
      short-circuits.
- [x] `clear_acl(path)` -> `MacosAclActionResult`.  Dispatches
      `chmod -N <path>`.  Skipped when no ACEs exist.  Dry-run
      short-circuits.
- [x] `copy_acl(src, dst)` -> `MacosAclActionResult`.  Reads source
      via `ls -lde`, dispatches `chmod -N <dst>`, then `chmod +a`
      per parsed source ACE.  Skipped when source / destination
      ACLs match.  Dry-run short-circuits.

## Module-Level Helpers

- [x] `_INHERITANCE_TOKENS` frozenset of macOS inheritance rights
      tokens.
- [x] `_materialise_ace_spec(principal, rights_override)` for
      caller convenience on `add_ace`.
- [x] `_render_chmod_ace_token(spec)` rendering one ACE token.
- [x] `_parse_ls_le_output(output, path)` ->
      `MacosAclInfo`.
- [x] `_parse_ls_long_line_owner_group(line)` for the `ls -l` first
      line.
- [x] `_parse_ls_le_ace_line(line)` for a single ACE row.
- [x] `_ace_already_present(current, spec)` for `add_ace`
      idempotency.
- [x] `_find_matching_aces(current, kind, principal)` for
      `remove_ace`.
- [x] `_acl_matches_spec(current, desired)` for `set_acl`
      idempotency.
- [x] `_ace_sort_key(item)` ordering helper.
- [x] `_acl_infos_equal(left, right)` for `copy_acl` idempotency.

## Test Module

- [x] Create `test_aclmanagement.py` under
      `source/testroots/testbase/ccai/tests/sysman/macos/`.
- [x] `_MockCommandRunner` recorder.
- [x] `_ls_le_full_payload()` fixture (allow + deny + group ACEs).
- [x] `_ls_le_minimal_payload()` fixture (no ACEs).
- [x] `_seed_full_acl(runner, path)` and `_seed_minimal_acl(...)`
      seeding helpers.
- [x] `test_get_acl_parses_ls_le_output`.
- [x] `test_list_aces_returns_full_set`.
- [x] `test_set_acl_invokes_chmod`.
- [x] `test_set_acl_skipped_when_match`.
- [x] `test_set_acl_dry_run_short_circuits`.
- [x] `test_add_ace_invokes_chmod`.
- [x] `test_add_ace_skipped_when_present`.
- [x] `test_remove_ace_invokes_chmod`.
- [x] `test_remove_ace_skipped_when_absent`.
- [x] `test_clear_acl_invokes_chmod`.
- [x] `test_clear_acl_skipped_when_already_clear`.
- [x] `test_copy_acl_invokes_chmod`.
- [x] `test_copy_acl_skipped_when_match`.
- [x] `test_writes_elevate`.
- [x] `test_reads_never_elevate`.
- [x] `test_failed_command_surfaces_stderr`.
- [x] `test_get_acl_raises_on_failure`.

## Specs

- [x] `requirements.md` covering the 12 numbered requirements.
- [x] `design.md` covering the cross-OS ACL-model divergence,
      verb-to-command mapping, parsing, idempotency, dry-run,
      elevation, error surface, and aspect composition.
- [x] `tasks.md` (this document).

## Out of Scope

- BSD file flags (`chflags`) — covered by a separate filesystem-
  attributes feature.
- macOS Directory-Services principal resolution edge cases (e.g.
  Active Directory bound users) — `chmod` handles resolution; this
  feature does not duplicate it.
- Recursive ACL operations — the cross-OS contract on macOS
  exposes per-path verbs only; `chmod -R` is reserved for a later
  extension.
- macOS Keychain ACLs and System Integrity Protection rules — out
  of scope for filesystem ACLs.
