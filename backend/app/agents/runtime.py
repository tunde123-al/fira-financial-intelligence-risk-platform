"""Explicit state-graph runtime for the investigation workflow.

The workflow is declared once (nodes, edges, conditional routers) in
`app.agents.workflow`. It can be compiled to:

* LangGraph (`langgraph.graph.StateGraph`) when the `langgraph` package is
  installed — the default in the Docker image and CI; or
* `MiniStateGraph`, a small dependency-free executor with the same semantics
  (each node returns a partial state update that overwrites keys; conditional
  edges pick the next node from the state; a step limit prevents cycles from
  running away). It exists so the agent can run in minimal environments and in
  unit tests without the LangGraph dependency chain.

Both engines execute the same node functions, so behaviour is identical.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

END = "__end__"

NodeFn = Callable[[dict[str, Any]], dict[str, Any]]
Router = Callable[[dict[str, Any]], str]


@dataclass
class WorkflowSpec:
    entry: str
    nodes: dict[str, NodeFn] = field(default_factory=dict)
    edges: dict[str, str] = field(default_factory=dict)
    conditional: dict[str, tuple[Router, dict[str, str]]] = field(default_factory=dict)

    def node(self, name: str, fn: NodeFn) -> WorkflowSpec:
        self.nodes[name] = fn
        return self

    def edge(self, a: str, b: str) -> WorkflowSpec:
        self.edges[a] = b
        return self

    def branch(self, a: str, router: Router, mapping: dict[str, str]) -> WorkflowSpec:
        self.conditional[a] = (router, mapping)
        return self

    def describe(self) -> dict[str, Any]:
        return {"entry": self.entry, "nodes": list(self.nodes), "edges": self.edges,
                "conditional": {k: v[1] for k, v in self.conditional.items()}}


class StepLimitExceeded(RuntimeError):
    pass


class MiniStateGraph:
    engine = "mini-state-graph"

    def __init__(self, spec: WorkflowSpec, max_steps: int = 50):
        self.spec = spec
        self.max_steps = max_steps
        for a, b in spec.edges.items():
            assert a in spec.nodes and (b in spec.nodes or b == END), f"bad edge {a}->{b}"
        for a, (_, mapping) in spec.conditional.items():
            assert a in spec.nodes and all(t in spec.nodes or t == END for t in mapping.values())

    def invoke(self, state: dict[str, Any], on_step: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        state = dict(state)
        current = self.spec.entry
        trace: list[str] = []
        for _ in range(self.max_steps):
            if current == END:
                state["node_trace"] = trace
                return state
            trace.append(current)
            update = self.spec.nodes[current](state) or {}
            state.update(update)
            if on_step:
                on_step(current, state)
            if current in self.spec.conditional:
                router, mapping = self.spec.conditional[current]
                current = mapping[router(state)]
            else:
                current = self.spec.edges.get(current, END)
        raise StepLimitExceeded(f"workflow exceeded {self.max_steps} steps: {trace[-10:]}")


class LangGraphAdapter:  # pragma: no cover - exercised in CI where langgraph is installed
    engine = "langgraph"

    def __init__(self, spec: WorkflowSpec, state_schema: type, max_steps: int = 50):
        from langgraph.graph import END as LG_END
        from langgraph.graph import StateGraph

        g: Any = StateGraph(state_schema)
        for name, fn in spec.nodes.items():
            g.add_node(name, _tracing(name, fn))
        g.set_entry_point(spec.entry)
        for a, b in spec.edges.items():
            g.add_edge(a, LG_END if b == END else b)
        for a, (router, mapping) in spec.conditional.items():
            g.add_conditional_edges(a, router, {k: (LG_END if v == END else v) for k, v in mapping.items()})
        self.graph = g.compile()
        self.max_steps = max_steps

    def invoke(self, state: dict[str, Any], on_step: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        out = self.graph.invoke({**state, "node_trace": []}, config={"recursion_limit": self.max_steps})
        return dict(out)


def _tracing(name: str, fn: NodeFn) -> NodeFn:
    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        upd = fn(state) or {}
        return {**upd, "node_trace": list(state.get("node_trace") or []) + [name]}
    return wrapped


def compile_workflow(spec: WorkflowSpec, state_schema: type, prefer: str = "auto", max_steps: int = 50):
    if prefer in ("auto", "langgraph"):
        try:
            return LangGraphAdapter(spec, state_schema, max_steps)
        except ImportError:
            if prefer == "langgraph":
                raise
    return MiniStateGraph(spec, max_steps)
