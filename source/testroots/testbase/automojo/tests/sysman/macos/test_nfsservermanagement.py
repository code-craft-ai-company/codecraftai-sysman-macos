"""
    Tests for :class:`MacosNfsServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``nfsd`` / ``showmount`` /
    ``cat`` / ``tee`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.macos.nfsservermanagement import (
    MacosNfsActionStatus,
    MacosNfsClientSpec,
    MacosNfsExportInfo,
    MacosNfsExportSpec,
    MacosNfsExportStatus,
    MacosNfsManagerConfig,
    MacosNfsQueryError,
    MacosNfsServerManager,
    _classify_darwin_tokens,
    _parse_exports_file,
    _parse_showmount_e_output,
    _render_export_line,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_EXPORTS_PATH = "/etc/exports"


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


def _seed_showmount(
    runner: _MockCommandRunner,
    *,
    stdout: str,
) -> None:
    runner.results[("showmount", "-e", "localhost")] = _ok(stdout=stdout)
    return


def _make_spec(
    *,
    path: str = "/Users/Shared/share",
    host: str = "*",
    options: Optional[list] = None,
) -> MacosNfsExportSpec:
    if options is None:
        options_list = ["ro", "alldirs", "mapall=nobody"]
    else:
        options_list = list(options)
    spec = MacosNfsExportSpec(
        path=path,
        clients=[MacosNfsClientSpec(host=host, options=options_list)],
    )
    return spec


# ----------------------------------------------------------------------
# add_export
# ----------------------------------------------------------------------


def test_add_export_invokes_nfsd():
    """
        add_export writes the rendered Darwin line via ``tee`` and
        chains ``nfsd update``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    _seed_showmount(runner, stdout="")

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, MacosNfsActionStatus.SUCCESS)
    testbase.assert_equal(result.path, "/Users/Shared/share")

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2
        and call["args"][0] == "tee"
        and call["args"][1] == _EXPORTS_PATH
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "/Users/Shared/share -ro -alldirs -mapall=nobody" in written, True,
    )

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_add_export_skipped_when_present():
    """
        add_export returns SKIPPED when the file already contains an
        identical Darwin-rendered line.
    """
    runner = _MockCommandRunner()
    existing = (
        "/Users/Shared/share -ro -alldirs -mapall=nobody "
        "-network 0.0.0.0 -mask 0.0.0.0\n"
    )
    _seed_cat(runner, path=_EXPORTS_PATH, body=existing)

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, MacosNfsActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_add_export_dry_run():
    """
        add_export under ``dry_run=True`` returns DRY_RUN and does
        not invoke ``tee`` or ``nfsd update``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")

    config = MacosNfsManagerConfig(elevated=True, dry_run=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, MacosNfsActionStatus.DRY_RUN)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_export
# ----------------------------------------------------------------------


def test_remove_export_invokes_nfsd():
    """
        remove_export strips the matching line and chains
        ``nfsd update``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    existing = (
        "/Users/Shared/share -ro -alldirs -mapall=nobody "
        "-network 0.0.0.0 -mask 0.0.0.0\n"
        "/srv/other -ro 10.0.0.0 -mask 255.255.255.0\n"
    )
    _seed_cat(runner, path=_EXPORTS_PATH, body=existing)

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="/Users/Shared/share")

    testbase.assert_equal(result.status, MacosNfsActionStatus.SUCCESS)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("/Users/Shared/share" in written, False)
    testbase.assert_equal("/srv/other" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_remove_export_skipped_when_absent():
    """
        remove_export returns SKIPPED when no line matches.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body="/srv/other -ro 10.0.0.0 -mask 255.255.255.0\n",
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="/Users/Shared/share")

    testbase.assert_equal(result.status, MacosNfsActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# list_exports
# ----------------------------------------------------------------------


def test_list_exports_parses_showmount_output():
    """
        list_exports merges configured exports and active state from
        ``showmount -e localhost``.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "# Top comment\n"
            "/Users/Shared/share -ro -alldirs -mapall=nobody "
            "-network 0.0.0.0 -mask 0.0.0.0\n"
            "/srv/inactive -ro 10.0.0.0 -mask 255.255.255.0\n"
        ),
    )
    _seed_showmount(
        runner,
        stdout=(
            "Exports list on localhost:\n"
            "/Users/Shared/share              Everyone\n"
        ),
    )

    config = MacosNfsManagerConfig()
    manager = MacosNfsServerManager(runner=runner, config=config)

    rtnval = manager.list_exports()

    testbase.assert_equal(len(rtnval), 2)
    by_path = {entry.path: entry for entry in rtnval}
    testbase.assert_equal(
        by_path["/Users/Shared/share"].is_active, True,
    )
    testbase.assert_equal(
        by_path["/srv/inactive"].is_active, False,
    )
    return


