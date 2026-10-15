"""RepoMemory — history-aware per-file memory for code generation.

Canonical modules (use these):
  profiler, history_miner (miner.py is a compat wrapper), distiller,
  graph_builder (dict-based, CLI/orchestrator path; depgraph.py is the
  optional networkx advanced view), store, injector, verifier,
  orchestrator, llm, updater, sanitize, mcp_server, http_server, viewer, hooks, eval_harness.
"""

from .depgraph import blast_radius, build_graph, detect_cycles, load_graph, save_graph, to_json
from .graph_builder import blast_radius as blast_radius_data
from .graph_builder import build_graph_data, dependencies, dependents, find_cycles, one_hop
from .profiler import extract_profile, profile_all
from .store import MemoryStore

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "MemoryStore",
    "extract_profile",
    "profile_all",
    "build_graph",
    "blast_radius",
    "detect_cycles",
    "to_json",
    "save_graph",
    "load_graph",
    "build_graph_data",
    "blast_radius_data",
    "dependencies",
    "dependents",
    "find_cycles",
    "one_hop",
]
