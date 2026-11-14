"""installer/uninstall_app.py: the uninstaller Windows' Uninstall button runs
(Smart App Control blocks Inno's unins000.exe). Nothing here touches the real
service, shortcuts or Installed-apps entry: registry tests use a throwaway
HKCU subkey, file tests a temp folder."""
import os
import re
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))

import uninstall_app as ua  # noqa: E402

winreg = pytest.importorskip("winreg")
HKCU = winreg.HKEY_CURRENT_USER


# --- a fake install ------------------------------------------------------------

SHIPPED = ["Setup.bat", "nssm.exe", "api\\serve.py", "api\\edit_prompt.json",
           "client\\settings_gui.py", "client\\assets\\app_icon.ico",
           "client\\assets\\fonts\\NotoSans.ttf", "installer\\uninstall_app.py",
           "installer\\installed_files.txt"]
USER_DATA = [".env", "api\\transcripts.log", "api\\processing_config.json",
             "api\\word_list_backups\\words-1.json", "models\\turbo\\model.bin",
             "client\\app_styles.json"]
REBUILT = [".venv\\Scripts\\pythonw.exe", "installer\\.tools\\python\\python.exe",
           "installer\\logs\\install_service.ps1.log", "client\\__pycache__\\theme.pyc",
           "unins000.exe", "unins000.dat"]


def make_install(tmp_path, manifest=True):
    app = tmp_path / "Programs" / "Whisper Unchained"
    for rel in SHIPPED + USER_DATA + REBUILT:
        p = app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    if manifest:
        (app / "installer" / "installed_files.txt").write_text(
            "\ufeff" + "\n".join(SHIPPED + ["", "..\\outside.txt", "# comment"]) + "\n",
            encoding="utf-8")
    else:
        (app / "installer" / "installed_files.txt").unlink()
    return str(app)


# --- paths ------------------------------------------------------------------------

