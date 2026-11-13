import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
import shortcuts  # noqa: E402


def test_names_are_hints_shortcuts_are_not():
    for t in ("Lesley", "Cory", "St. Louis", "Ollama", "C plus plus", "Bonsai"):
        assert shortcuts.is_spelling_hint(t), t
    for t in ("503-555-0100", "name@example.com", "cyrus-studio.com",
              "here's my link: cyrus-studio.com", "123 Main St, Portland",
              "Thanks for reaching out, talk soon", "https://x.y/z", ""):
        assert not shortcuts.is_spelling_hint(t), t
    assert shortcuts.spelling_hints({"a": "Lesley", "b": "x@y.com", "c": "Lesley"}) == ["Lesley"]
