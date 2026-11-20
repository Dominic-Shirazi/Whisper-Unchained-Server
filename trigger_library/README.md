# Skill library

A skill is a catchphrase you say while dictating that stands for a saved instruction to
your AI. Say "prompt ticket" and a rambling bug description comes out as a ticket; say
"skill grocery list" and your shopping is grouped by aisle. You never have to explain
what you want twice.

**To use one:** Settings > Skills > Browse the library, then
Add. Every skill can be edited after you add it: the catchphrase, the instruction, all of it.

**To share yours:** in Settings, press Share next to your skill, or see
[CONTRIBUTING.md](CONTRIBUTING.md).

"Needs a strong model" means a large model (a 20B+ local model, or a cloud model such as
Claude or GPT) does it reliably and a small local model often won't.

## Work writing

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Bug ticket](triggers/ticket.json) | "prompt ticket", "ticket this" | A spoken bug description becomes a ticket: title, steps, expected, actual. | Any model |
| [Commit message](triggers/commit.json) | "prompt commit" | A spoken description of a change becomes a git commit message. | Any model |
| [Email from notes](triggers/email.json) | "skill email", "prompt email" | Say the gist; get a complete, polite email with greeting and sign-off. | Any model |
| [Meeting notes](triggers/meeting-notes.json) | "skill meeting notes", "prompt meeting notes" | A spoken recap becomes decisions, action items with owners, and open questions. | Any model |
| [Stand-up update](triggers/standup.json) | "prompt stand up" | Talk through your day; get Yesterday, Today and Blockers. | Any model |

## Customers and sales

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Client call note](triggers/crm-note.json) | "skill call note", "prompt call note", "prompt crm note" | Talk through a call; get a one-line summary, the key details and the next follow-up, ready for a CRM's notes field. | Any model |
| [Client intake sheet](triggers/intake.json) | "prompt intake", "prompt client sheet" | A client's rambling email or call notes, pulled into the same fields in the same order every time. | Needs a strong model |
| [Client update](triggers/client-update.json) | "skill client update", "prompt client update" | Rough job notes become a polite progress message for a customer. | Any model |
| [Contact block](triggers/contact-block.json) | "skill contact", "prompt contact", "prompt contact block" | Someone's details, said or highlighted in any order, become a tidy contact block. Lines you didn't give are left out. | Any model |
| [Quick quote](triggers/quote.json) | "skill quote", "prompt quote", "prompt estimate" | Say the job and the prices; get a short estimate to text or email: scope, line items, total and timeline. | Needs a strong model |

## Prompts for AI

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Build an AI prompt](triggers/prompt-builder.json) | "prompt builder" | A loose spoken request becomes a well-structured prompt for another AI. | Needs a strong model |
| [Feature spec](triggers/spec.json) | "skill spec", "prompt spec" | A spoken feature idea becomes a short spec with acceptance criteria. | Needs a strong model |

## Formats

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Bullet list](triggers/bullets.json) | "skill bullets", "prompt bullets", "prompt bullet list" | Your dictation as a tidy bullet list, one point per line. | Any model |
| [Chat message](triggers/chat-message.json) | "prompt slack", "prompt chat message" | A short, friendly message for Slack, Teams or Discord. | Any model |
| [Markdown](triggers/markdown.json) | "prompt markdown", "output in markdown" | Your dictation, laid out as proper Markdown with headings and lists. | Any model |
| [Pros and cons](triggers/pros-cons.json) | "skill pros and cons", "prompt pros and cons" | Options you talked through, laid out as short pros and cons you can text to a partner or team. | Any model |
| [Regular expression](triggers/regex.json) | "prompt regex", "prompt regular expression" | Describe the pattern in words; get the regular expression. | Needs a strong model |
| [Shell command](triggers/shell-command.json) | "prompt shell command", "prompt command line" | Say what you want done; get the command to paste into a terminal. | Needs a strong model |
| [Spreadsheet formula](triggers/formula.json) | "prompt formula" | Describe the calculation; get an Excel or Google Sheets formula. | Needs a strong model |
| [Step-by-step instructions](triggers/steps.json) | "skill steps", "prompt instructions" | How you'd explain a task out loud, as numbered steps someone else can follow. | Any model |
| [Table](triggers/table.json) | "skill table", "prompt table" | Things you compared out loud, or a messy pasted list, laid out as a table with one row per thing. | Any model |
| [WhatsApp message](triggers/whatsapp.json) | "skill whatsapp", "prompt whatsapp" | A message laid out with WhatsApp's own formatting marks, so bold and lists show up properly. | Any model |
| [Write it in Python](triggers/python.json) | "prompt python" | Describe what the code should do; get the Python. | Needs a strong model |

## Professions

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Clinical note (SOAP)](triggers/chart-note.json) | "prompt chart note", "prompt soap note" | A clinician's spoken summary, organised as Subjective, Objective, Assessment, Plan. | Needs a strong model |
| [Property listing](triggers/listing.json) | "skill listing", "prompt listing" | Everything you can see or read about a property, in any order, becomes a listing in the same order every time. | Any model |

## Language and tone

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Fits in a post](triggers/post.json) | "prompt tweet", "prompt short post" | Your point in 280 characters or fewer, for X, Bluesky or Threads. | Any model |
| [Half the length](triggers/shorter.json) | "skill shorter", "prompt shorter" | The same message in about half the words. | Any model |
| [Plain English](triggers/plain-english.json) | "prompt plain english" | Dense or technical text, rewritten so anyone can follow it. | Any model |
| [Proofread only](triggers/proofread.json) | "skill proofread", "prompt proofread" | Fixes spelling, punctuation and grammar slips and changes nothing else: your words stay your words. | Any model |
| [Translate to Spanish](triggers/spanish.json) | "skill spanish", "prompt spanish" | Speak in your own language; paste natural Spanish. | Any model |
| [What's that word](triggers/that-word.json) | "skill word", "what's that word" | Describe the word you can't think of, right where it belongs; it is swapped for the word itself. | Any model |

## Personal

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Forum question](triggers/forum-question.json) | "skill forum question", "prompt forum question", "prompt ask the forum" | A problem you explain out loud becomes a clear question for Reddit, Nextdoor or Discord: context, what you tried, the exact question. | Needs a strong model |
| [Grocery list by aisle](triggers/grocery-list.json) | "skill grocery list", "prompt shopping list" | Say what you need in whatever order it comes to mind; get a shopping list grouped the way a store is laid out. | Any model |
| [Item for sale](triggers/item-listing.json) | "skill for sale", "prompt for sale" | A rambling description of something you are selling becomes a marketplace listing: title, condition, details, what's included. | Any model |
| [Journal entry](triggers/journal.json) | "prompt journal" | A spoken brain dump becomes a tidy entry, with to-dos pulled out underneath. | Any model |
| [Recipe](triggers/recipe.json) | "skill recipe", "prompt recipe" | A recipe told out loud becomes an ingredient list and numbered steps. | Any model |

## Fun

| Skill | Say | What it does | Model |
| :--- | :--- | :--- | :--- |
| [Mean mode](triggers/mean-mode.json) | "prompt mean mode", "prompt meme mode", "prompt roast", "prompt diss", "prompt tik tok", "prompt tick tock" | Turns your rough thoughts into a short, cutting comeback in your own voice. | Needs a strong model |

_This page is written by build.py from the files in triggers/. Don't edit it by hand._
