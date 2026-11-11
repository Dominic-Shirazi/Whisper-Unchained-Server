"""'prompt AI' voice commands (contract 03)."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import edit_commands as ec  # noqa: E402


@pytest.mark.parametrize("text,content,instruction", [
    ("The meeting moved to noon. Prompt AI, make this formal.",
     "The meeting moved to noon", "make this formal"),
    ("blah blah blah. Prompt A.I. this was a test, so simply reply past",
     "blah blah blah", "this was a test, so simply reply past"),
    ("prompt AI write a haiku about rain", "", "write a haiku about rain"),
    ("fix my typos please prompt AI", "fix my typos please", ""),
    ("notes here prompte I make it shorter end prompt", "notes here", "make it shorter"),
    ("No trigger at all.", "No trigger at all", ""),
])
def test_split(text, content, instruction):
    assert ec.split(text) == (content, instruction)


def test_end_prompt_alone_is_never_a_split_point():
    # A user list that contains the terminator must not split on it.
    assert ec.split("keep this end prompt", [r"end\s*prompt"]) == ("keep this end prompt", "")


def test_user_message_shapes():
    assert ec.user_message("Text", "do it") == "Editing instruction: do it\n\nText to edit:\nText"
    assert ec.user_message("", "do it") == "do it"
    assert ec.user_message("Text", "") == "Text"
    assert ec.user_message("", "") is None


def test_has_trigger():
    assert ec.has_trigger("ok prompt ai do it")
    assert not ec.has_trigger("a prompt answer")


def test_tidy():
    assert ec.tidy("<think>x</think> past") == "past"
    assert ec.tidy("past <think>cut off") == "past"
    assert ec.tidy("  past ") == "past"


def test_system_prompt_is_the_tuned_one():
    s = ec.SYSTEM_PROMPT
    assert s.startswith("You are a personal copy editor for voice dictation.")
    assert "An editing instruction from the speaker always overrides every rule above." in s


def test_contract_copy_matches_if_present():
    other = os.path.join(os.path.expanduser("~"), "AndroidStudioProjects",
                         "WhisperRemote_for_Android", "contracts", "03-edit-prompt.json")
    if not os.path.exists(other):
        pytest.skip("WhisperRemote repo not on this machine")
    with open(other, encoding="utf-8") as a, open(ec.PROMPT_PATH, encoding="utf-8") as b:
        assert json.load(a) == json.load(b)
