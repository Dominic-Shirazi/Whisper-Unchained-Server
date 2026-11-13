"""The "choose programs" picker: the pure helpers (UserAssist decoding, .lnk
parsing, merge, sort, filter) on made-up data, and the dialog driven with a fake
catalog. Nothing here reads or writes the real registry."""
import codecs
import os
import struct
import sys
import tkinter as tk
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import app_picker as ap  # noqa: E402
from app_picker import App  # noqa: E402

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows program picker (Start menu, UserAssist, .lnk, exe paths)")


def _filetime(dt):
    return int((dt - ap.FILETIME_EPOCH).total_seconds() * 10 ** 7)


def _ua_value(dt, count=3):
    data = bytearray(72)
    struct.pack_into("<I", data, 4, count)
    struct.pack_into("<Q", data, 60, _filetime(dt) if dt else 0)
    return bytes(data)


# --- names ---------------------------------------------------------------------

@WINDOWS_ONLY
def test_parse_names_normalizes_dedupes_and_keeps_order():
    assert ap.parse_names("Minecraft, blender.exe ,, MINECRAFT.EXE") == \
        ["minecraft.exe", "blender.exe"]
    assert ap.parse_names(["C:\\Games\\Foo.exe", "foo"]) == ["foo.exe"]
    assert ap.parse_names("") == [] and ap.parse_names(None) == []
    assert ap.format_names(["a", "b.exe"]) == "a.exe, b.exe"


def test_noise_is_skipped():
    for exe in ("explorer.exe", "pythonw.exe", "unins000.exe", "setup.exe",
                "steamupdater.exe", "crashpad_handler.exe", "readme.txt"):
        assert ap.is_noise(exe), exe
    for exe in ("slack.exe", "minecraft.exe", "code.exe"):
        assert not ap.is_noise(exe), exe


# --- UserAssist ------------------------------------------------------------------

