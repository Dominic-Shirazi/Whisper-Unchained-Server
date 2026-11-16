"""Trigger phrases (api/triggers.py): plain phrases instead of regexes."""
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import edit_commands as ec  # noqa: E402
import triggers as tr  # noqa: E402
import words_config as wc  # noqa: E402

# The regexes the server shipped before trigger_phrases existed. The roast list
# is from when mean mode was built in; it is ignored now.
LEGACY = {
    "trigger_patterns": ["prompt[^a-zA-Z]{0,2}a[^a-zA-Z]{0,2}(?:eye|i)",
                         "prompte[\\s.,\\-]*i\\.?", "end\\s*prompt"],
    "roast_trigger_patterns": [
        "prompt\\s*a\\.?i\\.?\\W*(?:to\\s*)?roast", "prompt\\W*(?:to\\s*)?roast",
        "prompt\\s*a\\.?i\\.?\\W*(?:tik\\s*tok|tick\\s*tock)", "prompt\\W*(?:tik\\s*tok|tick\\s*tock)",
        "prompt\\s*a\\.?i\\.?\\W*(?:some\\s*)?diss(?:es)?", "prompt\\W*(?:some\\s*)?diss(?:es)?",
        "prompt\\s*a\\.?i\\.?\\W*(?:mean|meme)\\s*mode", "prompt\\W*(?:mean|meme)\\s*mode"],
}
DEFAULTS = {"trigger_phrases": tr.DEFAULT_PHRASES}


def _j(pats):
    return r"\b(" + "|".join(pats) + r")\b"


def route(config, text):
    """What the server does with `text` (mirrors process_transcribed_text):
    ("edit", content, instruction) / ("none",)."""
    p = tr.patterns(config)
    if p["edit"] and re.search(_j(p["edit"]), text, re.I):
        end = p["end"] if tr.uses_phrases(config) else None
        return ("edit",) + ec.split(text, p["edit"], end)
    return ("none",)


# Every utterance the trigger tests use (test_edit_commands, test_v1_api) plus
# the spellings the old regexes were written for.
SAME_AS_BEFORE = [
    "The meeting moved to noon. Prompt AI, make this formal.",
    "blah blah blah. Prompt A.I. this was a test, so simply reply past",
    "prompt AI write a haiku about rain",
    "fix my typos please prompt AI",
    "notes here prompte I make it shorter end prompt",
    "No trigger at all.",
    "fix this. prompt AI make it formal",
    "a prompt answer",
    "ok prompt ai do it",
    "My point stands. Prompt mean mode",
    "prompt AI meme mode",
    "prompt AI, mean mode.",
    "I prompted my mean mom",
    "promptai do it", "prompt a eye do it", "prompt A. Eye do it", "prompt A-I do it",
    "prompt a idea", "prompt a image",
    "Prompte I roast this guy",
    "prompt roast this guy", "prompt to roast him", "prompt AI to roast him", "prompt AI roast him",
    "prompt tiktok him", "prompt tik tok him", "prompt ticktock him", "prompt tick tock him",
    "Prompt AI TikTok this", "prompt a.i. tiktok this",
    "prompt disses", "prompt diss", "prompt some disses here", "prompt AI disses",
    "prompt AI some disses", "prompt meme mode", "promptmeanmode",
    "the weekend prompt was AI",
    "prompt AI make it shorter end prompt and more",
    "clean up my pot roast recipe prompt AI make it shorter",
    "Prompt AI. Make it a list. End prompt.",
    "prompte, i make it",
]


@pytest.mark.parametrize("text", SAME_AS_BEFORE)
def test_defaults_route_like_the_old_regexes(text):
    assert route(DEFAULTS, text) == route(LEGACY, text)


@pytest.mark.parametrize("text,content,instruction", [
    ("The meeting moved to noon. Prompt AI, make this formal.",
     "The meeting moved to noon", "make this formal"),
    ("blah blah blah. Prompt A.I. this was a test, so simply reply past",
     "blah blah blah", "this was a test, so simply reply past"),
    ("notes here prompte I make it shorter end prompt", "notes here", "make it shorter"),
])
def test_defaults_split(text, content, instruction):
    assert route(DEFAULTS, text) == ("edit", content, instruction)


