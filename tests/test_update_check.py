"""client/update_check.py: asks GitHub for the newest version once a day, says
when there is one, and never downloads or installs anything. No network here:
the opener is a fake."""
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

import update_check as uc  # noqa: E402

RELEASES = [
    {"tag_name": "engine-whispercpp-v1.9.5", "draft": False, "published_at": "2026-11-19T00:00:00Z"},
    {"tag_name": "v0.9.20", "draft": False, "published_at": "2026-11-18T20:00:00Z"},
    {"tag_name": "v0.9.19", "draft": False, "published_at": "2026-11-18T06:13:51Z"},
    {"tag_name": "v0.10.0", "draft": True, "published_at": None},     # not published
    {"tag_name": "v0.9.9", "draft": False, "published_at": "2026-11-13T22:14:30Z"},
    # A real one: an early release from before the numbering restarted at 0.9.
    {"tag_name": "v1.0.0", "draft": False, "published_at": "2026-03-26T07:25:19Z"},
    {"tag_name": "nightly", "draft": False, "published_at": "2026-11-19T00:00:00Z"},
]


class Opener:
    def __init__(self, releases=RELEASES, error=None):
        self.releases, self.error, self.requests = releases, error, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.releases).encode())


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / "update_check.json")


def test_parse_takes_app_versions_only():
    assert uc.parse("v0.9.21") == (0, 9, 21) and uc.parse("0.9.21") == (0, 9, 21)
    for bad in ("engine-whispercpp-v1.9.5", "nightly", "1.0", "", None, "v1.2.3-beta"):
        assert uc.parse(bad) is None


def test_newest_is_the_latest_published_app_version():
    assert uc.newest(RELEASES) == "0.9.20"          # not the old 1.0.0, not the engine
    assert uc.newest([]) == "" and uc.newest({"message": "rate limited"}) == ""


def test_a_newer_version_is_reported(cache):
    opener = Opener()
    assert uc.check({}, current="0.9.19", now=1000, opener=opener, path=cache) == "0.9.20"
    assert uc.page("0.9.20").endswith("/releases/tag/v0.9.20")


def test_up_to_date_says_nothing(cache):
    assert uc.check({}, current="0.9.20", now=1000, opener=Opener(), path=cache) == ""
    assert uc.check({}, current="0.9.21", now=1000, opener=Opener(), path=cache) == ""


def test_asks_github_once_a_day_at_most(cache):
    opener = Opener()
    uc.check({}, current="0.9.19", now=1000, opener=opener, path=cache)
    assert uc.check({}, current="0.9.19", now=1000 + 3600, opener=opener, path=cache) == "0.9.20"
    assert len(opener.requests) == 1
    uc.check({}, current="0.9.19", now=1000 + uc.CHECK_EVERY, opener=opener, path=cache)
    assert len(opener.requests) == 2
    uc.check({}, current="0.9.19", now=1001 + uc.CHECK_EVERY, opener=opener, path=cache, force=True)
    assert len(opener.requests) == 3


def test_switched_off_never_asks(cache):
    opener = Opener()
    for value in ("false", "0", "off", "No"):
        assert uc.check({"UPDATE_CHECK": value}, current="0.9.19", now=1000,
                        opener=opener, path=cache) == ""
    assert opener.requests == [] and not os.path.exists(cache)
    assert uc.enabled({}) and uc.enabled({"UPDATE_CHECK": "true"}) and uc.enabled(None)


def test_a_developer_copy_never_asks(cache, tmp_path):
    opener = Opener()
    assert uc.current_version(str(tmp_path)) == ""
    assert uc.check({}, current="", now=1000, opener=opener, path=cache) == ""
    assert opener.requests == []


def test_installed_version_is_read_from_the_installer_folder(tmp_path):
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "version.txt").write_text("0.9.20\n", encoding="utf-8")
    assert uc.current_version(str(tmp_path)) == "0.9.20"
    (tmp_path / "installer" / "version.txt").write_text("1.0.0-test", encoding="utf-8")
    assert uc.current_version(str(tmp_path)) == ""


