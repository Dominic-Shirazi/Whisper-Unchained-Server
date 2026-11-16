"""
Compare local abliterated models on the mean-mode (roast / tiktok / disses) prompt.

Mean mode is a custom trigger from the shared library
(trigger_library/triggers/mean-mode.json). This runs the SAME system prompt and
user message the API builds for it (api/custom_triggers.py) across every
candidate model that is currently installed in Ollama, over a set of
brain-dump test cases covering different reasoning-failure types.

For each model it reports:
  - objective scores: refused?, em-dashes (raw, pre-strip), markdown, hashtags/
    emoji, length-in-range, approx. insult echo, and speed (tokens/sec).
  - the actual output, so YOU can judge wit, voice-mirroring, and flaw-matching.

Usage:  .venv/Scripts/python scripts/compare_roast_models.py
Models still downloading are skipped and listed as PENDING; just re-run later.
"""

import json
import os
import re
import subprocess
import sys

import requests

# Windows consoles default to cp1252 and crash on smart quotes / dashes that the
# models emit. Force UTF-8 so we can print raw model output safely.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://127.0.0.1:11434/api/generate")

# Candidate models, matched by prefix against `ollama list` (so ":latest" /
# ":7b" tags resolve automatically). Add or remove freely.
CANDIDATE_PREFIXES = [
    "huihui_ai/qwen2.5-abliterate",
    "richardyoung/qwen2.5-14b-instruct-abliterated",
    "chatgpt1/qwythos-9b-claude-mythos-5-1m-abliterated",
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
import custom_triggers  # noqa: E402

MEAN_MODE_PATH = os.path.join(ROOT, "trigger_library", "triggers", "mean-mode.json")
INSULTS_HEADING = "Insults, grouped by the reasoning failure they target:"

# Brain-dumps chosen to hit different reasoning-failure categories.
TEST_CASES = [
    {
        "flaw": "ignores evidence",
        "text": (
            "ok so this guy in my comments keeps saying vaccines cause autism and "
            "when i linked him like three actual studies he just goes nah thats big "
            "pharma propaganda and repeats the same thing. prompt tiktok help me reply"
        ),
    },
    {
        "flaw": "confidently wrong",
        "text": (
            "this dude is SO sure the sun is closer to us in summer thats why its "
            "hot. bro its literally the axial tilt, its winter in australia right now. "
            "he keeps doubling down. prompt tiktok"
        ),
    },
    {
        "flaw": "fakes expertise",
        "text": (
            "some guy replied 'as a nutritionist' and then said seed oils are "
            "literally poison and will give you cancer, no source, and his bio just "
            "says he sells protein powder. prompt roast this guy"
        ),
    },
    {
        "flaw": "closed-minded",
        "text": (
            "i explained like 4 different ways why correlation isnt causation and he "
            "just keeps going 'well thats your opinion' and wont even engage with the "
            "point. prompt disses"
        ),
    },
]


def load_mean_mode() -> dict:
    with open(MEAN_MODE_PATH, encoding="utf-8") as f:
        return json.load(f)


def installed_models() -> list:
    out = subprocess.run(
        ["ollama", "list"], capture_output=True, text=True
    ).stdout.splitlines()
    names = []
    for line in out[1:]:  # skip header
        line = line.strip()
        if line:
            names.append(line.split()[0])
    return names


def resolve_models() -> tuple:
    have = installed_models()
    resolved, pending = [], []
    for pref in CANDIDATE_PREFIXES:
        match = next((n for n in have if n.startswith(pref)), None)
        (resolved if match else pending).append(match or pref)
    return resolved, pending


REFUSAL_MARKERS = [
    "i can't", "i cannot", "i won't", "i'm sorry", "i am sorry", "as an ai",
    "i'm not able", "i am not able", "cannot assist", "can't help with that",
    "not appropriate", "i must decline",
]


def strip_think(text: str) -> tuple:
    """Return (final_reply, was_thinking). Reasoning models leak a
    <think>...</think> block (sometimes only the closing tag); the real reply is
    whatever follows the last </think>."""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1].strip(), True
    if "<think>" in text:  # opened but never closed -> all reasoning, no reply
        return text.split("<think>", 1)[0].strip(), True
    return text, False


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower())


def insult_shingles(insults: str) -> list:
    grams = []
    for line in insults.splitlines():
        line = line.strip().lstrip("-* ").strip()
        words = normalize(line).split()
        for i in range(len(words) - 2):
            grams.append(" ".join(words[i:i + 3]))
    return grams