@pytest.mark.parametrize("text", ["prompt a.i. fix it", "prompt A. Eye fix it", "promptai fix it",
                                  "Prompt, AI fix it", "prompt-AI fix it", "PROMPT AI fix it",
                                  "prompt a eye fix it", "prompt A.I fix it"])
def test_acronym_spellings(text):
    assert route(DEFAULTS, text) == ("edit", "", "fix it")


@pytest.mark.parametrize("text", ["prompt a idea", "prompt aim", "prompt Al", "prompter AI do it",
                                  "reprompt AI do it", "prompt a... i"])
def test_acronym_is_conservative(text):
    assert route(DEFAULTS, text) == ("none",)


def test_phrase_to_pattern_shapes():
    assert re.fullmatch(tr.phrase_to_pattern("hey GPT"), "hey gee pee tee", re.I)
    assert re.fullmatch(tr.phrase_to_pattern("hey GPT"), "Hey, G.P.T", re.I)
    # Lowercase "ai" is an ordinary word, not an acronym.
    assert re.fullmatch(tr.phrase_to_pattern("prompt ai"), "prompt ai", re.I)
    assert not re.fullmatch(tr.phrase_to_pattern("prompt ai"), "prompt a eye", re.I)
    # Special characters are literal; punctuation around the phrase is ignored.
    assert re.fullmatch(tr.phrase_to_pattern(' "C++ mode!" '), "c++ mode", re.I)
    assert tr.phrase_to_pattern("") == ""


def test_custom_phrase_jarvis():
    cfg = {"trigger_phrases": [{"phrase": "Jarvis", "action": "edit"},
                               {"phrase": "over", "action": "end"}]}
    assert route(cfg, "Lunch at noon. Jarvis, make it formal over") == (
        "edit", "Lunch at noon", "make it formal")
    assert route(cfg, "prompt AI make it formal") == ("none",)   # renamed away
    assert route(cfg, "jarvisx stays") == ("none",)


def test_rename_removes_old_phrase():
    rows = tr.rows({})
    rows[0]["phrase"] = "Jarvis"                       # "prompt AI" -> "Jarvis"
    clean, err = tr.validate(rows)
    assert err is None
    cfg = {"trigger_phrases": clean}
    assert route(cfg, "notes. Jarvis shorten it")[0] == "edit"
    assert route(cfg, "notes. prompt AI shorten it") == ("none",)
    assert route(cfg, "hey Whisper shorten it")[0] == "edit"  # its own row, untouched


def test_end_phrases():
    cfg = {"trigger_phrases": [{"phrase": "Jarvis", "action": "edit"},
                               {"phrase": "done done", "action": "end"}]}
    # An end phrase is never a split point; it only ends the instruction.
    assert route(cfg, "keep this done done") == ("none",)
    assert route(cfg, "text Jarvis bold it done done") == ("edit", "text", "bold it")
    # No end phrases at all: the instruction runs to the end.
    cfg = {"trigger_phrases": [{"phrase": "Jarvis", "action": "edit"}]}
    assert route(cfg, "text Jarvis bold it end prompt") == ("edit", "text", "bold it end prompt")
    # An edit phrase containing "end" is still a start phrase.
    cfg = {"trigger_phrases": [{"phrase": "weekend helper", "action": "edit"}]}
    assert route(cfg, "notes weekend helper tidy") == ("edit", "notes", "tidy")


def test_empty_phrase_list_means_no_triggers():
    cfg = dict(LEGACY, trigger_phrases=[])
    assert route(cfg, "prompt AI do it") == ("none",)
    assert tr.patterns(cfg) == {"edit": [], "end": []}


def test_legacy_fallback():
    p = tr.patterns(LEGACY)
    assert p["edit"] == LEGACY["trigger_patterns"][:2]
    assert p["end"] == ["end\\s*prompt"]
    assert "mean" not in p                     # roast_trigger_patterns is ignored
    assert tr.patterns({}) == {"edit": [], "end": []}
    assert not tr.uses_phrases(LEGACY)


