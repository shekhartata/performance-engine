from typer.testing import CliRunner

from perf_envelope.cli.app import app

runner = CliRunner()


def test_validate_example(example_project):
    result = runner.invoke(app, ["validate", "--project", str(example_project)])
    assert result.exit_code == 0, result.output
    assert "scale" in result.output


def test_plan_example(example_project):
    result = runner.invoke(
        app, ["plan", "--project", str(example_project), "--experiment", "scale"]
    )
    assert result.exit_code == 0, result.output
    assert "coarse_cells" in result.output


def test_validate_spec(example_project):
    result = runner.invoke(app, ["validate", "--spec", str(example_project / "target.yaml")])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_ui_help():
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0, result.output
    assert "web UI" in result.output
