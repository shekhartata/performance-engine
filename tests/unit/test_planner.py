from perf_envelope.config.loader import ProjectLoader
from perf_envelope.experiment.planner import ExperimentPlanner


def test_example_plan_cell_count(example_project):
    loader = ProjectLoader(example_project)
    resolved = loader.resolve_experiment("scale")
    plan = ExperimentPlanner(resolved).plan_dict()
    # 3 N × 2 selectivity × 2 concurrency × 2 cache = 24 coarse cells
    assert plan["coarse_cells"] == 24
    assert plan["models"] == ["embedded", "referenced"]
    assert plan["repetitions"] == 2
