---
name: memory
description: Save, recall, correct and forget what you know about the user, in /memory, which lasts across sessions.
---

# Memory

`/memory` is your long-term memory of the user, kept as a knowledge bundle (the `run_code` tool
describes the format). Unlike the conversation, it outlives this session: whatever you write
there is still there the next time the user starts a session with the same memory, and other
sessions sharing it may have changed it since you last looked. Look it up fresh every time;
never assume you know what is in it.

## What to remember

- Facts about the user: their preferences, plans, the people in their life, their projects, and
  things they ask you to remember.
- One memory per concept, in the user's own terms, a few short lines of body.
- Not transcripts or summaries of the conversation.
- Never passwords, keys or other secrets, even when asked. Say why instead.

## Layout

- Concept types: `Preference`, `Person`, `Plan`, `Project`, `Fact`, or another short descriptive
  type when none fits.
- Concepts start at the top of `/memory` (`/memory/favorite-coffee.md`). Once there are three
  or more of one kind, move them into a folder named for it (`people/`, `plans/`) and fix the
  links and indexes that pointed at them.
- `/memory/index.md` lists every top-level concept and folder; each folder has its own
  `index.md`. `/memory/log.md` records every change.

## Recalling

1. Call `okf_concepts()` and pick the concepts whose title, description or tags look relevant.
2. Read those files, and follow their links (`okf_links(id)`) when a related concept would
   help.
3. Answer from what you found, saying it comes from memory. A concept marked `stale` or
   `deprecated` may no longer be true: say so. If nothing relevant is there, say you have
   nothing saved about it rather than guessing.

## Saving

1. Call `okf_concepts()` first. If a concept already covers the topic, update that file; don't
   write a second one.
2. Write the concept with `okf_render`. Give `type`, `title`, a one-sentence `description` and
   `tags`. For anything with a date the user gave you, such as a trip or an appointment, set
   `stale_after` to the end of that day (`2026-10-14T23:59:59Z`).
3. Link it to the concepts it relates to: a plan to the people in it, a preference to the
   person it belongs to. Add the link back where it reads naturally.
4. Update the `index.md` it belongs in, and add a `log.md` entry.
5. Tell the user in one sentence what you saved.

## Correcting, confirming and forgetting

- When a fact simply was wrong, rewrite the concept.
- When something changed (the user used to like tea and now prefers coffee), keep the old
  concept with `status: deprecated` and a link to the new one, so the history is there.
- When the user confirms a memory is right, add `verified: [{by: "human:user"}]` to it.
- When the user asks you to forget something, delete the file, remove it from its index, and
  log the deletion without repeating what it said.

## Working efficiently

Do each recall or save in as few scripts as you can: one script can list the concepts, read the
ones it needs and print what it found; one script can write a concept, its index line and its
log entry together.

```python
import asyncio
from pathlib import Path

async def main():
    concepts = await okf_concepts()
    found = [c for c in concepts if "coffee" in (c["title"] + " " + " ".join(c.get("tags", []))).lower()]
    return [(c["title"], Path(c["path"]).read_text()) for c in found]

asyncio.run(main())
```
