"""Stand-in for the real `prometheus_client` package -- only the names
orchestrator/main.py and gateway/main.py import at module level. See
langgraph/__init__.py (sibling stub) for why this exists."""


class _Metric:
    def __init__(self, *args, **kwargs):
        pass

    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        pass

    def dec(self, *args, **kwargs):
        pass

    def observe(self, *args, **kwargs):
        pass

    def set(self, *args, **kwargs):
        pass

    def time(self):
        class _Ctx:
            def __enter__(inner_self):
                return inner_self

            def __exit__(inner_self, *exc_info):
                return False

        return _Ctx()


Counter = _Metric
Histogram = _Metric
Gauge = _Metric
Summary = _Metric


def generate_latest(*args, **kwargs):
    return b""


CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
