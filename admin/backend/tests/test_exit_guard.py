"""A process that exits without running shutdown must not abort in the interpreter's finalizer.

admin-backend's background loops log from a daemon thread. If the process ends while one is
mid-log, CPython aborts ("_enter_buffered_busy: could not acquire lock for <stdout> at
interpreter shutdown", exit status -6). ``app.utils.exit_guard`` cancels the loop's tasks
before the interpreter finalizes.
"""
import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.utils import exit_guard

BACKEND = Path(__file__).resolve().parents[1]

# A loop in a daemon thread whose task logs to stdout as fast as it can, like a maintenance
# loop that fails every tick once the executor is gone. The process then exits without
# stopping it. GUARD toggles the exit hook.
CHILD = r"""
import asyncio, importlib.util, sys, threading

spec = importlib.util.spec_from_file_location("exit_guard", EXIT_GUARD)  # no app import: fast
exit_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exit_guard)

loop = asyncio.new_event_loop()
ready = threading.Event()

async def spin():
    ready.set()
    try:
        while True:
            print("memory_vector_tick_failed error=cannot schedule new futures after shutdown " * 8, flush=True)
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        print("SPIN-CANCELLED", flush=True)
        raise

def run():
    asyncio.set_event_loop(loop)
    loop.create_task(spin())
    loop.run_forever()

threading.Thread(target=run, daemon=True).start()
ready.wait(5)
if GUARD:
    exit_guard.stop_loop_tasks_at_exit(loop)
"""


def _run_child(guard: bool):
    script = f"GUARD = {guard}\nEXIT_GUARD = {str(Path(exit_guard.__file__))!r}\n" + CHILD
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=BACKEND, timeout=60)


def test_with_the_exit_hook_the_spinning_logger_is_cancelled_before_exit_and_the_interpreter_does_not_abort():
    outcomes = [_run_child(True) for _ in range(20)]
    assert [o.returncode for o in outcomes] == [0] * 20
    assert all("SPIN-CANCELLED" in o.stdout for o in outcomes), "the task was stopped by the hook, not by luck"


def test_without_the_hook_the_logger_is_still_running_when_the_interpreter_finalizes():
    """The control for the test above: same child, no hook. It is never cancelled (and in the real
    app that is the state in which about one startup check in six aborts with status -6)."""
    outcomes = [_run_child(False) for _ in range(3)]
    assert all("SPIN-CANCELLED" not in o.stdout for o in outcomes)


def test_stop_tasks_now_cancels_every_task_and_waits_for_them():
    loop = asyncio.new_event_loop()
    cancelled = []

    async def forever(name):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    def run():
        asyncio.set_event_loop(loop)
        loop.create_task(forever("a"))
        loop.create_task(forever("b"))
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.1)
    try:
        assert exit_guard.stop_tasks_now(loop) is True
        assert sorted(cancelled) == ["a", "b"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_stop_tasks_now_is_a_no_op_for_a_loop_that_is_not_running():
    loop = asyncio.new_event_loop()
    try:
        assert exit_guard.stop_tasks_now(loop) is False
    finally:
        loop.close()
    assert exit_guard.stop_tasks_now(loop) is False


def test_startup_registers_the_hook_for_the_running_loop():
    """Every startup path (uvicorn and the TestClient used by the startup checks) registers it."""
    import inspect

    import main

    source = inspect.getsource(main.startup_event)
    assert "exit_guard.stop_loop_tasks_at_exit(asyncio.get_running_loop())" in source
