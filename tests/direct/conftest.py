import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = str(ROOT / "contracts" / "accessbond.py")
FIX = ROOT / "tests" / "fixtures"
URL = "https://bakery.example/order"
GEN = 10**18


def html(name: str) -> str:
    return (FIX / f"{name}.html").read_text()


def llm_json(*verdicts: bool) -> str:
    return json.dumps({"results": [{"id": i + 1, "pass": v, "reason": "mock"} for i, v in enumerate(verdicts)]})


@pytest.fixture
def bond(direct_deploy):
    return direct_deploy(CONTRACT)


@pytest.fixture
def serve(direct_vm):
    """serve('broken') -> mock the bounty URL with that fixture (replaces previous mocks)."""

    def _serve(name: str, *llm_verdicts: bool):
        direct_vm.clear_mocks()
        direct_vm.mock_web(r"bakery\.example", {"status": 200, "body": html(name)})
        if llm_verdicts:
            direct_vm.mock_llm(r"accessibility auditor", llm_json(*llm_verdicts))

    return _serve
