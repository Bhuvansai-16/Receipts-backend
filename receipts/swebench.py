"""SWE-bench Verified instances. Never exposes test_patch, FAIL_TO_PASS or hints (hidden ground truth)."""
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from . import config

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


CACHE = config.ROOT / ".cache" / "swebench_verified.json"


@lru_cache(maxsize=1)
def _dataset() -> dict:
    """Rows by instance id. The first load comes from Hugging Face (15-30 s, it also checks the hub);
    after that from a local copy, so the server and CLI start fast."""
    if CACHE.is_file():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    from datasets import load_dataset

    rows = {r["instance_id"]: dict(r) for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    tmp.replace(CACHE)  # atomic: an interrupted write never leaves a half file behind
    return rows


def load_instance(instance_id: str) -> Instance:
    row = _dataset().get(instance_id)
    if row is None:
        raise ValueError(f"{instance_id} is not in SWE-bench Verified")
    if row["repo"] not in PYTEST_REPOS:
        raise ValueError(f"{row['repo']} does not use pytest; the MVP is pytest-only")
    return Instance(row["instance_id"], row["repo"], row["problem_statement"], row["patch"],
                    json.loads(row["PASS_TO_PASS"]))


def docker_ref(instance_id: str) -> str:
    """SWE-bench env image: repo at /testbed, conda env 'testbed'."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


async def base_image(instance_id: str):
    """Clean base checkpoint for the instance on the configured sandbox provider."""
    ref = docker_ref(instance_id)
    if config.SANDBOX_PROVIDER == "daytona":
        from . import daytona_backend

        return await daytona_backend.base_image(ref, "receipts-" + re.sub(r"[^a-z0-9]+", "-", instance_id.lower()))
    return await config.contree().images.oci(f"docker://docker.io/{ref}")  # reuses the preloaded env
