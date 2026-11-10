import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))

from fillers import remove_fillers  # noqa: E402


@pytest.mark.parametrize("before,after", [
    ("I, um, think so.", "I think so."),
    ("Um, so the plan is set.", "So the plan is set."),
    ("It was uh fine.", "It was fine."),
    ("Yes, um.", "Yes."),
    ("Hmm... maybe later.", "Maybe later."),
    ("We could, uhh, wait. Umm, no.", "We could wait. No."),
])
def test_removes_filler_sounds(before, after):
    out, removed = remove_fillers(before)
    assert out == after
    assert removed


@pytest.mark.parametrize("text", [
    "I like it, you know, a lot.",          # real words: never touched
    "Uh-oh, the build broke.",               # hyphenated
    "Bring an umbrella.",                    # substring
    "She works at UM now.",                  # acronym in caps
    "Took him to the ER.",                   # not in the list
    "Cut it to 5 mm.",                       # not in the list
    "",
])
def test_leaves_real_words_alone(text):
    assert remove_fillers(text) == (text, [])


def test_reports_what_it_removed_for_the_log():
    _, removed = remove_fillers("Um, I, uh, think, um, yes.")
    assert [w.lower() for w in removed] == ["um", "uh", "um"]
