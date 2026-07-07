from typer.testing import CliRunner

from bankapp.cli import app

runner = CliRunner()


def test_advice_add_from_file_then_show_and_list(app_env, tmp_path):
    runner.invoke(app, ["init"])
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("Here is the coaching text.\nMore detail.")

    r = runner.invoke(
        app,
        ["advice", "add", "--file", str(brief_file), "--as-of", "2026-07-06", "--source", "claude"],
    )
    assert r.exit_code == 0, r.output
    assert "Brief #1 saved" in r.output

    show = runner.invoke(app, ["advice", "show"])
    assert show.exit_code == 0, show.output
    assert "Here is the coaching text." in show.output

    listed = runner.invoke(app, ["advice", "list"])
    assert listed.exit_code == 0, listed.output
    assert "#1" in listed.output
    assert "2026-07-06" in listed.output


def test_advice_add_from_stdin(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["advice", "add", "--as-of", "2026-07-06"], input="hello brief")
    assert r.exit_code == 0, r.output
    assert "Brief #1 saved" in r.output

    show = runner.invoke(app, ["advice", "show"])
    assert show.exit_code == 0, show.output
    assert "hello brief" in show.output


def test_advice_add_bad_source_rejected(app_env, tmp_path):
    runner.invoke(app, ["init"])
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("content")

    r = runner.invoke(
        app,
        ["advice", "add", "--file", str(brief_file), "--source", "bogus"],
    )
    assert r.exit_code != 0


def test_advice_show_empty_db(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["advice", "show"])
    assert r.exit_code == 0, r.output
    assert "No briefs yet." in r.output


def test_advice_list_empty_db(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["advice", "list"])
    assert r.exit_code == 0, r.output
    assert "No briefs yet." in r.output
