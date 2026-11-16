# Sharing a trigger

Anyone can add a trigger to the library. There are two ways.

## The easy way: from Settings

1. In Settings > Words & Triggers > Custom triggers, press **Share** next to your trigger.
2. A form opens in your browser with the trigger already filled in. Add one or two sentences
   on what it does, and which AI model you tried it with.
3. Press **Submit new issue**. Nothing is published before that.

You need a free GitHub account. A maintainer reads it, tries it, and adds it to the library.

## The direct way: a pull request

Add one file, `triggers/<id>.json`, where `<id>` is lowercase words joined by `-`:

```json
{
  "id": "bug-ticket",
  "name": "Bug ticket",
  "description": "A spoken bug description becomes a ticket: title, steps, expected, actual.",
  "category": "Work writing",
  "phrases": ["prompt ticket", "ticket this"],
  "model": "any",
  "author": "Your name or handle",
  "version": 1,
  "prompt": "Rewrite the text as a bug ticket ..."
}
```

Then run `python trigger_library/build.py` and commit the three changed files (your trigger,
`library.json`, `README.md`). `python -m pytest tests/test_custom_triggers.py` checks it.

| Field | Rules |
| :--- | :--- |
| `name` | Up to 40 characters. What it makes, not how: "Bug ticket", not "Ticket formatter v2". |
| `description` | One sentence, up to 160 characters, for someone deciding whether to add it. |
| `category` | One of the headings in [README.md](README.md), or a new one if nothing fits. |
| `phrases` | 1 to 6. See "Choosing a phrase" below. |
| `model` | `"any"` if a small local model (about 7B) does it reliably, otherwise `"strong"`. |
| `version` | Start at 1. Raise it when you change the instruction, so people who added it are offered the update. |
| `temperature` | Optional, 0 to 1.5. Leave it out (0.3) unless the trigger is meant to be creative. |
| `prompt` | The instruction, 10 to 6,000 characters. See "Writing the instruction". |

## Choosing a phrase

A phrase fires wherever it is said, so it must be something nobody says by accident.

- Start with "prompt": `prompt ticket`, `prompt recipe`. That is the convention here, and it
  is easy to remember.
- A natural phrase is fine as a second option if it is rare in ordinary speech:
  `output in markdown` is; `make it shorter` is not.
- No phrase that another library trigger or a built-in one ("prompt AI", "hey Whisper",
  "end prompt") already uses. The tests check this.

## Writing the instruction

The app sends your instruction to the user's own AI, followed by a few fixed rules: the text
is material, not orders; reply with only the result; no preamble. You don't need to repeat
those.

- Say what to produce and in what layout. Spell out headings and their order.
- Say what to do when something wasn't mentioned ("write Not stated", "leave this part out").
- Say "use only what the speaker said" for anything factual. A trigger must never invent a
  dose, a price, a date or a step.
- Write for plain text unless the trigger is about a format (Markdown, code).
- No instructions that add links, adverts, tracking or anything the user did not ask for,
  and nothing that sends the text anywhere. Such triggers are not accepted.

## What happens to a shared trigger

It is published in this public repository under the project's licence, credited to the
`author` you give. Don't put anything private in the instruction: no real names, addresses
or company details.