@WINDOWS_ONLY
def test_parse_userassist_decodes_rot13_paths_and_filetime():
    env = {"ProgramW6432": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "SystemRoot": r"C:\Windows"}
    when = NOW - timedelta(days=2)
    values = [
        (codecs.encode(r"{6D809377-6AF0-444B-8957-A3773F02200E}\Slack\slack.exe", "rot13"),
         _ua_value(when)),
        (codecs.encode(r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\notepad.exe", "rot13"),
         _ua_value(None)),
        (codecs.encode(r"{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\Steam\steam.exe", "rot13"),
         b"short"),
        (codecs.encode("Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "rot13"), _ua_value(when)),
        (codecs.encode("UEME_CTLSESSION", "rot13"), b"\0" * 1612),
    ]
    out = ap.parse_userassist(values, env)
    assert out[0][0] == r"C:\Program Files\Slack\slack.exe"
    assert abs((out[0][1] - when).total_seconds()) < 1
    assert out[1] == (r"C:\Windows\System32\notepad.exe", None)
    assert out[2] == (r"C:\Program Files (x86)\Steam\steam.exe", None)
    assert len(out) == 3          # the Store app and the session counter are skipped


@WINDOWS_ONLY
def test_unknown_folder_guid_keeps_the_file_name():
    path = ap.resolve_userassist_path(r"{00000000-0000-0000-0000-000000000000}\x\foo.exe", {})
    assert ap.normalize_app(path) == "foo.exe"


def test_filetime_zero_is_none():
    assert ap.filetime_to_datetime(0) is None


# --- .lnk ----------------------------------------------------------------------

def _lnk(base, suffix="", args="", env_target=None):
    """A minimal shortcut: header, LinkInfo with a local path, optional
    arguments string, optional environment-variable block."""
    flags = 0x80                                   # IsUnicode
    body = b""
    if base is not None:
        flags |= 0x02
        hdr = 0x24
        b_off = hdr
        b_bytes = base.encode("utf-16-le") + b"\0\0"
        s_off = b_off + len(b_bytes)
        s_bytes = suffix.encode("utf-16-le") + b"\0\0"
        size = s_off + len(s_bytes)
        body += struct.pack("<IIIIIIIII", size, hdr, 1, 0, 0, 0, 0, b_off, s_off)
        body += b_bytes + s_bytes
    if args:
        flags |= 0x20
        body += struct.pack("<H", len(args)) + args.encode("utf-16-le")
    if env_target:
        block = struct.pack("<II", 0x314, 0xA0000001) + b"\0" * 260 + \
            env_target.encode("utf-16-le").ljust(520, b"\0")
        body += block + b"\0\0\0\0"
    header = bytearray(0x4C)
    struct.pack_into("<I", header, 0, 0x4C)
    struct.pack_into("<I", header, 0x14, flags)
    return bytes(header) + body


def test_parse_lnk_joins_base_path_and_suffix():
    assert ap.parse_lnk(_lnk("C:\\", "Program Files\\Obsidian\\Obsidian.exe")) == \
        ("C:\\Program Files\\Obsidian\\Obsidian.exe", "")


@WINDOWS_ONLY
def test_parse_lnk_env_block(monkeypatch):
    monkeypatch.setenv("WU_TEST_DIR", r"D:\Apps")
    path, _ = ap.parse_lnk(_lnk(None, env_target=r"%WU_TEST_DIR%\Tool\tool.exe"))
    assert path == r"D:\Apps\Tool\tool.exe"


def test_parse_lnk_garbage_is_empty():
    assert ap.parse_lnk(b"not a shortcut") == ("", "")
    assert ap.parse_lnk(_lnk("C:\\x.exe")[:0x50]) == ("", "")


@WINDOWS_ONLY
def test_squirrel_update_shortcut_means_the_real_app():
    path, args = ap.parse_lnk(_lnk("C:\\Users\\me\\AppData\\Local\\Discord\\Update.exe",
                                   args="--processStart Discord.exe"))
    assert ap.shortcut_target_exe(path, args) == "discord.exe"
    assert ap.shortcut_target_exe(r"C:\docs\readme.txt") == ""


@WINDOWS_ONLY
def test_start_menu_programs_reads_shortcuts(tmp_path):
    games = tmp_path / "Games"
    games.mkdir()
    (games / "Minecraft Launcher.lnk").write_bytes(_lnk("C:\\Games\\MinecraftLauncher.exe"))
    (tmp_path / "Uninstall Foo.lnk").write_bytes(_lnk("C:\\Foo\\unins000.exe"))
    admin = tmp_path / "Administrative Tools"
    admin.mkdir()
    (admin / "Registry Editor.lnk").write_bytes(_lnk("C:\\Windows\\regedt32.exe"))
    found = ap.start_menu_programs([str(tmp_path)])
    names = {e["exe"]: e["name"] for e in found}
    assert names["minecraftlauncher.exe"] == "Minecraft Launcher"
    assert "regedt32.exe" not in names         # admin folder skipped
    assert ap.merge([found]) and all(a.exe != "unins000.exe" for a in ap.merge([found]))


# --- merge / sort / filter ---------------------------------------------------------

@WINDOWS_ONLY
def test_merge_dedupes_and_combines_sources():
    old, new = NOW - timedelta(days=9), NOW - timedelta(days=1)
    apps = ap.merge([
        [{"exe": "Slack.exe", "name": "Slack (Beta channel)"}, {"exe": "slack", "name": "Slack"}],
        [{"exe": r"C:\x\slack.exe", "last_used": old}, {"exe": "slack.exe", "last_used": new}],
        [{"exe": "slack.exe", "running": True}, {"exe": "svchost.exe", "running": True}],
    ])
    assert len(apps) == 1
    a = apps[0]
    assert (a.exe, a.name, a.running, a.last_used) == ("slack.exe", "Slack", True, new)


def _sample():
    return [
        App("zoom.exe", "Zoom", last_used=NOW - timedelta(days=1)),
        App("blender.exe", "Blender"),
        App("code.exe", "Visual Studio Code", running=True),
        App("slack.exe", "Slack", running=True, last_used=NOW - timedelta(hours=1)),
        App("audacity.exe", "Audacity", last_used=NOW - timedelta(days=30)),
        App("minecraft.exe", "minecraft"),
    ]


def test_recent_sort_running_then_last_use_then_az():
    order = [a.exe for a in ap.sort_apps(_sample())]
    assert order == ["slack.exe", "code.exe", "zoom.exe", "audacity.exe",
                     "blender.exe", "minecraft.exe"]


def test_az_sort_and_chosen_first():
    order = [a.exe for a in ap.sort_apps(_sample(), ap.SORT_AZ, first={"zoom.exe"})]
    assert order == ["zoom.exe", "audacity.exe", "blender.exe", "minecraft.exe",
                     "slack.exe", "code.exe"]
    assert ap.group_of(_sample()[0], ap.SORT_AZ, {"zoom.exe"}) == "Chosen"


def test_filter_by_name_or_exe_all_words():
    apps = _sample()
    assert [a.exe for a in ap.filter_apps(apps, "code")] == ["code.exe"]
    assert [a.exe for a in ap.filter_apps(apps, "VISUAL studio")] == ["code.exe"]
    assert [a.exe for a in ap.filter_apps(apps, "zoom.exe")] == ["zoom.exe"]
    assert len(ap.filter_apps(apps, "  ")) == len(apps)


@WINDOWS_ONLY
def test_display_name_prefers_short_descriptions():
    assert ap.display_name(r"C:\S\steam.exe", "steam.exe", "Steam") == "Steam"
    assert ap.display_name(r"C:\C\Core Temp.exe", "core temp.exe",
                           "CPU temperature and system information utility") == "Core Temp"
    assert ap.display_name(r"C:\D\unifying210.exe", "unifying210.exe", "Setup") == "unifying210"


def test_when_text():
    assert ap.when_text(NOW - timedelta(hours=3), NOW) == "Used today"
    assert ap.when_text(NOW - timedelta(days=1, hours=1), NOW) == "Used yesterday"
    assert ap.when_text(NOW - timedelta(days=20), NOW) == "Used 2 weeks ago"
    assert ap.when_text(None, NOW) == ""


def test_catalog_survives_failing_sources():
    def boom():
        raise OSError("no registry here")
    apps = ap.catalog(sources={"running": lambda: [{"exe": "slack.exe", "running": True}],
                               "recent": boom,
                               "installed": lambda: [{"exe": "slack.exe", "name": "Slack"},
                                                     {"exe": "zoom.exe", "name": "Zoom"}]})
    by = {a.exe: a for a in apps}
    assert set(by) == {"slack.exe", "zoom.exe"}
    assert by["slack.exe"].running and by["slack.exe"].name == "Slack"
    assert not by["zoom.exe"].running


# --- the dialog -----------------------------------------------------------------

pytestmark_tk = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only window")


@pytest.fixture(scope="module")
def tk_root():
    # One Tk interpreter for the module (several in a row intermittently fail
    # on Windows); each test opens its picker in a Toplevel of it.
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip("no display: %s" % e)
    root.withdraw()
    yield root
    root.destroy()


def _picker(tk_root, selected="minecraft.exe, slack"):
    p = ap.AppPicker(tk_root, "Pick", selected, help_text="Help",
                     load=lambda: _sample()[:5])
    p.top.withdraw()
    for _ in range(100):                 # wait for the background load
        tk_root.update()
        if p.loaded:
            break
    assert p.loaded
    return p


@pytestmark_tk
def test_dialog_lists_catalog_and_keeps_unknown_choices(tk_root):
    p = _picker(tk_root)
    try:
        exes = p.visible_exes()
        # Chosen first (minecraft isn't in the catalog but still shows), then running...
        assert set(exes[:2]) == {"minecraft.exe", "slack.exe"}
        assert exes[2] == "code.exe"
        assert set(exes) == {a.exe for a in _sample()}
        assert [r[1] for r in p.rows if r[0] == "head"] == \
            ["Chosen", "Running now", "Recently used", "Other programs"]
    finally:
        p.cancel()
    assert p.result is None


@pytestmark_tk
def test_dialog_toggle_search_add_and_ok(tk_root):
    p = _picker(tk_root)
    p.toggle("slack.exe")                       # untick
    p.toggle("zoom.exe")                        # tick
    p.query.set("blend")
    assert p.visible_exes() == ["blender.exe"]
    p.list.focus_set()
    p.toggle_cursor()                           # Space on the only row
    p.add_var.set("Game")
    p.add_typed()
    assert p.query.get() == "" and "game.exe" in p.visible_exes()
    p._set_sort(ap.SORT_AZ)
    assert p.visible_exes()[0] == "minecraft.exe"   # chosen-at-open still lead
    p.ok()
    assert p.result == ["minecraft.exe", "zoom.exe", "blender.exe", "game.exe"]


@pytestmark_tk
def test_ok_takes_a_typed_but_unadded_name(tk_root):
    p = _picker(tk_root, selected=[])
    p.add_var.set("foo")
    p.ok()
    assert p.result == ["foo.exe"]


@pytestmark_tk
def test_keyboard_moves_and_toggles(tk_root):
    p = _picker(tk_root, selected="")
    first = p.visible_exes()[0]
    assert p.list.bind("<space>")          # Space is wired to the same action
    p.toggle_cursor()
    assert p.is_checked(first)
    p.move(1)
    p.toggle_cursor()
    assert p.checked == [first, p.visible_exes()[1]]
    p.move(-100)
    assert p.rows[p.cursor][1].exe == first
    p.cancel()
