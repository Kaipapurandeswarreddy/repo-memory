"""New modules: sanitize, recall/scoring, export/snapshot, eval audit, distill_llm fallback."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repomemory import sanitize as sz
from repomemory.distiller import distill_with_llm
from repomemory.eval_harness import audit_tasks
from repomemory.llm import LLMGateway
from repomemory.store import MemoryStore, compute_score


def test_sanitize_four_layers():
    secrets = {"API_KEY": "supersecret123"}
    text = ('ApiCall GET https://api.x.com?apikey=supersecret123&q=hi '
            '{"api_key": "abc12345"} "sk-abcdefgh123456"')
    out = sz.sanitize_secrets(text, secrets)
    assert "supersecret123" not in out
    assert "{{API_KEY}}" in out or "{{APIKEY}}" in out or "apikey={{APIKEY}}" in out
    assert sz.resolve_secrets("hi {{API_KEY}}", secrets) == "hi supersecret123"


def test_compute_score_correction_boost():
    base = compute_score(tfidf=2.0, access_count=0)
    corr = compute_score(tfidf=2.0, access_count=0, is_correction=True)
    assert corr == base * 2.0
    assert compute_score(tfidf=0.0, access_count=0) == 0.0


def test_recall_and_export_snapshot(tmp_path):
    store = MemoryStore.open(tmp_path / ".repomemory")
    store.save_history("a.py", [{"sha": "abc1234", "kind": "BUGFIX",
                                 "summary": "duplicate refunds guard",
                                 "why": "gateway retry", "confidence": "high",
                                 "evidence": {"sha": "abc", "quote": "x"}}])
    res = store.recall("duplicate refunds", max_items=5)
    assert res["items"] and res["items"][0]["file"] == "a.py"
    assert "duplicate" in res["formatted"].lower()
    data = store.export()
    assert data["version"] == 1
    snap = store.snapshot("t")
    assert snap["id"] in store.list_snapshots()
    assert store.restore(snap["id"]) is True
    assert store.verify()["valid"] is True


def test_audit_flags():
    tasks = [{"id": "t1", "fail_to_pass": ["test_x[w/"],
              "gold_patch": "Z" * 200, "problem": "P" * 50 + "Z" * 200},
             {"id": "t2", "fail_to_pass": [f"t{i}" for i in range(60)]}]
    res = audit_tasks(tasks)
    assert res["flagged"] == 2
    assert any("broken-tests" in r for r in res["flags"]["t1"])
    assert any("broad" in r for r in res["flags"]["t2"])


def test_distill_llm_fallback():
    gw = LLMGateway(mode="stub")
    note = distill_with_llm({"sha": "abc", "msg": "fix: duplicate refunds guard",
                             "diff": "+if amount > 10000:\n+    raise X", "files": ["p.py"]}, gw)
    assert note["kind"] == "BUGFIX"
    note2 = distill_with_llm({"sha": "x", "msg": "fix stuff"}, None)
    assert note2["confidence"] == "low"


def test_recall_best_note_wins(tmp_path):
    store = MemoryStore.open(tmp_path / ".repomemory")
    vague = [{"sha": f"v{i}", "kind": "FEATURE", "summary": "general typing cleanup",
              "why": "typing", "confidence": "high",
              "evidence": {"sha": f"v{i}", "quote": "typing"}} for i in range(5)]
    store.save_history("vague.py", vague)
    store.save_history("exact.py", [{"sha": "abc1234", "kind": "BUGFIX",
                                     "summary": "Prevent Response self-reference in redirect history",
                                     "why": "redirect history", "confidence": "low",
                                     "evidence": {"sha": "abc1234", "quote": "redirect"},
                                     "fix_lines": ["resp.history = hist[:]"]}])
    res = store.recall("redirect history self reference", max_items=5)
    assert res["items"] and res["items"][0]["file"] == "exact.py"
    assert "hist[:]" in res["formatted"]


def test_check_utf16_diff_parses():
    from repomemory.verifier import check_patch
    patch = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
             "@@ -1,3 +1,2 @@\n-    if amount > 10000:\n-        raise ValueError(1)\n+    pass\n")
    raw_utf16 = patch.encode("utf-16")  # PowerShell `>` redirection
    card = {"file": "x.py", "history": [
        {"sha": "abc1234", "kind": "BUGFIX", "summary": "dup refunds",
         "risk_flag": {"lines_hint": "L1", "text": "DO NOT REMOVE"}}]}
    findings = check_patch(raw_utf16, card, [])
    assert any(f["type"] == "guard-removed" and f["severity"] == "high"
               for f in findings)


def test_check_unparsed_diff_warns_not_clean():
    from repomemory.verifier import check_patch, format_report
    findings = check_patch("definitely not a diff \x00\x01\x02", {}, [])
    assert len(findings) == 1 and findings[0]["type"] == "unparsed-diff"
    assert "No issues found" not in format_report(findings)
    assert check_patch("   \n  ", {}, []) == []


def test_profiler_records_calls():
    from repomemory import profiler as prof
    src = ("import gateway_client\n\n"
           "def process_refund(order_id):\n"
           "    return gateway_client.refund(order_id)\n")
    p = prof.profile_source(src, "payments.py")
    names = [c["name"] for c in p["calls"]]
    assert "gateway_client.refund" in names
    assert all(c["lineno"] == 4 and c["in"] == "process_refund" for c in p["calls"])


def test_calls_edges_and_callers():
    from repomemory import graph_builder as gb
    profiles = {
        "orders.py": {"imports": ["payments"],
                      "functions": [], "classes": [],
                      "calls": [{"name": "process_refund", "lineno": 41,
                                 "in": "<module>"}]},
        "payments.py": {"imports": [], "functions": [{"name": "process_refund"}],
                        "classes": [], "calls": []},
    }
    g = gb.build_graph_data(profiles)
    kinds = {(e[0], e[1], e[2]) for e in g["edges"]}
    assert ("orders.py", "payments.py", "IMPORTS") in kinds
    assert ("orders.py", "payments.py", "CALLS") in kinds
    sites = gb.function_callers(g, profiles, "payments.py", "process_refund")
    assert sites == [{"file": "orders.py", "line": 41, "in": "<module>"}]
    assert gb.function_callers(g, profiles, "payments.py", "nope") == []


def test_verifier_cites_call_sites():
    from repomemory import verifier as ver
    patch = ("diff --git a/payments.py b/payments.py\n--- a/payments.py\n+++ b/payments.py\n"
             "@@ -1,2 +1,2 @@\n"
             "-def process_refund(order_id: str):\n"
             "+def process_refund(order_id: int):\n")
    card = {"file": "payments.py", "dependents": ["orders.py"],
            "history": [], "profile": {}, "decisions": [], "dependencies": []}
    dep = {"file": "orders.py", "dependents": [], "history": [],
           "profile": {"calls": [{"name": "process_refund", "lineno": 41,
                                  "in": "<module>"}]},
           "decisions": [], "dependencies": []}
    findings = ver.check_patch(patch, card, [dep])
    cc = [f for f in findings if f["type"] == "contract-change"]
    assert cc and "orders.py:41" in cc[0]["message"]