def test_is_ours_inside_only(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    assert ua.is_ours(app, app)
    assert ua.is_ours(app + "\\", app)
    assert ua.is_ours('"%s\\.venv\\Scripts\\pythonw.exe"' % app, app)
    assert ua.is_ours((app + "\\client\\x.py").upper(), app)       # case-insensitive
    assert not ua.is_ours(app + "2\\x.py", app)                     # a sibling folder
    assert not ua.is_ours(str(tmp_path / "x.py"), app)
    assert not ua.is_ours("", app) and not ua.is_ours(None, app) and not ua.is_ours('""', app)


def test_read_manifest(tmp_path):
    app = make_install(tmp_path)
    m = ua.read_manifest(app)
    assert m == SHIPPED                      # BOM, blanks, comments and ..\ escapes dropped
    assert ua.read_manifest(str(tmp_path)) is None


def test_safe_app_dir():
    assert not ua.safe_app_dir("C:\\")
    assert not ua.safe_app_dir("C:\\Users")
    assert ua.safe_app_dir("C:\\Users\\bob\\AppData\\Local\\Programs\\Whisper Unchained")


# --- what gets deleted --------------------------------------------------------------

def test_plan_keep_data(tmp_path):
    app = make_install(tmp_path)
    plan = ua.Plan(app, False, ua.read_manifest(app))
    files = {os.path.relpath(f, app) for f in plan.files}
    assert files == set(SHIPPED) | {"unins000.exe", "unins000.dat"}
    dirs = {os.path.relpath(d, app) for d in plan.dirs}
    assert dirs == {"installer\\logs", "client\\__pycache__"}
    assert [os.path.relpath(d, app) for d in plan.deferred_dirs] == [".venv", "installer\\.tools"]
    # Deepest folders first, the install folder itself last.
    depths = [d.count(os.sep) for d in plan.empty_dirs[:-1]]
    assert depths == sorted(depths, reverse=True)
    assert plan.empty_dirs[-1] == app
    assert os.path.join(app, "client", "assets", "fonts") in plan.empty_dirs


def test_delete_files_keeps_user_data(tmp_path):
    app = make_install(tmp_path)
    plan = ua.Plan(app, False, ua.read_manifest(app))
    assert ua.delete_files(plan) == []
    for rel in USER_DATA:
        assert os.path.exists(os.path.join(app, rel)), rel
    for rel in SHIPPED + ["unins000.exe", "unins000.dat"]:
        assert not os.path.exists(os.path.join(app, rel)), rel
    assert not os.path.exists(os.path.join(app, "installer", "logs"))
    assert not os.path.exists(os.path.join(app, "client", "__pycache__"))
    assert not os.path.exists(os.path.join(app, "client", "assets"))   # emptied, so gone
    # The running Python's folders are left for cmd.exe.
    assert os.path.exists(os.path.join(app, ".venv", "Scripts", "pythonw.exe"))
    assert os.path.exists(os.path.join(app, "installer", ".tools"))
    assert ua.has_user_data(app)
    for rel in USER_DATA:
        os.remove(os.path.join(app, rel))
    assert not ua.has_user_data(app)     # only .venv / .tools left: nothing to mention


def test_plan_delete_everything(tmp_path):
    app = make_install(tmp_path)
    plan = ua.Plan(app, True, ua.read_manifest(app))
    assert plan.files == [] and plan.dirs == []
    assert ua.delete_files(plan) == []
    assert os.path.exists(os.path.join(app, ".env"))   # all of it goes in cmd.exe, later
    line = ua.cleanup_command(plan)
    assert line.count("rd /s /q") == 1 and '"%s"' % app in line


def test_cleanup_command_keep_data(tmp_path):
    app = make_install(tmp_path)
    plan = ua.Plan(app, False, ua.read_manifest(app))
    locked = os.path.join(app, "client", "assets", "fonts", "NotoSans.ttf")
    line = ua.cleanup_command(plan, [locked])
    assert 'rd /s /q "%s"' % os.path.join(app, ".venv") in line
    assert 'rd /s /q "%s"' % os.path.join(app, "installer", ".tools") in line
    assert 'del /f /q "%s"' % locked in line
    assert 'rd /s /q "%s"' % app not in line        # never the whole folder when keeping data
    assert line.endswith('rd "%s" 2>nul' % app)     # only if it ended up empty
    assert "%%" not in line and "%i" in line       # cmd /c line, not a .cmd file


# --- registry: a throwaway HKCU key, never the real ones -----------------------------

@pytest.fixture
def test_key():
    base = r"Software\WhisperUnchainedUninstallTest-" + uuid.uuid4().hex
    winreg.CreateKey(HKCU, base).Close()
    yield base
    ua.delete_tree(HKCU, base)


def _entry(base, location, group="Whisper Unchained"):
    key = base + r"\Uninstall\{GUID}_is1"
    with winreg.CreateKey(HKCU, key) as k:
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, location)
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, '"%sunins000.exe"' % location)
        winreg.SetValueEx(k, "Inno Setup: Icon Group", 0, winreg.REG_SZ, group)
    return key


def _value(key, name):
    with winreg.OpenKey(HKCU, key) as k:
        return winreg.QueryValueEx(k, name)[0]


def test_register_points_uninstall_at_python(tmp_path, test_key):
    app = make_install(tmp_path)
    key = _entry(test_key, app + "\\")
    programs = tmp_path / "Start Menu"
    (programs / "Whisper Unchained").mkdir(parents=True)
    link = programs / "Whisper Unchained" / ua.UNINSTALL_LINK
    link.write_text("lnk")
    relinked = []
    assert ua.register(app, key, programs_dir=str(programs),
                       relink=lambda l, a: relinked.append((l, a)))
    cmd = _value(key, "UninstallString")
    assert cmd == '"%s\\.venv\\Scripts\\pythonw.exe" "%s\\installer\\uninstall_app.py"' % (app, app)
    assert _value(key, "QuietUninstallString") == cmd + " --quiet"
    assert relinked == [(str(link), app)]


