"""Wiring of `cgc index --repo-init` to initialize_repository."""
import io
from unittest.mock import AsyncMock, MagicMock, patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from codegraphcontext.cli import cli_helpers
from codegraphcontext.cli import main as cli_main
from codegraphcontext.cli.main import app
from codegraphcontext.tools.indexing.repo_init import RepoAlreadyIndexedError

runner = CliRunner()


def _services():
    db_manager = MagicMock()
    graph_builder = MagicMock()
    code_finder = MagicMock()
    ctx = MagicMock(cgcignore_path=None)
    return db_manager, graph_builder, code_finder, ctx


def test_index_repo_init_flag_calls_repo_init_helper(tmp_path):
    with patch.object(cli_main, "repo_init_helper") as repo_init, \
         patch.object(cli_main, "index_helper") as index_helper, \
         patch.object(cli_main, "_load_credentials"):
        result = runner.invoke(app, ["index", "--repo-init", str(tmp_path)])

    assert result.exit_code == 0
    repo_init.assert_called_once()
    assert repo_init.call_args.args[0] == str(tmp_path)
    index_helper.assert_not_called()


def test_index_without_flag_keeps_standard_path(tmp_path):
    with patch.object(cli_main, "repo_init_helper") as repo_init, \
         patch.object(cli_main, "index_helper") as index_helper, \
         patch.object(cli_main, "_load_credentials"):
        result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 0
    index_helper.assert_called_once()
    repo_init.assert_not_called()


def test_index_repo_init_rejects_force(tmp_path):
    with patch.object(cli_main, "repo_init_helper") as repo_init, \
         patch.object(cli_main, "reindex_helper") as reindex, \
         patch.object(cli_main, "_load_credentials"):
        result = runner.invoke(app, ["index", "--repo-init", "--force", str(tmp_path)])

    assert result.exit_code != 0
    repo_init.assert_not_called()
    reindex.assert_not_called()


def test_repo_init_helper_success_closes_driver(tmp_path):
    services = _services()
    out = io.StringIO()
    with patch.object(cli_helpers, "_initialize_services", return_value=services), \
         patch.object(cli_helpers, "console", Console(file=out, width=200)), \
         patch.object(cli_helpers, "initialize_repository", new=AsyncMock(return_value={})) as init:
        cli_helpers.repo_init_helper(str(tmp_path))

    init.assert_awaited_once()
    assert init.call_args.args[0] is services[1]
    services[0].close_driver.assert_called_once()


def test_repo_init_helper_already_indexed_prints_and_exits_nonzero(tmp_path):
    services = _services()
    message = f"{tmp_path.resolve().as_posix()} is already in the graph. Use 'cgc index' to re-index it."
    out = io.StringIO()
    with patch.object(cli_helpers, "_initialize_services", return_value=services), \
         patch.object(cli_helpers, "console", Console(file=out, width=400)), \
         patch.object(cli_helpers, "initialize_repository",
                      side_effect=RepoAlreadyIndexedError(message)):
        try:
            cli_helpers.repo_init_helper(str(tmp_path))
        except typer.Exit as exc:
            assert exc.exit_code == 1
        else:
            raise AssertionError("repo_init_helper did not exit")

    assert "already in the graph" in out.getvalue()
    services[0].close_driver.assert_called_once()


def test_index_repo_init_already_indexed_exits_nonzero_via_cli(tmp_path):
    services = _services()
    out = io.StringIO()
    with patch.object(cli_helpers, "_initialize_services", return_value=services), \
         patch.object(cli_helpers, "console", Console(file=out, width=400)), \
         patch.object(cli_helpers, "initialize_repository",
                      side_effect=RepoAlreadyIndexedError("already in the graph")), \
         patch.object(cli_main, "_load_credentials"):
        result = runner.invoke(app, ["index", "--repo-init", str(tmp_path)])

    assert result.exit_code == 1
    assert "already in the graph" in out.getvalue()
