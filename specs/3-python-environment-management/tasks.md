# Implementation Plan — macOS Python Environment Management

Checked items (`[x]`) are implemented and covered by tests.

## Module

- [x] 1. Create `ccai.sysman.macos.pythonenvironmentmanagement`
- [x] 2. Define `Macos`-prefixed aliases of the shared types
- [x] 3. Define `MacosPythonEnvManagerConfig` (prerequisite_packages, brew_user)
- [x] 4. Implement `_build_layout` → POSIX layout
- [x] 5. Implement `ensure_prerequisites` delegating to `MacosPackageManager`

## Tests

- [x] 6. `test_layout_is_posix`
- [x] 7. `test_ensure_prerequisites_installs_python_formula_without_elevation`
- [x] 8. `test_ensure_prerequisites_wraps_with_brew_user_when_configured`
- [x] 9. `test_ensure_prerequisites_honours_override_and_stops_on_failure`
- [x] 10. `test_ensure_prerequisites_dry_run_dispatches_nothing`
- [x] 11. `test_plain_base_config_is_accepted`
- [x] 12. `test_deploy_end_to_end_with_homebrew_interpreter`

## Follow-ups

- [ ] 13. Helper that resolves the Homebrew prefix (`brew --prefix`) through the
  runner and sets `base_interpreter` automatically after `ensure_prerequisites`
- [ ] 14. Live-host verification on Apple-silicon and Intel macOS with and without
  the Command Line Tools
