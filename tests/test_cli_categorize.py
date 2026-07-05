from typer.testing import CliRunner

from bankapp.cli import app
from tests.conftest import FIXTURES

runner = CliRunner()


def _ingest_visa(app_env):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["ingest", str(FIXTURES / "td_visa_jan.qfx")])


def test_categorize_workflow(app_env):
    _ingest_visa(app_env)
    # visa has NETFLIX, TIM HORTONS, PAYMENT - all uncategorized initially
    r = runner.invoke(app, ["review", "count"])
    assert r.output.strip() == "3"

    runner.invoke(app, ["rules", "add", "--kind", "substring", "--pattern", "netflix",
                        "--category", "subscriptions", "--source", "claude"])
    r = runner.invoke(app, ["categorize"])
    assert "Categorized 1" in r.output
    assert runner.invoke(app, ["review", "count"]).output.strip() == "2"


def test_rules_add_invalid_regex(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["rules", "add", "--kind", "regex", "--pattern", "([bad", "--category", "x"])
    assert r.exit_code == 1
    assert "Invalid rule" in r.output


def test_rules_add_duplicate_noop(app_env):
    runner.invoke(app, ["init"])
    runner.invoke(app, ["rules", "add", "--pattern", "netflix", "--category", "subscriptions"])
    r = runner.invoke(app, ["rules", "add", "--pattern", "netflix", "--category", "subscriptions"])
    assert "already exists" in r.output


def test_rules_list_shows_seeds(app_env):
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["rules", "list"])
    assert "tfr-to" in r.output
    assert "transfer" in r.output


def test_review_export_markdown(app_env):
    _ingest_visa(app_env)
    r = runner.invoke(app, ["review", "export", "--format", "markdown"])
    assert "# Review queue" in r.output
    assert "netflix" in r.output