def test_register_leaves_other_copies_alone(tmp_path, test_key):
    app = make_install(tmp_path)
    key = _entry(test_key, str(tmp_path / "Somewhere else") + "\\")
    assert not ua.register(app, key, programs_dir=str(tmp_path))
    assert _value(key, "UninstallString").endswith('unins000.exe"')
    # A developer copy (no installed_files.txt) never registers either.
    dev = make_install(tmp_path / "dev", manifest=False)
    key2 = _entry(test_key + r"\dev", dev + "\\")
    assert not ua.register(dev, key2, programs_dir=str(tmp_path))


def test_register_needs_the_venv(tmp_path, test_key):
    app = make_install(tmp_path)
    os.remove(os.path.join(app, ".venv", "Scripts", "pythonw.exe"))
    key = _entry(test_key, app + "\\")
    assert not ua.register(app, key, programs_dir=str(tmp_path))


def test_remove_entry_only_ours(tmp_path, test_key):
    app = make_install(tmp_path)
    other = _entry(test_key + r"\other", str(tmp_path / "dev") + "\\")
    assert ua.remove_entry(app, other) is None
    assert _value(other, "InstallLocation")
    ours = _entry(test_key, app + "\\", group="My Group")
    assert ua.remove_entry(app, ours) == "My Group"
    with pytest.raises(OSError):
        winreg.OpenKey(HKCU, ours).Close()


def test_find_context_keys_only_ours(tmp_path, test_key):
    app = str(tmp_path / "Whisper Unchained")
    dev = str(tmp_path / "Faster_Whisper_API")
    classes = test_key + r"\Classes"

    def add(sub, exe):
        with winreg.CreateKey(HKCU, classes + "\\" + sub + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, '"%s" "x.py" "%%1"' % exe)
    add(r"SystemFileAssociations\.mp3\shell\WhisperUnchained.Clipboard",
        app + r"\.venv\Scripts\pythonw.exe")
    add(r"SystemFileAssociations\.wav\shell\WhisperUnchained.Open",
        dev + r"\.venv\Scripts\pythonw.exe")
    add(r"*\shell\TranscribeToClipboard", app + r"\.venv\Scripts\pythonw.exe")
    found = ua.find_context_keys(app, HKCU, classes)
    assert sorted(found) == sorted([
        classes + r"\SystemFileAssociations\.mp3\shell\WhisperUnchained.Clipboard",
        classes + r"\*\shell\TranscribeToClipboard"])
    for k in found:
        ua.delete_tree(HKCU, k)
    assert ua.find_context_keys(app, HKCU, classes) == []
    assert ua.find_context_keys(dev, HKCU, classes)          # the dev copy's entry survived


def test_find_astral_keys(tmp_path, test_key):
    app = str(tmp_path / "Whisper Unchained")
    base = test_key + r"\Astral"
    for name, where in (("CPython3.13", app + r"\installer\.tools\python"),
                        ("CPython3.12", str(tmp_path / "elsewhere"))):
        with winreg.CreateKey(HKCU, base + "\\" + name + r"\InstallPath") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, where)
    assert ua.find_astral_keys(app, HKCU, base) == [base + r"\CPython3.13"]


