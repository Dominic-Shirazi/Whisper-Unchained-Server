import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import words_config as wc  # noqa: E402


def test_load_falls_back_to_example(tmp_path):
    ex = tmp_path / "ex.json"
    ex.write_text(json.dumps({"vocabulary": ["A"]}))
    assert wc.load(str(tmp_path / "missing.json"), str(ex)) == {"vocabulary": ["A"]}


def test_save_keeps_other_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"vocabulary": ["Old"], "trigger_patterns": ["x"], "_comment": "keep"}))
    wc.save(["New"], {"Leslie": "Lesley"}, path=str(p))
    d = json.loads(p.read_text())
    assert d == {"vocabulary": ["New"], "trigger_patterns": ["x"], "_comment": "keep",
                 "name_corrections": {"Leslie": "Lesley"}}


def test_clean_vocabulary():
    assert wc.clean_vocabulary([" Qwen ", "qwen", "", "Ollama,"]) == ["Qwen", "Ollama"]


def test_clean_corrections():
    assert wc.clean_corrections([("Leslie", "Lesley"), ("", "")]) == ({"Leslie": "Lesley"}, None)
    assert wc.clean_corrections([("Emma", "")])[1]
    assert wc.clean_corrections([("a", "b"), ("A", "c")])[1]


def test_hint_chars_matches_server_join():
    assert wc.hint_chars(["Qwen", "Ollama"], {"Leslie": "Lesley"}) == len("Qwen, Ollama, Lesley")


def test_save_keeps_words_added_elsewhere_but_honours_deletes(tmp_path):
    p = tmp_path / "c.json"
    # Page opened with [A, B]; meanwhile the phone added C.
    p.write_text(json.dumps({"vocabulary": ["A", "B", "C"]}))
    # User deleted B on the page and added D.
    wc.save(["A", "D"], {}, path=str(p), loaded_vocabulary=["A", "B"])
    assert json.loads(p.read_text())["vocabulary"] == ["A", "D", "C"]