# ----------------------------------------------------------------------
# reload_exports
# ----------------------------------------------------------------------


def test_reload_exports_invokes_nfsd():
    """
        reload_exports dispatches ``nfsd update``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)

    result = manager.reload_exports()

    testbase.assert_equal(result.status, MacosNfsActionStatus.SUCCESS)
    testbase.assert_equal(result.command, ["nfsd", "update"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_export_status
# ----------------------------------------------------------------------


def test_get_export_status_parses_showmount_output():
    """
        get_export_status parses ``showmount -e localhost`` output
        for the target path.
    """
    runner = _MockCommandRunner()
    _seed_showmount(
        runner,
        stdout=(
            "Exports list on localhost:\n"
            "/Users/Shared/share              Everyone\n"
        ),
    )
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "/Users/Shared/share -ro -alldirs -mapall=nobody "
            "-network 0.0.0.0 -mask 0.0.0.0\n"
        ),
    )

    config = MacosNfsManagerConfig()
    manager = MacosNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="/Users/Shared/share")

    testbase.assert_equal(status.is_active, True)
    testbase.assert_equal(status.client_count, 1)
    testbase.assert_equal("ro" in status.flags, True)
    return


def test_get_export_status_returns_inactive_for_missing_path():
    """
        get_export_status returns an inactive status for a path not
        in the showmount output.
    """
    runner = _MockCommandRunner()
    _seed_showmount(runner, stdout="Exports list on localhost:\n")

    config = MacosNfsManagerConfig()
    manager = MacosNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="/srv/missing")

    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return


def test_get_export_status_raises_on_failure_with_stderr():
    """
        get_export_status raises :class:`MacosNfsQueryError` when
        ``showmount`` exits non-zero with stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("showmount", "-e", "localhost")] = _fail(
        stderr="showmount: not found", exit_code=127,
    )

    config = MacosNfsManagerConfig()
    manager = MacosNfsServerManager(runner=runner, config=config)

    raised: Optional[MacosNfsQueryError] = None
    try:
        manager.get_export_status(path="/Users/Shared/share")
    except MacosNfsQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 127)
    return


# ----------------------------------------------------------------------
# set_export_options
# ----------------------------------------------------------------------


