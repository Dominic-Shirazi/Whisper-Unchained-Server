import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import game_mode as gm  # noqa: E402


def test_tracker_pauses_after_delay_only():
    t = gm.Tracker(start_after=15, end_after=10)
    assert t.step(True, 0, None) is None
    assert t.step(True, 14, None) is None
    assert t.step(True, 15, None) == "pause"


def test_quick_alt_tab_does_not_flap():
    t = gm.Tracker(start_after=15, end_after=10)
    t.step(True, 0, None)
    t.step(False, 5, None)            # tabbed out: timer resets
    assert t.step(True, 12, None) is None
    assert t.step(True, 26, None) is None
    assert t.step(True, 27, None) == "pause"


def test_resumes_only_its_own_pause():
    t = gm.Tracker(start_after=15, end_after=10)
    assert t.step(False, 0, "game") is None
    assert t.step(False, 10, "game") == "resume"
    t2 = gm.Tracker(start_after=15, end_after=10)
    assert t2.step(False, 0, "user") is None
    assert t2.step(False, 999, "user") is None      # your own pause stays


def test_no_pause_while_already_paused():
    t = gm.Tracker(start_after=0, end_after=0)
    assert t.step(True, 0, "user") is None
    assert t.step(True, 0, "game") is None


def test_is_gaming_rules():
    assert gm.is_gaming(state=gm.QUNS_RUNNING_D3D_FULL_SCREEN, front="eldenring.exe")
    assert gm.is_gaming(state=gm.QUNS_BUSY, front="game.exe")
    assert not gm.is_gaming(state=gm.QUNS_BUSY, front="chrome.exe")   # full-screen video
    assert not gm.is_gaming(state=5, front="game.exe")                 # normal desktop
    assert gm.is_gaming({"minecraft.exe"}, state=5, front="explorer.exe",
                        running={"minecraft.exe", "explorer.exe"})


def test_parse_list():
    assert gm.parse_list(" Game.exe, other.EXE ,,") == {"game.exe", "other.exe"}


def test_live_detection_does_not_raise():
    gm.is_gaming()  # real Windows calls; on the desktop this is normally False
