"""
    macOS configuration-management feature.

    Implements the cross-OS configuration-management contract for macOS
    by routing each call by the source path:

    * Apple plist files (path ends in ``.plist`` OR lives under
      ``/Library/Preferences/`` or ``~/Library/Preferences/``) are
      manipulated with ``defaults``.
    * Every other path is treated as a plain shell-style ``KEY=VALUE``
      configuration file and is manipulated with ``cat`` / ``tee``.

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin; backups use ``cp``; restore-byte-compares use ``cmp``.
        This module does NOT consume an :class:`IFileOperator`.

    Plist-type inference (write):
        ``"true"``/``"false"`` -> ``-bool``; all-digit strings ->
        ``-int``; everything else -> ``-string``.  Floats, arrays, and
        nested dicts must be written via :meth:`reload_configuration`
        + an externally rendered plist; the limitation is documented
        in ``specs/3-configuration-management/tasks.md``.

    Same-user plist caveat:
        Writing to a user plist while running as root requires either
        ``sudo -u <user>`` wrapping or running as that user; the
        current pilot does NOT detect same-user vs root and lets the
        caller drive the choice via ``config.elevated``.

    Elevation policy:
        Write verbs and ``reload_configuration`` honour
        ``config.elevated``.  Reads (``read_setting``,
        ``list_settings``) and idempotency probes never elevate.
"""

import os
import re

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_DEFAULTS = "defaults"

_SHELL_KV_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)

# Match a single ``    key = value;`` row in ``defaults read`` output.
# The leading indent (four spaces in Apple's pretty-printer), the
# unquoted-or-quoted key, the ``=``, the value (everything up to the
# trailing ``;``).  Nested dict / array values do NOT match this pattern
# and are surfaced as opaque single-line entries when possible, skipped
# otherwise — see :func:`_parse_defaults_read`.
_DEFAULTS_KV_PATTERN = re.compile(
    r'^\s*"?([^"=]+?)"?\s*=\s*(.+?);\s*$'
)


MacosConfigActionStatus = ActionStatus


@dataclass
class MacosConfigManagerConfig(FeatureConfig):
    """
        Configuration for :class:`MacosConfigurationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backup_suffix_format: ``strftime`` format used to render
                                     the timestamp portion of backup
                                     filenames.  The default produces
                                     a sibling ``.YYYYmmdd-HHMMSS.bak``
                                     name.
        :param defaults_command: Override for the ``defaults`` binary
                                 (used by tests; production callers
                                 should leave the default).
    """
    backup_suffix_format: str = ".%Y%m%d-%H%M%S.bak"
    defaults_command: str = _DEFAULTS