def test_set_export_options_invokes_nfsd():
    """
        set_export_options rewrites the line's option list and
        chains ``nfsd update``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "/Users/Shared/share -ro -alldirs "
            "-network 0.0.0.0 -mask 0.0.0.0\n"
        ),
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="/Users/Shared/share",
        options=["rw", "alldirs", "mapall=nobody"],
    )

    testbase.assert_equal(result.status, MacosNfsActionStatus.SUCCESS)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("-rw" in written, True)
    testbase.assert_equal("-mapall=nobody" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_set_export_options_skipped_when_match():
    """
        set_export_options returns SKIPPED when the line already
        carries the requested options.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "/Users/Shared/share -ro -alldirs -mapall=nobody "
            "-network 0.0.0.0 -mask 0.0.0.0\n"
        ),
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="/Users/Shared/share",
        options=["ro", "alldirs", "mapall=nobody"],
    )

    testbase.assert_equal(result.status, MacosNfsActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")
    manager.add_export(spec=spec)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs and the ``cat`` / ``showmount`` probes used inside
        write paths never elevate.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "/Users/Shared/share -ro -alldirs -mapall=nobody "
            "-network 0.0.0.0 -mask 0.0.0.0\n"
        ),
    )
    _seed_showmount(
        runner,
        stdout=(
            "Exports list on localhost:\n"
            "/Users/Shared/share              Everyone\n"
        ),
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    manager.list_exports()
    manager.get_export_status(path="/Users/Shared/share")

    for call in runner.calls:
        if call["args"][0] == "cat":
            testbase.assert_equal(call["elevated"], False)
        if call["args"] == ["showmount", "-e", "localhost"]:
            testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``tee`` produces a FAILED result with the runner's
        stderr / exit-code / command attached.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    runner.results[("tee", _EXPORTS_PATH)] = _fail(
        stderr="tee: cannot open", exit_code=1,
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, MacosNfsActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("tee: cannot open" in result.stderr, True)
    testbase.assert_equal(result.command[0], "tee")

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["nfsd", "update"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_reload_failure_propagates():
    """
        A failing ``nfsd update`` after a successful ``tee`` maps to
        FAILED with the reload's argv / stderr / exit code on the
        result.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    runner.results[("tee", _EXPORTS_PATH)] = _ok()
    runner.results[("nfsd", "update")] = _fail(
        stderr="nfsd: invalid", exit_code=2,
    )

    config = MacosNfsManagerConfig(elevated=True)
    manager = MacosNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/Users/Shared/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, MacosNfsActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.command, ["nfsd", "update"])
    testbase.assert_equal("nfsd: invalid" in result.stderr, True)
    return


# ----------------------------------------------------------------------
# Renderer / parser unit tests
# ----------------------------------------------------------------------


def test_export_line_uses_dash_options_syntax():
    """
        The rendered Darwin line carries dash-prefixed options and
        does NOT use the Linux-canonical parenthesised form.
    """
    spec = MacosNfsExportSpec(
        path="/Users/Shared/share",
        clients=[
            MacosNfsClientSpec(
                host="*",
                options=["ro", "alldirs", "mapall=nobody"],
            ),
        ],
    )
    rendered = _render_export_line(spec=spec)

    testbase.assert_equal(rendered.startswith("/Users/Shared/share"), True)
    testbase.assert_equal("-ro" in rendered, True)
    testbase.assert_equal("-alldirs" in rendered, True)
    testbase.assert_equal("-mapall=nobody" in rendered, True)
    # Guard against regressions toward Linux syntax.
    testbase.assert_equal("(rw" in rendered, False)
    testbase.assert_equal("(ro" in rendered, False)
    return


def test_wildcard_host_expands_to_network_mask():
    """
        ``host="*"`` is rendered as ``-network 0.0.0.0 -mask 0.0.0.0``.
    """
    spec = MacosNfsExportSpec(
        path="/srv/share",
        clients=[MacosNfsClientSpec(host="*", options=["ro"])],
    )
    rendered = _render_export_line(spec=spec)
    testbase.assert_equal(
        "-network 0.0.0.0 -mask 0.0.0.0" in rendered, True,
    )
    testbase.assert_equal(" * " in rendered, False)
    return


def test_parse_exports_file_folds_wildcard_back():
    """
        ``-network 0.0.0.0 -mask 0.0.0.0`` parses back to ``host="*"``.
    """
    body = (
        "/Users/Shared/share -ro -alldirs -mapall=nobody "
        "-network 0.0.0.0 -mask 0.0.0.0\n"
    )
    parsed = _parse_exports_file(content=body)
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0].clients[0].host, "*")
    testbase.assert_equal("ro" in parsed[0].clients[0].options, True)
    return


def test_classify_darwin_tokens_handles_options_and_clients():
    """
        ``_classify_darwin_tokens`` separates dash-prefixed options
        from client hosts.
    """
    tokens = ["-ro", "-alldirs", "10.0.0.0", "-mask", "255.255.255.0"]
    options, clients = _classify_darwin_tokens(tokens=tokens)
    testbase.assert_equal(options, ["ro", "alldirs"])
    testbase.assert_equal(clients, ["10.0.0.0/255.255.255.0"])
    return


def test_parse_showmount_e_output_skips_header():
    """
        The ``Exports list on…`` header line is skipped.
    """
    text = (
        "Exports list on localhost:\n"
        "/Users/Shared/share              Everyone\n"
        "/srv/restricted                  10.0.0.0\n"
    )
    parsed = _parse_showmount_e_output(text=text)
    testbase.assert_equal(parsed["/Users/Shared/share"], ["Everyone"])
    testbase.assert_equal(parsed["/srv/restricted"], ["10.0.0.0"])
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_export_info_dataclass_carries_share_name_field():
    """
        The :class:`MacosNfsExportInfo` dataclass exposes the
        ``share_name`` field for cross-OS parity.
    """
    info = MacosNfsExportInfo(
        path="/Users/Shared/share",
        share_name=None,
        clients=[MacosNfsClientSpec()],
        is_active=False,
    )
    testbase.assert_equal(info.share_name, None)
    return


def test_export_status_dataclass_shape():
    """
        :class:`MacosNfsExportStatus` exposes path, is_active,
        client_count, flags.
    """
    status = MacosNfsExportStatus(
        path="/Users/Shared/share",
        is_active=False,
        client_count=0,
        flags=[],
    )
    testbase.assert_equal(status.path, "/Users/Shared/share")
    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return
