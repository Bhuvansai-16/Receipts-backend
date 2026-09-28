"""SWE-bench Verified instances. Never exposes test_patch, FAIL_TO_PASS or hints (hidden ground truth)."""
import json
from dataclasses import dataclass
from functools import lru_cache

# Repos whose SWE-bench harness runs pytest. django (runtests.py) and sympy (bin/test) are out of MVP scope.
PYTEST_REPOS = {
    "astropy/astropy", "matplotlib/matplotlib", "mwaskom/seaborn", "pallets/flask", "psf/requests",
    "pydata/xarray", "pylint-dev/pylint", "pytest-dev/pytest", "scikit-learn/scikit-learn", "sphinx-doc/sphinx",
}


@dataclass
class Instance:
    instance_id: str
    repo: str
    problem_statement: str
    gold_patch: str
    pass_to_pass: list[str]


@lru_cache(maxsize=1)
def _dataset() -> dict:
    from datasets import load_dataset

    return {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")}


def load_instance(instance_id: str) -> Instance:
    row = _dataset().get(instance_id)
    if row is None:
        raise ValueError(f"{instance_id} is not in SWE-bench Verified")
    if row["repo"] not in PYTEST_REPOS:
        raise ValueError(f"{row['repo']} does not use pytest; the MVP is pytest-only")
    return Instance(row["instance_id"], row["repo"], row["problem_statement"], row["patch"],
                    json.loads(row["PASS_TO_PASS"]))


async def swe_image(sdk, instance_id: str):
    """Preloaded SWE-bench env image (repo at /testbed, conda env 'testbed'). oci() reuses it if already imported."""
    name = instance_id.replace("__", "_1776_").lower()
    return await sdk.images.oci(f"docker://docker.io/swebench/sweb.eval.x86_64.{name}:latest")