def test_rows():
    assert tr.rows({}) == tr.DEFAULT_PHRASES
    rows = tr.rows(LEGACY)
    rows[0]["phrase"] = "changed"
    assert tr.DEFAULT_PHRASES[0]["phrase"] == "prompt AI"      # copies
    mine = [{"phrase": "Jarvis", "action": "edit"}]
    assert tr.rows({"trigger_phrases": mine}) == mine


def test_validate_cleans():
    clean, err = tr.validate([{"phrase": "  Jarvis  ", "action": "edit"},
                              ("", "edit"), ("   ", "end"),
                              ("fix   this!", "edit"), ("over", "end")])
    assert err is None
    assert clean == [{"phrase": "Jarvis", "action": "edit"},
                     {"phrase": "fix this", "action": "edit"},
                     {"phrase": "over", "action": "end"}]


@pytest.mark.parametrize("rows,needle", [
    ([("x", "edit")], 'Trigger phrase "x" is too short (2 letters minimum).'),
    ([("y" * 61, "edit")], "too long"),
    ([("hey, you", "edit")], "commas"),
    ([("hey\nyou", "edit")], "line breaks"),
    ([("123", "edit")], "at least one letter"),
    ([("?!", "edit")], "at least one letter"),
    ([("Jarvis", "shout")], "choose what it does"),
    ([("Jarvis", "mean")], "choose what it does (AI edit, End of instruction)"),
    ([("Jarvis", "edit"), ("jarvis", "end")], 'Trigger phrase "jarvis" is listed twice.'),
])
def test_validate_errors(rows, needle):
    clean, err = tr.validate(rows)
    assert clean is None and needle in err


def test_example_ships_the_defaults():
    with open(os.path.join(ROOT, "api", "processing_config.example.json"), encoding="utf-8") as f:
        ex = json.load(f)
    assert ex["trigger_phrases"] == tr.DEFAULT_PHRASES
    assert tr.validate(tr.DEFAULT_PHRASES) == (tr.DEFAULT_PHRASES, None)
    assert set(tr.ACTION_LABELS) == set(tr.ACTIONS) == {"edit", "end"}
    assert "roast_trigger_patterns" not in ex and "insults_file_path" not in ex


def test_saved_config_with_mean_rows_still_loads():
    # Mean mode used to be a third action. A file saved back then keeps its
    # "mean" rows: they are skipped, and edit / end work as before.
    cfg = {"trigger_phrases": [{"phrase": "prompt AI", "action": "edit"},
                               {"phrase": "prompt roast", "action": "mean"},
                               {"phrase": "prompt AI roast", "action": "mean"},
                               {"phrase": "end prompt", "action": "end"}]}
    assert tr.patterns(cfg) == {"edit": [tr.phrase_to_pattern("prompt AI")],
                                "end": [tr.phrase_to_pattern("end prompt")]}
    assert route(cfg, "notes. prompt AI shorten it end prompt") == ("edit", "notes", "shorten it")
    assert route(cfg, "my take. prompt roast") == ("none",)
    assert tr.rows(cfg) == [{"phrase": "prompt AI", "action": "edit"},
                            {"phrase": "end prompt", "action": "end"}]


# --- words_config.save(triggers=...) ----------------------------------------------

def test_save_writes_trigger_phrases_and_keeps_legacy_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(dict(LEGACY, vocabulary=["A"], _comment="keep")))
    rows = [{"phrase": "Jarvis", "action": "edit"}]
    wc.save(["A"], {}, path=str(p), triggers=rows)
    d = json.loads(p.read_text())
    assert d["trigger_phrases"] == rows
    assert d["trigger_patterns"] == LEGACY["trigger_patterns"] and d["_comment"] == "keep"
    assert wc.trigger_summary(d) == {"edit": 1, "end": 0}


def test_save_without_triggers_leaves_them_alone(tmp_path):
    p = tmp_path / "c.json"
    rows = [{"phrase": "Jarvis", "action": "edit"}]
    p.write_text(json.dumps({"trigger_phrases": rows}))
    wc.save(["B"], {}, path=str(p))
    assert json.loads(p.read_text())["trigger_phrases"] == rows


def test_trigger_summary_legacy():
    assert wc.trigger_summary(LEGACY) == {"edit": 2, "end": 1}
