---
name: memory
description: Save, recall, update and forget facts about the user in /memory, which lasts across sessions.
---

# Memory

`/memory` is long-term memory. Unlike the conversation, it outlives this session: whatever you
write there is still there the next time the user starts a session with the same memory, and
other sessions sharing it may have written there since you last looked. Read it fresh every
time; never assume you know what is in it.

## Layout

```
/memory/
  MEMORY.md          # the index: one line per memory
  <slug>.md          # one memory per file, e.g. favorite-coffee.md
```

`MEMORY.md` has one line per memory, in the form
`- [<Title>](<slug>.md) — <one-line summary>`. A slug is short kebab-case naming the topic.
Each memory file follows `/skills/memory/template.md`. When `MEMORY.md` does not exist yet,
there are no memories.

## Recalling

1. Read `/memory/MEMORY.md`.
2. Read every memory file whose line looks relevant to the question.
3. Answer from what you found, saying it comes from memory. If nothing relevant is there, say
   you have nothing saved about it rather than guessing.

## Saving

1. Read `/memory/MEMORY.md` first. If a memory already covers the topic, update that file;
   don't write a second one.
2. Write the memory to `/memory/<slug>.md` from the template: one topic per file, a few short
   lines, the facts in the user's own terms.
3. Add its line to `MEMORY.md`, or update the line if the summary changed. Create `MEMORY.md`
   with a `# Memory` heading if it does not exist.
4. Tell the user in one sentence what you saved.

## Forgetting and correcting

- When the user says a memory is wrong, rewrite its file and its index line.
- When the user asks you to forget something, delete its file and remove its line from
  `MEMORY.md`.

## Rules

- Save facts about the user and their preferences, plans and projects; not transcripts.
- Never save passwords, keys or other secrets, even when asked. Say why instead.
- Do each recall or save in as few scripts as you can: one script can read the index, read
  the files it names and print what it found.

## Example script

```python
from pathlib import Path

index = Path('/memory/MEMORY.md')
print(index.read_text() if index.exists() else 'No memories yet.')
```
