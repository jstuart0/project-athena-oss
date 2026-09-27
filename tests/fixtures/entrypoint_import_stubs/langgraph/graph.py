"""Stand-in for `langgraph.graph` -- only the two names main.py imports at
module level (`StateGraph`, `END`). See package __init__.py for why this
exists."""

END = "END"


class StateGraph:
    def __init__(self, *args, **kwargs):
        pass

    def add_node(self, *args, **kwargs):
        pass

    def add_edge(self, *args, **kwargs):
        pass

    def add_conditional_edges(self, *args, **kwargs):
        pass

    def set_entry_point(self, *args, **kwargs):
        pass

    def compile(self, *args, **kwargs):
        return self