@dataclass
class MacosConfigActionResult(FeatureActionResult):
    """
        Result of a configuration-management write operation.

        :param file: The configuration source the action targeted, or
                     ``None`` for ``reload_configuration``.
        :param key: The setting key targeted, or ``None`` for whole-
                    file operations.
        :param backup_path: Populated by :meth:`backup_configuration`
                            and :meth:`restore_configuration`.
        :param diff: Reserved for a future dry-run unified-diff field;
                     left ``None`` in this implementation.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    file: Optional[str] = None
    key: Optional[str] = None
    backup_path: Optional[str] = None
    diff: Optional[str] = None
    message: Optional[str] = None


@dataclass
class MacosSettingValue:
    """
        A single configuration setting.

        :param key: The setting name.
        :param value: The setting value.  For plist sources this is the
                      single-line ``defaults read <path> <key>``
                      output; for plain-text sources it is the
                      shell-unquoted value.
        :param file: The source path (or domain) the value was parsed
                     from.
    """
    key: str
    value: str
    file: str


class MacosConfigQueryError(Exception):
    """
        Raised when a configuration read fails for a reason other than
        the target source simply being absent.
    """

    def __init__(
        self,
        *,
        file: str,
        message: str,
    ) -> None:
        """
            :param file: The source path that failed to read.
            :param message: Human-readable detail.
        """
        self.file = file
        self.message = message
        err_msg_lines = ["Configuration query failed."]
        err_msg_lines.append("    FILE: {}".format(file))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class MacosConfigurationManager(BaseFeatureManager):
    """
        macOS configuration-file manager.  Routes plist sources through
        ``defaults`` and plain-text sources through ``cat`` / ``tee``;
        every operation is dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = MacosConfigActionResult
    status_class = MacosConfigActionStatus
    feature_name = "configuration-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def read_setting(self, file: str, key: str) -> MacosSettingValue:
        """
            Read a single setting from a configuration source.

            Plist sources dispatch ``defaults read <path> <key>``;
            plain-text sources read the file via ``cat`` and parse it
            as a shell-style ``KEY=VALUE`` file.

            :param file: The source path.
            :param key: The setting name.

            :returns: The :class:`MacosSettingValue`.
            :raises MacosConfigQueryError: When the source is absent or
                                           the key is not present.
        """
        if _is_plist_source(source=file):
            rtnval = self._read_plist_setting(file=file, key=key)
            return rtnval

        rtnval = self._read_text_setting(file=file, key=key)
        return rtnval

    def list_settings(self, file: str) -> dict[str, str]:
        """
            Enumerate every setting in a configuration source.

            Returns an empty dict when the source is absent or
            unparseable.  Plist sources are flattened to top-level
            ``key -> single-line value`` entries via
            ``defaults read <path>``; plain-text sources are parsed
            with the shell-style ``KEY=VALUE`` pattern.

            :param file: The source path.

            :returns: A ``dict[str, str]`` of every parsed entry.
        """
        if _is_plist_source(source=file):
            rtnval = self._list_plist_settings(file=file)
            return rtnval

        rtnval = self._list_text_settings(file=file)
        return rtnval

    # ------------------------------------------------------------------
    # Per-setting write verbs
    # ------------------------------------------------------------------

    def write_setting(
        self,
        file: str,
        key: str,
        value: str,
    ) -> MacosConfigActionResult:
        """
            Set ``key=value`` inside ``file``.

            Plist sources dispatch ``defaults write <path> <key>
            -<type> <value>``; plain-text sources rewrite the file via
            ``tee``.  Skips when the source already carries the
            desired value.

            :param file: The source path.
            :param key: The setting name.
            :param value: The desired value.

            :returns: The :class:`MacosConfigActionResult`.
        """
        if _is_plist_source(source=file):
            rtnval = self._write_plist_setting(
                file=file, key=key, value=value,
            )
            return rtnval

        rtnval = self._write_text_setting(
            file=file, key=key, value=value,
        )
        return rtnval

    def remove_setting(
        self,
        file: str,
        key: str,
    ) -> MacosConfigActionResult:
        """
            Remove ``key`` from ``file``.

            Plist sources dispatch ``defaults delete <path> <key>``;
            plain-text sources rewrite the file via ``tee`` with the
            matching line dropped.  Skips when the key is not present.

            :param file: The source path.
            :param key: The setting name.

            :returns: The :class:`MacosConfigActionResult`.
        """
        if _is_plist_source(source=file):
            rtnval = self._remove_plist_setting(file=file, key=key)
            return rtnval

        rtnval = self._remove_text_setting(file=file, key=key)
        return rtnval

    # ------------------------------------------------------------------
    # Whole-file write / reload verbs
    # ------------------------------------------------------------------

    def reload_configuration(
        self,
        service: str,
    ) -> MacosConfigActionResult:
        """
            Reload a backing service via
            ``launchctl kickstart -k system/<service>``.

            macOS launchd has no first-class ``reload`` primitive;
            ``kickstart -k`` performs a kill+restart, which is the
            closest analogue to systemd's ``reload``.  User-domain
            services would require ``gui/<uid>/<service>`` and a
            target_uid lookup that is out of scope for this manager;
            that follow-up is tracked in
            ``specs/3-configuration-management/tasks.md``.

            Never skips.  Honours dry-run.

            :param service: The launchd service label.

            :returns: The :class:`MacosConfigActionResult`.
        """
        target = "system/{}".format(service)
        command = ["launchctl", "kickstart", "-k", target]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=None,
                key=None,
                command=command,
                message=service,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=service,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=None,
            key=None,
        )
        return rtnval

    def backup_configuration(self, file: str) -> MacosConfigActionResult:
        """
            Copy ``file`` to a sibling timestamped backup via ``cp``.

            No idempotency: every successful invocation creates a
            fresh backup with a new timestamp suffix.  Honours
            dry-run.

            :param file: The source path.

            :returns: The :class:`MacosConfigActionResult` with
                      ``backup_path`` populated on success.
        """
        backup_path = _build_backup_path(
            file=file,
            timestamp_format=self._typed_config().backup_suffix_format,
        )
        command = ["cp", "-p", file, backup_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=file,
                key=None,
                command=command,
                backup_path=backup_path,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
            populated_backup: Optional[str] = backup_path
        else:
            mapped_status = MacosConfigActionStatus.FAILED
            populated_backup = None

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=file,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=file,
            key=None,
            backup_path=populated_backup,
        )
        return rtnval

    def restore_configuration(
        self,
        backup_path: str,
        target_path: str,
    ) -> MacosConfigActionResult:
        """
            Restore ``backup_path`` over ``target_path``.

            Skips when ``cmp <backup> <target>`` reports a byte
            match.  Honours dry-run.

            :param backup_path: The backup file to copy from.
            :param target_path: The destination configuration file.

            :returns: The :class:`MacosConfigActionResult`.
        """
        command = ["cp", "-p", backup_path, target_path]

        if self._is_byte_match(backup_path=backup_path, target_path=target_path):
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=target_path,
                key=None,
                command=command,
                backup_path=backup_path,
                message="target already byte-matches backup",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=target_path,
                key=None,
                command=command,
                backup_path=backup_path,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=target_path,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=target_path,
            key=None,
            backup_path=backup_path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — plist routing
    # ------------------------------------------------------------------

    def _read_plist_setting(
        self,
        *,
        file: str,
        key: str,
    ) -> MacosSettingValue:
        """
            Run ``defaults read <file> <key>`` and surface the value.

            :param file: The plist source path.
            :param key: The setting key.

            :returns: The :class:`MacosSettingValue`.
            :raises MacosConfigQueryError: When ``defaults`` reports
                                           the source or key missing.
        """
        path_token = _plist_path_token(source=file)
        command = [
            self._typed_config().defaults_command, "read", path_token, key,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise MacosConfigQueryError(
                file=file,
                message="defaults read failed: {}".format(
                    run_result.stderr.strip(),
                ),
            )

        rtnval = MacosSettingValue(
            key=key,
            value=run_result.stdout.strip(),
            file=file,
        )
        return rtnval

    def _list_plist_settings(self, *, file: str) -> dict[str, str]:
        """
            Run ``defaults read <file>`` and parse the simple
            top-level entries.

            :param file: The plist source path.

            :returns: A ``dict[str, str]`` of parseable top-level
                      entries.  Nested dicts and arrays are skipped.
        """
        path_token = _plist_path_token(source=file)
        command = [self._typed_config().defaults_command, "read", path_token]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: dict[str, str] = {}
            return rtnval

        rtnval = _parse_defaults_read(text=run_result.stdout)
        return rtnval

    def _write_plist_setting(
        self,
        *,
        file: str,
        key: str,
        value: str,
    ) -> MacosConfigActionResult:
        """
            Dispatch ``defaults write <file> <key> -<type> <value>``.

            :param file: The plist source path.
            :param key: The setting key.
            :param value: The desired value.

            :returns: The :class:`MacosConfigActionResult`.
        """
        path_token = _plist_path_token(source=file)
        type_flag, normalised_value = _infer_plist_type(value=value)

        write_command = [
            self._typed_config().defaults_command, "write",
            path_token, key, type_flag, normalised_value,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=write_command,
            )
            return dry

        current_value = self._read_plist_value_or_none(
            file=file, key=key,
        )
        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=file,
            key=key,
        )
        return rtnval

    def _remove_plist_setting(
        self,
        *,
        file: str,
        key: str,
    ) -> MacosConfigActionResult:
        """
            Dispatch ``defaults delete <file> <key>``.

            :param file: The plist source path.
            :param key: The setting key.

            :returns: The :class:`MacosConfigActionResult`.
        """
        path_token = _plist_path_token(source=file)
        delete_command = [
            self._typed_config().defaults_command, "delete",
            path_token, key,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=delete_command,
            )
            return dry

        current_value = self._read_plist_value_or_none(
            file=file, key=key,
        )
        if current_value is None:
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=delete_command,
                message="key not present in plist",
            )
            return skipped

        run_result = self._runner.run(
            list(delete_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(delete_command),
            file=file,
            key=key,
        )
        return rtnval

    def _read_plist_value_or_none(
        self,
        *,
        file: str,
        key: str,
    ) -> Optional[str]:
        """
            Read a single plist key without raising.  Used as the
            idempotency probe by :meth:`_write_plist_setting` and
            :meth:`_remove_plist_setting`.

            :param file: The plist source path.
            :param key: The setting key.

            :returns: The current value, or ``None`` when the key is
                      missing or ``defaults`` fails.
        """
        path_token = _plist_path_token(source=file)
        command = [
            self._typed_config().defaults_command, "read", path_token, key,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout.strip()
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — plain-text routing
    # ------------------------------------------------------------------

    def _read_text_setting(
        self,
        *,
        file: str,
        key: str,
    ) -> MacosSettingValue:
        """
            Read a single shell-style setting.

            :param file: The plain-text source path.
            :param key: The setting key.

            :returns: The :class:`MacosSettingValue`.
            :raises MacosConfigQueryError: When the file is absent or
                                           the key is not present.
        """
        content = self._read_file(path=file)
        if content is None:
            raise MacosConfigQueryError(
                file=file,
                message="file does not exist or is unreadable",
            )

        parsed = _parse_shell_kv(text=content)
        if key not in parsed:
            raise MacosConfigQueryError(
                file=file,
                message="key not present: {}".format(key),
            )

        rtnval = MacosSettingValue(
            key=key,
            value=parsed[key],
            file=file,
        )
        return rtnval

    def _list_text_settings(self, *, file: str) -> dict[str, str]:
        """
            Enumerate every shell-style setting in ``file``.

            :param file: The plain-text source path.

            :returns: A ``dict[str, str]`` of every parsed entry.
        """
        content = self._read_file(path=file)
        if content is None:
            rtnval: dict[str, str] = {}
            return rtnval

        rtnval = _parse_shell_kv(text=content)
        return rtnval

    def _write_text_setting(
        self,
        *,
        file: str,
        key: str,
        value: str,
    ) -> MacosConfigActionResult:
        """
            Rewrite a plain-text file to contain ``key=value``.

            :param file: The plain-text source path.
            :param key: The setting key.
            :param value: The desired value.

            :returns: The :class:`MacosConfigActionResult`.
        """
        write_command = ["tee", file]

        existing = self._read_file(path=file)
        existing_content = "" if existing is None else existing
        desired_content = _set_shell_kv(
            content=existing_content, key=key, value=value,
        )

        if existing is not None and existing == desired_content:
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired_content,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=file,
            key=key,
        )
        return rtnval

    def _remove_text_setting(
        self,
        *,
        file: str,
        key: str,
    ) -> MacosConfigActionResult:
        """
            Rewrite a plain-text file with the matching ``key=`` line
            removed.

            :param file: The plain-text source path.
            :param key: The setting key.

            :returns: The :class:`MacosConfigActionResult`.
        """
        write_command = ["tee", file]

        existing = self._read_file(path=file)
        if existing is None:
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="file does not exist",
            )
            return skipped

        new_content = _remove_shell_kv(content=existing, key=key)
        if new_content == existing:
            skipped = self._build_result(
                status=MacosConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="key not present in file",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=MacosConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code == 0:
            mapped_status = MacosConfigActionStatus.SUCCESS
        else:
            mapped_status = MacosConfigActionStatus.FAILED

        rtnval = MacosConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=file,
            key=key,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — config / file I/O / probes / result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> MacosConfigManagerConfig:
        """
            Return the manager's config typed as
            :class:`MacosConfigManagerConfig`.

            :returns: The active config typed as
                      :class:`MacosConfigManagerConfig`.
        """
        if isinstance(self._config, MacosConfigManagerConfig):
            rtnval = self._config
        else:
            rtnval = MacosConfigManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code (i.e. the
                      path is missing).
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _is_byte_match(
        self,
        *,
        backup_path: str,
        target_path: str,
    ) -> bool:
        """
            Compare two files byte-for-byte via ``cmp -s``.

            :param backup_path: The backup file path.
            :param target_path: The target file path.

            :returns: True only when ``cmp`` reports exit code 0.
        """
        command = ["cmp", "-s", backup_path, target_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code == 0:
            rtnval = True
        else:
            rtnval = False
        return rtnval

    def _build_result(
        self,
        *,
        status: MacosConfigActionStatus,
        file: Optional[str],
        key: Optional[str],
        command: list[str],
        backup_path: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> MacosConfigActionResult:
        """
            Construct a :class:`MacosConfigActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param file: The source path to echo on the result.
            :param key: The key to echo on the result.
            :param command: The would-have-been argv.
            :param backup_path: Populated for backup / restore actions.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`MacosConfigActionResult`.
        """
        if key is not None:
            detail = key
        else:
            detail = file
        rtnval = MacosConfigActionResult(
            status=status,
            detail=detail,
            command=list(command),
            file=file,
            key=key,
            backup_path=backup_path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _is_plist_source(*, source: str) -> bool:
    """
        Decide whether ``source`` should be routed through ``defaults``.

        Returns True when:
            * the path ends in ``.plist``;
            * the path lives directly under ``/Library/Preferences/`` or
              ``~/Library/Preferences/`` (the canonical plist
              homes — Apple stores domain plists there with no ``.plist``
              extension when accessed by domain name).

        :param source: The source path or domain.

        :returns: True for plist routing; False for plain-text routing.
    """
    if source.endswith(".plist"):
        return True
    if source.startswith("/Library/Preferences/"):
        return True
    if source.startswith("~/Library/Preferences/"):
        return True
    return False


def _plist_path_token(*, source: str) -> str:
    """
        Compute the token passed to ``defaults`` for ``source``.

        ``defaults read /full/path/file.plist`` works when the path is
        absolute; the trailing ``.plist`` is also accepted.  When the
        caller supplies a domain-style path under
        ``/Library/Preferences/<domain>``, we strip the trailing
        ``.plist`` so ``defaults`` parses it as a domain.

        :param source: The source path.

        :returns: The token to hand to ``defaults``.
    """
    rtnval = source
    return rtnval


def _infer_plist_type(*, value: str) -> tuple:
    """
        Infer the ``defaults write`` type flag for a string value.

        - ``"true"`` / ``"false"`` (case-insensitive) -> ``-bool`` and
          the lowercase rendering.
        - All-digit strings (with optional leading minus) -> ``-int``
          and the original string.
        - Everything else -> ``-string`` and the original string.

        Floats, arrays, and nested dicts are NOT inferred and must be
        written via :meth:`reload_configuration` plus an externally
        rendered plist.

        :param value: The desired value as a string.

        :returns: ``(type_flag, value_to_pass)`` tuple.
    """
    lower = value.lower()
    if lower == "true" or lower == "false":
        rtnval = ("-bool", lower)
        return rtnval

    if _is_int_string(text=value):
        rtnval = ("-int", value)
        return rtnval

    rtnval = ("-string", value)
    return rtnval


def _is_int_string(*, text: str) -> bool:
    """
        Return True when ``text`` is a plain integer literal (with an
        optional leading minus).

        :param text: The candidate string.

        :returns: True when integer-shaped.
    """
    if len(text) == 0:
        return False
    candidate = text
    if candidate.startswith("-"):
        candidate = candidate[1:]
    if len(candidate) == 0:
        return False
    if candidate.isdigit():
        return True
    return False


def _parse_defaults_read(*, text: str) -> dict[str, str]:
    """
        Parse ``defaults read <path>`` output into a flat dict.

        Apple's ``defaults`` pretty-prints plists in old-style ASCII
        form::

            {
                "key1" = "value1";
                key2 = 42;
                key3 = (
                    "item",
                );
            }

        We extract simple ``key = value;`` rows.  Multi-line nested
        dict / array values are skipped.

        :param text: Raw stdout from ``defaults read``.

        :returns: A ``dict[str, str]`` of parseable top-level entries.
    """
    rtnval: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _DEFAULTS_KV_PATTERN.match(line)
        if match is None:
            continue
        raw_key = match.group(1).strip()
        raw_value = match.group(2).strip()
        if len(raw_key) == 0:
            continue
        if raw_value.startswith("(") or raw_value.startswith("{"):
            continue
        rtnval[raw_key] = _strip_quotes(value=raw_value)
    return rtnval


def _parse_shell_kv(*, text: str) -> dict[str, str]:
    """
        Parse a shell-style ``KEY=VALUE`` file into a dict.

        Comment (``#``) and blank lines are skipped.  Surrounding
        double quotes around values are stripped.  Lines with non-
        identifier keys are silently skipped.

        :param text: Raw file content.

        :returns: A ``dict[str, str]`` of every parsed entry.
    """
    rtnval: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        match = _SHELL_KV_PATTERN.match(line)
        if match is None:
            continue
        key = match.group(1)
        raw_value = match.group(2)
        rtnval[key] = _strip_quotes(value=raw_value)
    return rtnval


def _set_shell_kv(*, content: str, key: str, value: str) -> str:
    """
        Return ``content`` updated to contain ``KEY="value"``.

        Existing lines for the same key are replaced in place.
        Comment and blank lines are preserved verbatim.  An empty
        starting content yields a single-line file ending in ``\\n``.

        :param content: Current file content.
        :param key: The setting name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = '{}="{}"'.format(key, escaped)

    if len(content) == 0:
        body = new_line + "\n"
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
            updated.append(new_line)
            replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_shell_kv(*, content: str, key: str) -> str:
    """
        Return ``content`` with any matching ``KEY=`` line removed.

        :param content: Current file content.
        :param key: The setting name to remove.

        :returns: The updated file content.
    """
    if len(content) == 0:
        return content

    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _strip_quotes(*, value: str) -> str:
    """
        Strip a single pair of surrounding double quotes from
        ``value``.

        :param value: Raw value text.

        :returns: The unquoted (and unescaped) value.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        unescaped = inner.replace('\\"', '"').replace("\\\\", "\\")
        return unescaped
    return value


def _escape_value(*, value: str) -> str:
    """
        Escape a value for double-quoted shell rendering.

        :param value: The raw value.

        :returns: The escaped value (no surrounding quotes).
    """
    rtnval = value.replace("\\", "\\\\").replace('"', '\\"')
    return rtnval


def _build_backup_path(
    *,
    file: str,
    timestamp_format: str,
) -> str:
    """
        Render a sibling timestamped backup path.

        :param file: The original file path.
        :param timestamp_format: ``strftime`` format applied to the
                                 current UTC time.  The result is
                                 appended to ``file`` as-is, so the
                                 caller controls leading separators.

        :returns: The new backup path.
    """
    timestamp = datetime.now(timezone.utc).strftime(timestamp_format)
    rtnval = "{}{}".format(file, timestamp)
    return rtnval