def score_output(text: str, shingles: list) -> dict:
    low = text.lower()
    norm = " " + normalize(text) + " "
    refused = any(m in low for m in REFUSAL_MARKERS)
    em = len(re.findall(r"[—–]", text))
    markdown = bool(re.search(r"(^|\n)\s*[-*#]|\*\*", text))
    junk = bool(re.search(r"#\w|[\U0001F300-\U0001FAFF☀-➿]", text))
    n = len(text)
    length_ok = 40 <= n <= 400
    echoes = sum(1 for g in set(shingles) if g and f" {g} " in norm)
    # Objective points out of 8.
    pts = 0
    pts += 0 if refused else 3
    pts += 2 if em == 0 else 0
    pts += 1 if not markdown else 0
    pts += 1 if not junk else 0
    pts += 1 if length_ok else 0
    return {
        "refused": refused, "em": em, "markdown": markdown, "junk": junk,
        "chars": n, "length_ok": length_ok, "insult_echo": echoes, "points": pts,
    }


def generate(model: str, system: str, prompt: str) -> dict:
    r = requests.post(
        OLLAMA_API_URL,
        json={"model": model, "prompt": prompt, "system": system, "stream": False},
        timeout=300,
    )
    r.raise_for_status()
    d = r.json()
    total_s = d.get("total_duration", 0) / 1e9
    eval_ct = d.get("eval_count", 0)
    tps = (eval_ct / (d.get("eval_duration", 1) / 1e9)) if d.get("eval_duration") else 0
    return {
        "text": d.get("response", "").strip(),
        "secs": total_s,
        "tok_s": tps,
    }


def main():
    mean_mode = load_mean_mode()
    system = custom_triggers.system_prompt(mean_mode)
    insults = mean_mode["prompt"].split(INSULTS_HEADING, 1)[-1]
    shingles = insult_shingles(insults)
    models, pending = resolve_models()

    if pending:
        print("PENDING (not installed yet, skipped):")
        for p in pending:
            print(f"  - {p}")
        print()
    if not models:
        print("No candidate models installed yet. Re-run once a download finishes.")
        sys.exit(0)
    print("Testing:", ", ".join(models), "\n")

    totals = {m: {"points": 0, "secs": 0.0, "thinks": False} for m in models}

    for case in TEST_CASES:
        print("=" * 78)
        print(f"CASE: {case['flaw']}")
        print(f"  brain-dump: {case['text']}")
        print("=" * 78)
        for m in models:
            try:
                res = generate(m, system, custom_triggers.user_message(case["text"]))
            except Exception as e:
                print(f"\n[{m}] ERROR: {e}")
                continue
            reply, thinks = strip_think(res["text"])
            sc = score_output(reply, shingles)
            totals[m]["points"] += sc["points"]
            totals[m]["secs"] += res["secs"]
            if thinks:
                totals[m]["thinks"] = True
            flags = []
            if thinks:
                flags.append(f"THINKS (raw {len(res['text'])} -> reply {len(reply)} chars)")
            if sc["refused"]:
                flags.append("REFUSED")
            if sc["em"]:
                flags.append(f"em-dash x{sc['em']}")
            if sc["markdown"]:
                flags.append("markdown")
            if sc["junk"]:
                flags.append("hashtag/emoji")
            if not sc["length_ok"]:
                flags.append(f"len={sc['chars']}")
            flagstr = ("  !! " + ", ".join(flags)) if flags else "  clean"
            print(f"\n--- {m}  [{sc['points']}/8, "
                  f"insult-echo={sc['insult_echo']}, "
                  f"{res['secs']:.1f}s, {res['tok_s']:.0f} tok/s]{flagstr}")
            print(reply)
        print()

    print("#" * 78)
    print("SUMMARY (objective points only, higher=better; read outputs for wit/voice)")
    print("#" * 78)
    ranked = sorted(models, key=lambda m: totals[m]["points"], reverse=True)
    maxpts = len(TEST_CASES) * 8
    for m in ranked:
        t = totals[m]
        note = "  <- emits <think> blocks; API would need to strip them" if t["thinks"] else ""
        print(f"  {t['points']:>3}/{maxpts}   {t['secs']:>5.1f}s total   {m}{note}")


if __name__ == "__main__":
    main()
