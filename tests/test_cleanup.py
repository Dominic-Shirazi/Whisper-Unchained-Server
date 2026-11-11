"""Dictation cleanup: prompt assembly (contract 02-cleanup.md) and output tidying."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import cleanup  # noqa: E402

P = cleanup.PROMPT


def test_default_prompt_assembly_is_exact():
    expected = "\n".join(P["rules"] + [
        P["style_header"] + "\n" + "\n".join([
            "- " + P["style"]["punctuation"]["standard"],
            "- " + P["style"]["rewrite"]["keep"],
            "- " + P["style"]["tone"]["neutral"],
            "- " + P["style"]["shorten"]["off"],
            "- " + P["style"]["emoji"]["off"],
        ]),
        P["final"],
    ])
    assert cleanup.build_system_prompt({}) == expected


def test_vocabulary_line_and_keep_while_shortening():
    s = cleanup.build_system_prompt({"shorten": "light"}, vocabulary=["Lesley", " ", "Ollama"])
    assert "exactly as written here: Lesley, Ollama." in s
    assert "- " + P["keep_while_shortening"] in s
    assert P["keep_while_shortening"] not in cleanup.build_system_prompt(
        {"shorten": "light", "rewrite": "fix"})


def test_style_options_change_the_lines():
    s = cleanup.build_system_prompt({"punctuation": "minimal", "rewrite": "polish",
                                     "shorten": "strong", "emoji": "on"})
    for key, value in (("punctuation", "minimal"), ("rewrite", "polish"),
                       ("shorten", "strong"), ("emoji", "on")):
        assert P["style"][key][value] in s


def test_bad_style_is_an_error():
    assert cleanup.normalize_style({"rewrite": "rewrite-it-all"})[1]
    assert cleanup.normalize_style({"tone": "pirate"})[1]
    with pytest.raises(ValueError):
        cleanup.build_system_prompt({"emoji": "lots"})


def test_user_message_wraps_in_tags():
    assert cleanup.user_message("hi there") == "<dictation>\nhi there\n</dictation>"


@pytest.mark.parametrize("raw,clean", [
    ("<think>hmm</think>Hello there.", "Hello there."),
    ("```\nHello there.\n```", "Hello there."),
    ("<dictation>\nHello there.\n</dictation>", "Hello there."),
    ("Here is the cleaned text: Hello there.", "Hello there."),
    ('"Hello there."', "Hello there."),
    ("<think>cut off by the token limit", ""),
    ("Hello there.", "Hello there."),
])
def test_tidy_output(raw, clean):
    assert cleanup.tidy_output(raw, "hello there") == clean


def test_tidy_keeps_quotes_the_speaker_dictated():
    assert cleanup.tidy_output('"Hi," she said.', '"Hi," she said') == '"Hi," she said.'


def test_contract_copy_matches_if_present():
    """The app builds its prompt from contracts/02-cleanup-prompt.json; it must
    be byte-identical to the server's copy."""
    other = os.path.join(os.path.expanduser("~"), "AndroidStudioProjects",
                         "WhisperRemote_for_Android", "contracts", "02-cleanup-prompt.json")
    if not os.path.exists(other):
        pytest.skip("WhisperRemote repo not on this machine")
    with open(other, encoding="utf-8") as a, open(cleanup.PROMPT_PATH, encoding="utf-8") as b:
        assert json.load(a) == json.load(b)


def test_tone_applies_only_when_rewording_is_allowed():
    fix = cleanup.build_system_prompt({"rewrite": "fix", "tone": "professional"})
    assert P["style"]["tone"]["professional"] in fix
    keep = cleanup.build_system_prompt({"rewrite": "keep", "tone": "professional"})
    assert P["style"]["tone"]["professional"] not in keep
    assert P["style"]["tone"]["neutral"] in keep
