"""Optional dictation cleanup: one LLM pass that removes fillers, applies
self-corrections and spoken formatting, and follows a chosen style.

Off by default. Runs only when asked (processing=clean on a request, or
CLEANUP_ALWAYS=true), and never when a "prompt AI" / mean-mode trigger fired:
those already send the text to an LLM with the speaker's own instruction.

The prompt text lives in cleanup_prompt.json, shared byte-for-byte with the
WhisperRemote app (contracts/02-cleanup-prompt.json) so a dictation cleaned on
the phone with a cloud LLM and one cleaned here come out the same. The assembly
below is the reference implementation of contracts/02-cleanup.md.
"""
import json
import os
import re

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup_prompt.json")

with open(PROMPT_PATH, encoding="utf-8") as _f:
    PROMPT = json.load(_f)

OPTIONS = {k: tuple(v) for k, v in PROMPT["style"].items()}  # e.g. rewrite: (keep, fix, polish)
# Line order in the prompt = key order in the JSON (punctuation, rewrite, tone, shorten, emoji).
STYLE_ORDER = tuple(PROMPT["style"])
DEFAULTS = dict(PROMPT["defaults"])


def normalize_style(style):
    """Fill in defaults; returns (style, error). Unknown values are an error,
    not silently ignored, so a client typo is visible."""
    out = dict(DEFAULTS)
    for key, value in (style or {}).items():
        if value in (None, ""):
            continue
        value = str(value).strip().lower()
        if key not in OPTIONS:
            return None, "unknown cleanup option '%s'" % key
        if value not in OPTIONS[key]:
            return None, "cleanup %s must be one of: %s" % (key, ", ".join(OPTIONS[key]))
        out[key] = value
    return out, None


def build_system_prompt(style, vocabulary=None):
    """Assemble the system prompt exactly as contracts/02-cleanup.md describes."""
    style, err = normalize_style(style)
    if err:
        raise ValueError(err)
    parts = list(PROMPT["rules"])
    terms = [t.strip() for t in (vocabulary or []) if t and t.strip()]
    if terms:
        parts.append(PROMPT["vocabulary_rule"].replace("{terms}", ", ".join(terms)))
    if style["rewrite"] == "keep":
        style["tone"] = "neutral"  # changing tone means rewording, which "keep" forbids
    lines = ["- " + PROMPT["style"][key][style[key]] for key in STYLE_ORDER]
    if style["rewrite"] == "keep" and style["shorten"] != "off":
        lines.append("- " + PROMPT["keep_while_shortening"])
    parts.append(PROMPT["style_header"] + "\n" + "\n".join(lines))
    parts.append(PROMPT["final"])
    return "\n".join(parts)


def user_message(text):
    return PROMPT["user_template"].replace("{text}", text)


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_FENCE = re.compile(r"^```[\w-]*\n(.*)\n```$", re.S)
_TAGS = re.compile(r"^<(dictation|text|output)>\s*(.*?)\s*</\1>$", re.S | re.I)
_LABEL = re.compile(
    r"^(?:here(?:'s| is)(?: the| your)?(?: cleaned[- ]up| cleaned| corrected| edited)?"
    r" (?:text|dictation|version|transcript)|cleaned[- ]up (?:text|dictation))\s*:\s*",
    re.I)
_QUOTES = [('"', '"'), ("“", "”"), ("'", "'")]


def tidy_output(output, original):
    """Remove the wrapping models add around an answer: reasoning blocks, code
    fences, echoed tags, a 'Here is the cleaned text:' label, and quotes around
    the whole reply (unless the dictation itself was quoted)."""
    text = _THINK.sub("", output or "")
    # A reasoning block cut off by the token limit has no closing tag.
    text = re.split(r"<(?:think|thinking|reasoning)>", text, flags=re.I)[0].strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    m = _TAGS.match(text)
    if m:
        text = m.group(2).strip()
    if not _LABEL.match(original.strip()):
        text = _LABEL.sub("", text, count=1)
    stripped = original.strip()
    for open_q, close_q in _QUOTES:
        if (len(text) >= 2 and text.startswith(open_q) and text.endswith(close_q)
                and open_q not in text[1:-1] and not stripped.startswith(open_q)):
            text = text[1:-1].strip()
            break
    return text.strip()
