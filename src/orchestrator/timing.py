"""Timing utilities for granular execution tracking."""
import time
from typing import Dict, Any, Optional
from contextlib import contextmanager, asynccontextmanager


class TimingTracker:
    """Hierarchical timing tracker for request processing.

    Tracks execution time across three categories:
    - pre_graph: Initialization before state machine execution
    - graph: Per-node and sub-stage timing within the state machine
    - post_graph: Finalization after state machine completion

    Also tracks individual LLM calls with model, token count, and duration.
    """

    def __init__(self):
        self.start_time = time.time()
        self.timings: Dict[str, Any] = {
            "pre_graph": {},
            "graph": {},
            "post_graph": {},
        }
        self.llm_calls: list = []

    @contextmanager
    def track(self, category: str, stage: str):
        """Context manager to track execution time of a stage.

        Usage:
            with tracker.track("pre_graph", "session_management"):
                # code to time

        Args:
            category: One of "pre_graph", "graph", "post_graph"
            stage: Name of the stage being tracked
        """
        start = time.time()
        try:
            yield
        finally:
            duration_ms = int((time.time() - start) * 1000)
            if category not in self.timings:
                self.timings[category] = {}
            self.timings[category][f"{stage}_ms"] = duration_ms

    @asynccontextmanager
    async def track_async(self, category: str, stage: str):
        """Async context manager to track execution time of a stage.

        Usage:
            async with tracker.track_async("pre_graph", "memory_retrieval"):
                await some_async_operation()

        Args:
            category: One of "pre_graph", "graph", "post_graph"
            stage: Name of the stage being tracked
        """
        start = time.time()
        try:
            yield
        finally:
            duration_ms = int((time.time() - start) * 1000)
            if category not in self.timings:
                self.timings[category] = {}
            self.timings[category][f"{stage}_ms"] = duration_ms

    def track_sync(self, category: str, stage: str, duration_seconds: float):
        """Record timing for a stage that was already measured.

        Args:
            category: One of "pre_graph", "graph", "post_graph"
            stage: Name of the stage
            duration_seconds: Duration in seconds
        """
        if category not in self.timings:
            self.timings[category] = {}
        self.timings[category][f"{stage}_ms"] = int(duration_seconds * 1000)

    def track_substage(self, category: str, parent: str, substage: str, duration_seconds: float):
        """Record timing for a sub-stage within a parent stage.

        Creates nested structure: category.parent.substage_ms

        Args:
            category: One of "pre_graph", "graph", "post_graph"
            parent: Name of the parent stage (e.g., "classify", "retrieve")
            substage: Name of the sub-stage (e.g., "cache_check", "llm_inference")
            duration_seconds: Duration in seconds
        """
        if category not in self.timings:
            self.timings[category] = {}
        if parent not in self.timings[category]:
            self.timings[category][parent] = {}
        elif not isinstance(self.timings[category][parent], dict):
            # Convert flat value to nested dict if needed
            existing = self.timings[category][parent]
            self.timings[category][parent] = {"total_ms": existing}
        self.timings[category][parent][f"{substage}_ms"] = int(duration_seconds * 1000)

    def record_llm_call(
        self,
        stage: str,
        model: str,
        tokens: int,
        duration_ms: int,
        call_type: str = "inference"
    ):
        """Record an individual LLM inference call.

        Args:
            stage: Which pipeline stage made the call (e.g., "classify", "synthesize")
            model: Model name used (e.g., "qwen2.5:3b", "llama3.1:8b")
            tokens: Number of tokens generated
            duration_ms: Duration in milliseconds
            call_type: Type of call (e.g., "inference", "tool_selection", "synthesis")
        """
        self.llm_calls.append({
            "stage": stage,
            "model": model,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "call_type": call_type
        })

    def _calculate_nested_total(self, data: Dict[str, Any]) -> int:
        """Calculate total from nested timing dict."""
        total = 0
        for key, value in data.items():
            if key == "total_ms":
                continue
            if isinstance(value, dict):
                total += self._calculate_nested_total(value)
            elif isinstance(value, (int, float)):
                total += int(value)
        return total

    def finalize(self) -> Dict[str, Any]:
        """Calculate totals and return final timing structure.

        Returns:
            Dict with hierarchical timing data including:
            - total_ms: Total request time
            - pre_graph: Pre-graph stage timings with total
            - graph: Graph node timings with sub-stage breakdowns
            - post_graph: Post-graph stage timings with total
            - llm_calls: List of individual LLM calls
        """
        total_ms = int((time.time() - self.start_time) * 1000)

        # Calculate category totals
        for category in ["pre_graph", "graph", "post_graph"]:
            if category in self.timings and self.timings[category]:
                cat_total = 0
                for key, value in list(self.timings[category].items()):
                    if key == "total_ms":
                        continue
                    if isinstance(value, dict):
                        # Nested stage - calculate its total
                        stage_total = self._calculate_nested_total(value)
                        if "total_ms" not in value:
                            value["total_ms"] = stage_total
                        cat_total += stage_total
                    elif isinstance(value, (int, float)):
                        cat_total += int(value)
                self.timings[category]["total_ms"] = cat_total

        return {
            "total_ms": total_ms,
            "pre_graph": self.timings.get("pre_graph", {}),
            "graph": self.timings.get("graph", {}),
            "post_graph": self.timings.get("post_graph", {}),
            "llm_calls": self.llm_calls
        }

    def get_summary(self) -> str:
        """Get a human-readable summary of timings.

        Returns:
            Formatted string with timing breakdown
        """
        data = self.finalize()
        lines = [f"Total: {data['total_ms']}ms"]

        for category in ["pre_graph", "graph", "post_graph"]:
            cat_data = data.get(category, {})
            if cat_data:
                cat_total = cat_data.get("total_ms", 0)
                lines.append(f"  {category}: {cat_total}ms")
                for key, value in cat_data.items():
                    if key == "total_ms":
                        continue
                    if isinstance(value, dict):
                        stage_total = value.get("total_ms", 0)
                        lines.append(f"    {key}: {stage_total}ms")
                    else:
                        lines.append(f"    {key}: {value}ms")

        if data["llm_calls"]:
            lines.append(f"  LLM calls: {len(data['llm_calls'])}")
            for call in data["llm_calls"]:
                lines.append(f"    - {call['stage']}/{call['model']}: {call['duration_ms']}ms ({call['tokens']} tokens)")

        return "\n".join(lines)
