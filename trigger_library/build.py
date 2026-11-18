"""Builds library.json and README.md from triggers/*.json.

    python trigger_library/build.py           rebuild both files
    python trigger_library/build.py --check   exit 1 if they are out of date

library.json is what every install downloads (Settings > Skills >
Browse the library), so it is only ever written from files that pass
custom_triggers.validate_library. tests/test_trigger_library.py runs --check.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "api"))

import custom_triggers  # noqa: E402

SOURCE = os.path.join(HERE, "triggers")
LIBRARY = os.path.join(HERE, "library.json")
README = os.path.join(HERE, "README.md")
FORMAT_VERSION = 1
# The order categories are shown in; any other category follows, A to Z.
CATEGORIES = ["Work writing", "Prompts for AI", "Formats", "Professions",
              "Language and tone", "Personal", "Fun"]
MODEL_NOTE = {"any": "Any model", "strong": "Needs a strong model"}


def read_sources():
    items = []
    for name in sorted(os.listdir(SOURCE)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(SOURCE, name), encoding="utf-8") as f:
            item = json.load(f)
        if item.get("id") != name[:-5]:
            raise SystemExit("%s: \"id\" must be \"%s\" (the file name)." % (name, name[:-5]))
        items.append(item)
    entries, err = custom_triggers.validate_library({"triggers": items})
    if err:
        raise SystemExit(err)
    order = {c: i for i, c in enumerate(CATEGORIES)}
    entries.sort(key=lambda e: (order.get(e["category"], len(order)), e["category"], e["name"].lower()))
    return entries


def library_text(entries, updated):
    return json.dumps({"version": FORMAT_VERSION, "updated": updated, "triggers": entries},
                      indent=1, ensure_ascii=False) + "\n"


def readme_text(entries):
    lines = [
        "# Skill library",
        "",
        "A skill is a phrase you say while dictating that stands for a saved instruction to",
        "your AI. Say \"prompt ticket\" and a rambling bug description comes out as a ticket; say",
        "\"skill grocery list\" and your shopping is grouped by aisle. You never have to explain",
        "what you want twice.",
        "",
        "**To use one:** Settings > Skills > Browse the library, then",
        "Add. Every skill can be edited after you add it: the phrase, the instruction, all of it.",
        "",
        "**To share yours:** in Settings, press Share next to your skill, or see",
        "[CONTRIBUTING.md](CONTRIBUTING.md).",
        "",
        "\"Needs a strong model\" means a large model (a 20B+ local model, or a cloud model such as",
        "Claude or GPT) does it reliably and a small local model often won't.",
        "",
    ]
    category = None
    for e in entries:
        if e["category"] != category:
            category = e["category"]
            lines += ["## " + category, "",
                      "| Skill | Say | What it does | Model |", "| :--- | :--- | :--- | :--- |"]
        phrases = ", ".join('"%s"' % p for p in e["phrases"])
        lines.append("| [%s](triggers/%s.json) | %s | %s | %s |" % (
            e["name"], e["id"], phrases, e["description"].replace("|", "\\|"),
            MODEL_NOTE[e["model"]]))
        nxt = entries.index(e) + 1
        if nxt == len(entries) or entries[nxt]["category"] != category:
            lines.append("")
    lines += ["_This page is written by build.py from the files in triggers/. Don't edit it by hand._", ""]
    return "\n".join(lines)


def current_updated():
    try:
        with open(LIBRARY, encoding="utf-8") as f:
            return json.load(f).get("updated", "")
    except Exception:
        return ""


def main(argv):
    entries = read_sources()
    readme = readme_text(entries)
    old_updated = current_updated()
    same_content = False
    try:
        with open(LIBRARY, encoding="utf-8") as f:
            same_content = f.read() == library_text(entries, old_updated)
        with open(README, encoding="utf-8") as f:
            same_content = same_content and f.read() == readme
    except OSError:
        same_content = False
    if "--check" in argv:
        if not same_content:
            print("trigger_library is out of date: run  python trigger_library/build.py")
            return 1
        print("trigger_library is up to date (%d triggers)." % len(entries))
        return 0
    if same_content:
        print("Nothing to do (%d triggers)." % len(entries))
        return 0
    with open(LIBRARY, "w", encoding="utf-8", newline="\n") as f:
        f.write(library_text(entries, datetime.date.today().isoformat()))
    with open(README, "w", encoding="utf-8", newline="\n") as f:
        f.write(readme)
    print("Wrote library.json and README.md (%d triggers)." % len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
