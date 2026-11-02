"""The conftest kill guard: no test may signal a process it did not start.

The guard is installed only outside a container (in the integration image the app's real
kills are the subject), so these tests skip wherever it is absent — they must never send
a real signal to a foreign process.
"""
import os
import signal
import subprocess
import sys

import pytest

from casa_browser import launcher

GUARDED = getattr(os.kill, "__name__", "") == "_guarded_kill"
pytestmark = pytest.mark.skipif(not GUARDED, reason="the kill guard is not installed here")


def test_guard_refuses_a_foreign_process():
    for target in (os.getppid(), 1):
        with pytest.raises(RuntimeError, match="test guard"):
            os.kill(target, signal.SIGKILL)
    with pytest.raises(RuntimeError, match="test guard"):
        os.kill(-1, signal.SIGKILL)
    with pytest.raises(RuntimeError, match="test guard"):
        os.killpg(1, signal.SIGKILL)
    # A default bound to os.kill at import time is guarded too.
    with pytest.raises(RuntimeError, match="test guard"):
        launcher.reap_leftover_chrome.__defaults__[-1](os.getppid(), signal.SIGKILL)


@pytest.mark.skipif("CASA_KILL_GUARD_VICTIM" not in os.environ,
                    reason="needs a disposable process started outside pytest")
def test_guard_refuses_a_disposable_victim():
    victim = int(os.environ["CASA_KILL_GUARD_VICTIM"])
    with pytest.raises(RuntimeError, match="test guard"):
        os.kill(victim, signal.SIGKILL)
    with pytest.raises(RuntimeError, match="test guard"):
        os.killpg(victim, signal.SIGKILL)
    os.kill(victim, 0)  # still alive


def test_guard_allows_a_child_this_test_started():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=True)
    os.killpg(child.pid, signal.SIGKILL)
    assert child.wait(5) == -signal.SIGKILL
