"""Smoke: profiler + graph blast-radius + store roundtrip + verifier guard-removed.

Covers GROUP C wiring against the canonical Group A/B APIs:
  profiler.profile_file / graph_builder.build_graph_data+blast_radius /
  store roundtrip+get_card / verifier.check_patch(patch, card, dependents) /
  orchestrator.run_generate with the stub gateway (zero keys, zero network).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repomemory import graph_builder as gb
from repomemory import profiler as prof
from repomemory import verifier as ver
from repomemory.llm import LLMGateway
from repomemory.orchestrator import Orchestrator
from repomemory.store import MemoryStore

SAMPLE = '''"""sample payments module."""
import gateway_client

MAX_RETRIES = 3

def process_refund(order_id: str, amount: float) -> dict:
    if amount > 10000:
        # guard against duplicate refunds on gateway retry (see c1a9f3)
        raise ValueError("needs manual review")
    return gateway_client.refund(order_id, amount)
'''


def test_profiler_on_sample_file(tmp_path):
    f = tmp_path / "payments.py"
    f.write_text(SAMPLE, encoding="utf-8")
    p = prof.profile_file(f, tmp_path)
    assert p["file"] == "payments.py"
    names = [fn["name"] for fn in p.get("functions", [])]
    assert "process_refund" in names
    assert any("gateway_client" in i for i in p.get("imports", []))


def test_graph_blast_radius():
    profiles = {
        "orders.py": {"imports": ["payments"]},
        "billing.py": {"imports": ["payments"]},
        "payments.py": {"imports": ["gateway_client"]},
        "gateway_client.py": {"imports": []},
    }
    g = gb.build_graph_data(profiles)
    br = gb.blast_radius(g, "payments.py")
    assert "orders.py" in br and "billing.py" in br
    assert gb.blast_radius(g, "orders.py") == []
    assert gb.find_cycles({"nodes": ["a"], "edges": []}) == []


def test_store_roundtrip(tmp_path):
    store = MemoryStore.open(tmp_path / ".repomemory")
    store.save_profile("payments.py", {"file": "payments.py", "functions": [], "sha": "abc"})
    store.save_history("payments.py", [{"sha": "c1a9f3", "kind": "BUGFIX", "summary": "x"}])
    assert store.load_profile("payments.py")["sha"] == "abc"
    card = store.get_card("payments.py")
    assert card["file"] == "payments.py" and len(card["history"]) == 1
    assert "payments.py" in store.list_files()


def test_verifier_guard_removed():
    patch = (
        "diff --git a/payments.py b/payments.py\n"
        "--- a/payments.py\n+++ b/payments.py\n"
        "@@ -1,4 +1,3 @@\n"
        "-    if amount > 10000:\n"
        "-        raise ValueError(\"needs manual review\")\n"
        "     return gateway_client.refund(order_id, amount)\n"
    )
    card = {"file": "payments.py", "dependents": ["orders.py"],
            "history": [{"sha": "c1a9f3", "kind": "BUGFIX",
                         "summary": "dup refunds >10k on retry",
                         "risk_flag": {"lines_hint": "L80-90",
                                       "text": "guard if amount > 10000 DO NOT REMOVE"},
                         "confidence": "high"}],
            "profile": {}, "decisions": [], "dependencies": []}
    findings = ver.check_patch(patch, card, [])
    assert any(f.get("type") == "guard-removed" and f.get("severity") == "high"
               and (f.get("sha") or "").startswith("c1a9f3") for f in findings)


def test_orchestrator_stub_generate(tmp_path):
    store = MemoryStore.open(tmp_path / ".repomemory")
    (tmp_path / "payments.py").write_text(SAMPLE, encoding="utf-8")
    store.save_profile("payments.py", prof.profile_file(tmp_path / "payments.py", tmp_path))
    store.save_history("payments.py", [{"sha": "c1a9f3", "kind": "BUGFIX",
                                        "summary": "dup refunds", "confidence": "high"}])
    store.save_graph(gb.build_graph_data({"payments.py": store.load_profile("payments.py")}))
    orch = Orchestrator(store, LLMGateway(mode="stub"), repo_root=tmp_path)
    res = orch.run_generate("add retry limit", "payments.py")
    assert "patch" in res and "warnings" in res and "tokens" in res
    assert res["tokens"]["tokens_in"] > 0
    assert isinstance(res["warnings"], list)