def test_find_uv_bins(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    b = tmp_path / "bin"
    b.mkdir()
    (b / "python3.13.exe").write_bytes(b"MZ..." + (app + "\\installer\\.tools\\python.exe").encode())
    (b / "python3.12.exe").write_bytes(b"MZ... C:\\Python312\\python.exe")
    (b / "other.exe").write_bytes(app.encode())
    assert ua.find_uv_bins(app, str(b)) == [str(b / "python3.13.exe")]


# --- shortcuts, service, processes -----------------------------------------------------

def test_find_shortcuts(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    folder = tmp_path / "Startup"
    folder.mkdir()
    for n in ("Whisper Unchained.lnk", "FasterWhisperListener.lnk", "Other.lnk"):
        (folder / n).write_text("")
    targets = {"Whisper Unchained.lnk": [app + r"\.venv\Scripts\pythonw.exe", app],
               "FasterWhisperListener.lnk": [r"C:\dev\.venv\Scripts\pythonw.exe", r"C:\dev"],
               "Other.lnk": ["", app]}   # no target: its working folder decides

    def refs(p):
        return targets[os.path.basename(p)]
    assert ua.find_shortcuts(str(folder), ua.STARTUP_LINKS, app, refs) == [
        str(folder / "Whisper Unchained.lnk")]
    assert sorted(ua.find_shortcuts(str(folder), None, app, refs)) == sorted([
        str(folder / "Whisper Unchained.lnk"), str(folder / "Other.lnk")])
    assert ua.find_shortcuts(str(tmp_path / "missing"), None, app, refs) == []


def test_shortcut_bytes_fallback(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    raw = [("junk" + app + "\\.venv\\Scripts\\pythonw.exe").encode("utf-16-le")]
    assert ua.shortcut_is_ours("x.lnk", app, refs=lambda p: [r.decode("utf-16-le") for r in raw])
    assert not ua.shortcut_is_ours("x.lnk", app, refs=lambda p: [app + "2\\pythonw.exe", "", ""])


def test_find_services_and_admin_line(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    dirs = {"WhisperUnchained": app, "FasterWhisperAPI": r"C:\Users\x\Faster_Whisper_API"}
    assert ua.find_services(app, app_directory=dirs.get) == ["WhisperUnchained"]
    assert ua.find_services(app, app_directory=lambda n: None) == []
    line = ua.elevated_command(["WhisperUnchained"])
    assert 'sc.exe stop "WhisperUnchained"' in line and 'sc.exe delete "WhisperUnchained"' in line
    assert "FasterWhisperAPI\" >nul" not in line.replace("Restart FasterWhisperAPI", "")
    assert 'netsh advfirewall firewall delete rule name="Whisper Unchained Server"' in line
    assert 'schtasks /Delete /TN "Restart Whisper Unchained" /F' in line
    assert "powershell" not in line.lower()      # nothing an App Control policy can restrict


def test_find_processes(tmp_path):
    app = str(tmp_path / "Whisper Unchained")
    py = app + r"\installer\.tools\python\pythonw.exe"
    procs = [(1, py, [py, app + r"\client\listener_watchdog.py"]),
             (2, py, [py, app + r"\installer\uninstall_app.py"]),        # the launcher
             (3, py, [py, app + r"\installer\uninstall_app.py"]),        # this process
             (4, r"C:\dev\.venv\Scripts\pythonw.exe", ["x"]),             # dev copy
             (5, None, None)]
    assert ua.find_processes(app, procs, own_pid=3) == [1]


# --- the whole run, files and registry only ------------------------------------------------

@pytest.fixture
def sandboxed(tmp_path, monkeypatch, test_key):
    """Start menu, Startup and the Installed-apps entry all in throwaway places;
    no cmd.exe handoff."""
    programs = tmp_path / "Start Menu"
    monkeypatch.setattr(ua, "shell_folder", lambda csidl: str(programs))
    key = test_key + r"\Uninstall\entry_is1"
    monkeypatch.setattr(ua, "UNINSTALL_KEY", key)
    for name in ("entry_is_ours", "remove_entry"):
        f = getattr(ua, name)
        monkeypatch.setattr(ua, name, lambda app, *_a, _f=f: _f(app, key))
    handoffs = []
    monkeypatch.setattr(ua.Uninstaller, "handoff", lambda self: handoffs.append(self.plan))
    return programs, key, handoffs


def test_quiet_files_only_keeps_data(tmp_path, sandboxed, monkeypatch):
    programs, key, handoffs = sandboxed
    app = make_install(tmp_path)
    with winreg.CreateKey(HKCU, key) as k:
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, app + "\\")
    group = programs / "Whisper Unchained"
    group.mkdir(parents=True)
    (group / "Whisper Unchained.lnk").write_text("")
    (group / "Someone else's.lnk").write_text("")
    targets = {"Whisper Unchained.lnk": [app + r"\.venv\Scripts\pythonw.exe", app],
               "Someone else's.lnk": [r"C:\dev\pythonw.exe", r"C:\dev"]}
    monkeypatch.setattr(ua, "shortcut_refs", lambda p: targets[os.path.basename(p)])
    assert ua.main(["--quiet", "--files-only", "--app-dir", app]) == 0
    assert sorted(os.listdir(group)) == ["Someone else's.lnk"]  # only ours removed
    with pytest.raises(OSError):
        winreg.OpenKey(HKCU, key).Close()                       # Installed-apps entry gone
    for rel in USER_DATA:                                       # default: data kept
        assert os.path.exists(os.path.join(app, rel)), rel
    assert not os.path.exists(os.path.join(app, "Setup.bat"))
    assert len(handoffs) == 1 and handoffs[0].delete_data is False


def test_dry_run_changes_nothing(tmp_path, sandboxed, capsys):
    app = make_install(tmp_path)
    before = sorted(os.path.relpath(os.path.join(t, f), app)
                    for t, _d, fs in os.walk(app) for f in fs)
    assert ua.main(["--dry-run", "--files-only", "--app-dir", app]) == 0
    after = sorted(os.path.relpath(os.path.join(t, f), app)
                   for t, _d, fs in os.walk(app) for f in fs)
    assert before == after
    assert "would remove: " + os.path.join(app, "Setup.bat") in capsys.readouterr().out


def test_refuses_a_folder_without_manifest(tmp_path, sandboxed):
    _, _, handoffs = sandboxed
    dev = make_install(tmp_path, manifest=False)
    assert ua.main(["--quiet", "--delete-data", "--files-only", "--app-dir", dev]) == 1
    assert os.path.exists(os.path.join(dev, "Setup.bat")) and handoffs == []


# --- the installer writes the same command as --register -----------------------------------

def test_iss_uses_the_same_uninstall_command():
    iss = open(os.path.join(ROOT, "installer", "WhisperUnchained.iss"), encoding="utf-8").read()
    assert ua.APP_GUID[1:] in iss                                  # AppId={{GUID}
    assert ua.UNINSTALL_KEY.split("\\")[-1] in iss
    m = re.search(r"Cmd := '\"' \+ ExpandConstant\('\{app\}\\(.+?)'\) \+ '\" \"' \+\s*"
                  r"ExpandConstant\('\{app\}\\(.+?)'\) \+ '\"';", iss)
    assert m, "CurStepChanged's command changed shape"
    app = r"C:\App"
    assert '"%s\\%s" "%s\\%s"' % (app, m.group(1), app, m.group(2)) == ua.uninstall_command(app)
    assert "' --quiet'" in iss
    assert "Check: VenvReady" in iss and "Check: not VenvReady" in iss


def test_build_writes_the_manifest():
    ps = open(os.path.join(ROOT, "installer", "build_exe.ps1"), encoding="utf-8").read()
    assert ua.MANIFEST in ps


# --- the window: data is kept unless the box is ticked --------------------------------------

def test_dialog_default_keeps_data(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        d = ua.Dialog(ROOT)
    except tk.TclError:
        pytest.skip("no display")
    try:
        d.root.after(50, lambda: d._set("go"))
        assert d.confirm() is False
        d.root.after(50, lambda: d._set("cancel"))
        assert d.confirm() is None
    finally:
        d.root.destroy()
