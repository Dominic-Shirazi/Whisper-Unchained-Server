""""prompt AI" voice commands: split the dictation at the trigger phrase into
the text to work on and the spoken instruction, build the editor request, tidy
the reply.

Reference implementation of contracts/03-edit-commands.md. The prompt text and
default trigger phrases live in edit_prompt.json, shared byte-for-byte with the
WhisperRemote app so a command spoken on the phone (cloud preset) and one spoken
at the desk behave the same.
"""
import json
import os
import re

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edit_prompt.json")

with open(PROMPT_PATH, encoding="utf-8") as _f:
    PROMPT = json.load(_f)

SYSTEM_PROMPT = PROMPT["system_prompt"]


def _edit_patterns(patterns):
    """Trigger patterns that start a command. A pattern containing "end" is an
    'end prompt' terminator, never a split point (same rule the server has
    always used for user-edited trigger lists)."""
    return [p for p in (patterns if patterns is not None else PROMPT["trigger_patterns"])
            if "end" not in p.lower()]


def has_trigger(text, patterns=None):
    pats = _edit_patterns(patterns)
    return bool(pats) and re.search(r"\b(" + "|".join(pats) + r")\b", text, re.IGNORECASE) is not None


def split(text, patterns=None, end_patterns=None):
    """(content, instruction): everything before the first trigger is the text
    to work on, everything after it the instruction. 'end prompt' is removed from
    the instruction; surrounding punctuation is trimmed from both.

    end_patterns: the phrases that end an instruction (removed from it). None
    means the defaults in edit_prompt.json, and `patterns` is a legacy mixed list
    whose "end" entries are dropped. When given (trigger phrases from Settings),
    `patterns` is already only the start phrases and is used as-is, so a
    phrase like "weekend helper" isn't mistaken for a terminator."""
    if end_patterns is None:
        pats = _edit_patterns(patterns)
        end_patterns = PROMPT["end_patterns"]
    else:
        pats = list(patterns or [])
    strip_chars = PROMPT["strip_chars"]
    if not pats:
        return text.strip(strip_chars), ""
    m = re.search(r"\b(" + "|".join(pats) + r")\b", text, re.IGNORECASE)
    if not m:
        return text.strip(strip_chars), ""
    content, instruction = text[:m.start()], text[m.end():]
    for end in end_patterns:
        instruction = re.sub(r"\b(?:" + end + r")\b", " ", instruction, flags=re.IGNORECASE)
    return content.strip(strip_chars), instruction.strip(strip_chars)


def user_message(content, instruction):
    """The user turn for the editor, or None when there's nothing to send (the
    dictation was only the trigger phrase)."""
    if instruction and content:
        tpl = PROMPT["user_template_both"]
    elif instruction:
        tpl = PROMPT["user_template_instruction_only"]
    elif content:
        tpl = PROMPT["user_template_content_only"]
    else:
        return None
    return tpl.replace("{instruction}", instruction).replace("{content}", content)


def tidy(reply):
    """Keep only the final answer from a reasoning model: whatever follows the
    last </think>; if only <think> is present, whatever precedes it."""
    if "</think>" in reply:
        return reply.rsplit("</think>", 1)[1].strip()
    if "<think>" in reply:
        return reply.split("<think>", 1)[0].strip()
    return reply.strip()
