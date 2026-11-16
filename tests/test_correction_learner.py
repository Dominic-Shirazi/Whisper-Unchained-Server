"""Learning from corrections: the comparison and the watch (pure parts)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import correction_learner as cl  # noqa: E402

PASTED = "I sent the invoice to Corey yesterday and he approved it."


def test_locate_needs_the_text_exactly_once():
    assert cl.locate(PASTED, "Hi. " + PASTED + " Thanks") == ("Hi. ", " Thanks")
    assert cl.locate(PASTED, "something else") is None
    assert cl.locate(PASTED, PASTED + " " + PASTED) is None        # twice: can't tell which
    assert cl.locate("okay", "okay") is None                       # one word: nothing to anchor on
    assert cl.locate(PASTED, PASTED + "x" * cl.FIELD_MAX) is None   # a document, not a text box


def test_region_follows_edits_inside_and_stops_for_edits_outside():
    anchors = cl.locate(PASTED, "Hi. " + PASTED + " Thanks")
    fixed = PASTED.replace("Corey", "Cory")
    assert cl.region(anchors, "Hi. " + fixed + " Thanks") == fixed
    assert cl.region(anchors, "Hi. " + fixed + " Thanks a lot") is None   # typed after it
    assert cl.region(anchors, "") is None                                  # sent / cleared
    assert cl.region(anchors, None) is None


def test_a_fixed_name_is_found():
    assert cl.substitutions(PASTED, PASTED.replace("Corey", "Cory")) == [("Corey", "Cory")]
    assert cl.substitutions("ask olama to run it", "ask Ollama to run it") == [("olama", "Ollama")]
    assert cl.substitutions("call the head pc now", "call the Head-PC now") == [("head pc", "Head-PC")]


def test_things_that_are_not_mishearings():
    assert cl.substitutions(PASTED, PASTED) == []
    assert cl.substitutions(PASTED, PASTED.replace("sent", "Sent")) == []            # capitals only
    assert cl.substitutions(PASTED, PASTED.replace("approved", "approves")) == []    # tense
    assert cl.substitutions("we have two cats", "we have two cat") == []             # plural
    assert cl.substitutions("meet at 3 pm today", "meet at 4 pm today") == []        # a number
    assert cl.substitutions("it was good to see you", "it was nice to see you") == []  # wording
    assert cl.substitutions("tell him about it", "tell her about it") == []          # common words
    assert cl.substitutions("the plan is fine", "the plan is fine, honestly") == []  # added, not fixed
    assert cl.substitutions(PASTED, "Completely different sentence about lunch.") == []  # a rewrite
    assert cl.substitutions("send it to Bob", "send it to Zygmunt") == []            # nothing alike
    assert cl.substitutions(PASTED, "") == [] and cl.substitutions(None, PASTED) == []


def test_at_most_two_offers_and_no_repeats():
    old = ("Corey met with Corey and with Leslie and then with Jon at the Olama office "
           "on the day before the long weekend started for everyone")
    new = old.replace("Corey", "Cory").replace("Leslie", "Lesley").replace("Jon", "John")              .replace("Olama", "Ollama")
    assert cl.substitutions(old, new) == [("Corey", "Cory"), ("Leslie", "Lesley")]


def test_known_and_refused_pairs_are_not_offered():
    pairs = [("Corey", "Cory"), ("olama", "Ollama")]
    assert cl.new_pairs(pairs, corrections={"corey": "Cory"}) == [("olama", "Ollama")]
    assert cl.new_pairs(pairs, corrections={"Corey": "Kory"}) == pairs       # a different fix: offer
    assert cl.new_pairs(pairs, skipped=[("OLAMA", "Ollama")]) == [("Corey", "Cory")]


def test_never_for_this_is_remembered(tmp_path):
    path = str(tmp_path / "skipped.json")
    assert cl.load_skipped(path) == []
    cl.add_skipped(("Corey", "Cory"), path)
    cl.add_skipped(("corey", "Cory"), path)                          # same pair: once
    cl.add_skipped(("olama", "Ollama"), path)
    assert cl.load_skipped(path) == [("corey", "Cory"), ("olama", "Ollama")]
    cl.add_skipped(("x", "y"), str(tmp_path / "no" / "such" / "dir.json"))   # never raises


def test_switch_and_words():
    assert cl.enabled({}) and cl.enabled({"LEARN_CORRECTIONS": "true"})
    assert not cl.enabled({"LEARN_CORRECTIONS": "false"})
    assert cl.offer_line(("Corey", "Cory")) == "Corey  →  Cory"
    assert cl.OFFER_BUTTONS == ("Always", "Not now", "Never for this")
    assert "Cory" in cl.saved_message(("Corey", "Cory"))[0]


# --- the watch ------------------------------------------------------------------

def box(text):
    return "Hi. " + text + " Thanks"


def test_watch_offers_after_the_edits_go_quiet():
    w = cl.Watch(PASTED, box(PASTED), now=0)
    assert not w.done
    assert not w.look(box(PASTED), 1)
    assert not w.look(box(PASTED.replace("Corey", "Cor")), 2)       # mid-edit
    assert not w.look(box(PASTED.replace("Corey", "Cory")), 3)
    assert not w.look(box(PASTED.replace("Corey", "Cory")), 4)      # quiet for 1 s: wait
    assert w.look(box(PASTED.replace("Corey", "Cory")), 3 + cl.QUIET_SECONDS)
    assert w.pairs == [("Corey", "Cory")]


def test_watch_stops_when_the_message_is_sent_or_you_click_away():
    fixed = box(PASTED.replace("Corey", "Cory"))
    w = cl.Watch(PASTED, box(PASTED), now=0)
    w.look(fixed, 1)
    assert w.look("", 1.5) and w.pairs == [("Corey", "Cory")]       # sent: the box emptied
    w = cl.Watch(PASTED, box(PASTED), now=0)
    w.look(fixed, 1)
    assert w.look(fixed, 1.5, still_there=False) and w.pairs == [("Corey", "Cory")]
    w = cl.Watch(PASTED, box(PASTED), now=0)
    assert w.look(None, 1) and w.pairs == []                        # nothing was changed


def test_watch_gives_up_after_its_time_and_on_an_unreadable_box():
    w = cl.Watch(PASTED, box(PASTED), now=0)
    assert not w.look(box(PASTED), cl.WATCH_SECONDS - 1)
    assert w.look(box(PASTED), cl.WATCH_SECONDS) and w.pairs == []
    assert cl.Watch(PASTED, "a box without it", now=0).done
    assert cl.Watch(PASTED, None, now=0).done


def test_typing_on_after_the_dictation_ends_the_watch_without_an_offer():
    w = cl.Watch(PASTED, PASTED, now=0)
    assert not w.look(PASTED + " And another thing", 1)             # still the same box
    assert w.look(PASTED + " And another thing", 1 + cl.QUIET_SECONDS) and w.pairs == []
