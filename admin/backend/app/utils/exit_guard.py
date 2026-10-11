"""Stop the event loop's background tasks before the interpreter finalizes.

admin-backend runs several loops that log (memory-vector maintenance, health polling,
telemetry, calendar sync, the conversation retention purge). Under uvicorn the lifespan
shutdown stops them. A process that ends without running shutdown (a startup check
driven from a script, a crash handler, a test harness) leaves them running in a daemon
thread. When the interpreter then begins to exit, ``concurrent.futures`` closes its
executors, a loop's next ``asyncio.to_thread`` fails ("cannot schedule new futures after
shutdown"), the loop logs that to stdout, and the main thread's own flush of stdout races
that write: CPython aborts with "_enter_buffered_busy: could not acquire lock for
<stdout> at interpreter shutdown, possibly due to daemon threads" (exit status -6).

``stop_loop_tasks_at_exit`` registers one hook that cancels every task on the loop and
waits for them (bounded), so nothing is left to log. It uses ``threading._register_atexit``
because those hooks run at the start of ``threading._shutdown``, last-registered first, so
this one runs before ``concurrent.futures`` closes its executors; plain ``atexit`` runs
after the threads are already being joined, which is too late. Where that private hook is
missing it falls back to ``atexit``.
"""
from __future__ import annotations

import asyncio
import atexit
import threading
from typing import Optional

STOP_TIMEOUT_S = 2.0

_registered_loop: Optional[asyncio.AbstractEventLoop] = None


async def _cancel_everything_else() -> None:
    me = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not me]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def stop_tasks_now(loop: asyncio.AbstractEventLoop, timeout: float = STOP_TIMEOUT_S) -> bool:
    """Cancel all tasks on ``loop`` from another thread and wait; ``False`` if it couldn't."""
    if loop.is_closed() or not loop.is_running():
        return False
    future = asyncio.run_coroutine_threadsafe(_cancel_everything_else(), loop)
    try:
        future.result(timeout)
    except BaseException:
        return False
    return True


def stop_loop_tasks_at_exit(loop: asyncio.AbstractEventLoop) -> None:
    """Register (once per loop) the exit hook described in the module docstring."""
    global _registered_loop
    if _registered_loop is loop:
        return
    _registered_loop = loop

    def hook() -> None:
        stop_tasks_now(loop)

    register = getattr(threading, "_register_atexit", None)
    if register is not None:
        try:
            register(hook)
            return
        except RuntimeError:  # the interpreter is already shutting down
            return
    atexit.register(hook)
