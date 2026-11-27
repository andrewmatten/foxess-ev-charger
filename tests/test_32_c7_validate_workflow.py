"""C7: Validate CI workflow grants contents: read to all jobs and checks out code in validate-hacs."""
from __future__ import annotations

import pathlib

import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "validate.yml"
)


def test_validate_workflow_permissions_and_checkout():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert doc["permissions"] == {"contents": "read"}
    jobs = doc["jobs"]
    assert {"validate-hacs", "validate-hassfest", "tests"} <= set(jobs)
    for job_name in ("validate-hacs", "validate-hassfest", "tests"):
        steps = jobs[job_name]["steps"]
        assert any(
            str(s.get("uses", "")).startswith("actions/checkout@") for s in steps
        ), f"{job_name} is missing actions/checkout"
        job_perms = jobs[job_name].get("permissions", doc["permissions"])
        assert job_perms.get("contents") == "read", job_name