def test_offline_keeps_the_last_answer_and_retries_in_an_hour(cache):
    good, down = Opener(), Opener(error=OSError("no network"))
    uc.check({}, current="0.9.19", now=1000, opener=good, path=cache)
    later = 1000 + uc.CHECK_EVERY
    assert uc.check({}, current="0.9.19", now=later, opener=down, path=cache) == "0.9.20"
    uc.check({}, current="0.9.19", now=later + 60, opener=down, path=cache)
    assert len(down.requests) == 1                      # not hammered
    uc.check({}, current="0.9.19", now=later + uc.RETRY_AFTER, opener=down, path=cache)
    assert len(down.requests) == 2


def test_first_check_offline_says_nothing(cache):
    assert uc.check({}, current="0.9.19", now=1000,
                    opener=Opener(error=OSError("x")), path=cache) == ""


def test_known_answers_from_memory_without_asking(cache):
    assert uc.known({}, current="0.9.19", path=cache) == ""
    uc.check({}, current="0.9.19", now=1000, opener=Opener(), path=cache)
    assert uc.known({}, current="0.9.19", path=cache) == "0.9.20"
    assert uc.known({"UPDATE_CHECK": "false"}, current="0.9.19", path=cache) == ""
    assert uc.known({}, current="0.9.20", path=cache) == ""   # after updating


def test_the_request_sends_nothing_about_the_person(cache):
    opener = Opener()
    uc.check({"WHISPER_API_KEY": "secret-key"}, current="0.9.19", now=1000,
             opener=opener, path=cache)
    req = opener.requests[0]
    assert req.full_url == uc.RELEASES_URL and req.data is None
    sent = " ".join("%s %s" % kv for kv in req.header_items())
    assert "secret-key" not in sent and "0.9.19" not in sent
    assert req.get_header("User-agent") == "WhisperUnchained"


def test_a_garbled_cache_is_treated_as_empty(cache):
    with open(cache, "w") as f:
        f.write("not json")
    assert uc.check({}, current="0.9.19", now=1000, opener=Opener(), path=cache) == "0.9.20"


def test_the_tray_says_a_new_version_once(cache):
    uc.check({}, current="0.9.19", now=1000, opener=Opener(), path=cache)
    assert uc.notice_due("0.9.20", path=cache) is True
    assert uc.notice_due("0.9.20", path=cache) is False
    # The next daily check does not forget that it was said.
    uc.check({}, current="0.9.19", now=1000 + uc.CHECK_EVERY, opener=Opener(), path=cache)
    assert uc.notice_due("0.9.20", path=cache) is False
    assert uc.notice_due("0.9.21", path=cache) is True      # a newer one is said again
    assert uc.notice_due("", path=cache) is False


def test_wording_names_the_version_and_promises_no_download():
    assert uc.menu_text("0.9.21") == "Version 0.9.21 is available..."
    assert "0.9.21" in uc.SETTINGS_TITLE % "0.9.21"
    assert "keeps your settings" in uc.SETTINGS_HELP
    # The button only opens a page: the words say so, and how to install.
    assert "opens the download page" in uc.SETTINGS_HELP and "run it" in uc.SETTINGS_HELP
    assert uc.SETTINGS_BUTTON == "Get the update"


def test_settings_has_the_switch_on_by_default_and_linux_hides_it():
    import settings_gui as sg
    field = next(f for _name, fields in sg.SCHEMA for f in fields if f.key == "UPDATE_CHECK")
    assert field.kind == "toggle" and field.default == "true"
    assert "Sends nothing about you" in field.help
    import linux_settings
    assert {"UPDATE_CHECK", "_h_updates"} <= linux_settings.WINDOWS_ONLY


def test_the_example_settings_file_has_the_switch():
    with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as f:
        assert "\nUPDATE_CHECK=true\n" in f.read()
