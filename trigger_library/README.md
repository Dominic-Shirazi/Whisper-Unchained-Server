# Trigger library

A trigger is a phrase you say while dictating that stands for a saved instruction to
your AI. Say "prompt ticket" and a rambling bug description comes out as a ticket; say
"prompt markdown" and the dictation is laid out as Markdown. You never have to explain
what you want twice.

**To use one:** Settings > Words & Triggers > Custom triggers > Browse the library, then
Add. Every trigger can be edited after you add it: the phrase, the instruction, all of it.

**To share yours:** in Settings, press Share next to your trigger, or see
[CONTRIBUTING.md](CONTRIBUTING.md).

"Needs a strong model" means a large model (a 20B+ local model, or a cloud model such as
Claude or GPT) does it reliably and a small local model often won't.

## Work writing

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Bug ticket](triggers/ticket.json) | "prompt ticket", "ticket this" | A spoken bug description becomes a ticket: title, steps, expected, actual. | Any model |
| [Commit message](triggers/commit.json) | "prompt commit" | A spoken description of a change becomes a git commit message. | Any model |
| [Email from notes](triggers/email.json) | "prompt email" | Say the gist; get a complete, polite email with greeting and sign-off. | Any model |
| [Meeting notes](triggers/meeting-notes.json) | "prompt meeting notes" | A spoken recap becomes decisions, action items with owners, and open questions. | Any model |
| [Stand-up update](triggers/standup.json) | "prompt stand up" | Talk through your day; get Yesterday, Today and Blockers. | Any model |

## Prompts for AI

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Build an AI prompt](triggers/prompt-builder.json) | "prompt builder" | A loose spoken request becomes a well-structured prompt for another AI. | Needs a strong model |
| [Feature spec](triggers/spec.json) | "prompt spec" | A spoken feature idea becomes a short spec with acceptance criteria. | Needs a strong model |

## Formats

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Bullet list](triggers/bullets.json) | "prompt bullets", "prompt bullet list" | Your dictation as a tidy bullet list, one point per line. | Any model |
| [Chat message](triggers/chat-message.json) | "prompt slack", "prompt chat message" | A short, friendly message for Slack, Teams or Discord. | Any model |
| [Markdown](triggers/markdown.json) | "prompt markdown", "output in markdown" | Your dictation, laid out as proper Markdown with headings and lists. | Any model |
| [Regular expression](triggers/regex.json) | "prompt regex", "prompt regular expression" | Describe the pattern in words; get the regular expression. | Needs a strong model |
| [Shell command](triggers/shell-command.json) | "prompt shell command", "prompt command line" | Say what you want done; get the command to paste into a terminal. | Needs a strong model |
| [Spreadsheet formula](triggers/formula.json) | "prompt formula" | Describe the calculation; get an Excel or Google Sheets formula. | Needs a strong model |
| [Write it in Python](triggers/python.json) | "prompt python" | Describe what the code should do; get the Python. | Needs a strong model |

## Professions

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Client update](triggers/client-update.json) | "prompt client update" | Rough job notes become a polite progress message for a customer. | Any model |
| [Clinical note (SOAP)](triggers/chart-note.json) | "prompt chart note", "prompt soap note" | A clinician's spoken summary, organised as Subjective, Objective, Assessment, Plan. | Needs a strong model |
| [Property listing](triggers/listing.json) | "prompt listing" | A spoken walk-through of a property becomes a listing description. | Any model |

## Language and tone

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Fits in a post](triggers/post.json) | "prompt tweet", "prompt short post" | Your point in 280 characters or fewer, for X, Bluesky or Threads. | Any model |
| [Half the length](triggers/shorter.json) | "prompt shorter" | The same message in about half the words. | Any model |
| [Plain English](triggers/plain-english.json) | "prompt plain english" | Dense or technical text, rewritten so anyone can follow it. | Any model |
| [Translate to Spanish](triggers/spanish.json) | "prompt spanish" | Speak in your own language; paste natural Spanish. | Any model |

## Personal

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Journal entry](triggers/journal.json) | "prompt journal" | A spoken brain dump becomes a tidy entry, with to-dos pulled out underneath. | Any model |
| [Recipe](triggers/recipe.json) | "prompt recipe" | A recipe told out loud becomes an ingredient list and numbered steps. | Any model |

## Fun

| Trigger | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Mean mode](triggers/mean-mode.json) | "prompt mean mode", "prompt meme mode", "prompt roast", "prompt diss", "prompt tik tok", "prompt tick tock" | Turns your rough thoughts into a short, cutting comeback in your own voice. | Needs a strong model |

_This page is written by build.py from the files in triggers/. Don't edit it by hand._
