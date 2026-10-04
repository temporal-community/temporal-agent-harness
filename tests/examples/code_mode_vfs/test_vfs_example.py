# ABOUTME: Tests for the Code Mode VFS example: the seed activity loads the sample skill files,
# the agent's Code Mode tool builds with its two mounts, and the expense-report instructions can
# be followed by a script over the seeded filesystem, the way the model is meant to.

from __future__ import annotations

from temporalio.testing import ActivityEnvironment

from examples.code_mode_vfs.activities import load_skills
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode import monty_stepper
from temporal_agent_harness.harness.code_mode.vfs import FileOp, perform

# What a model following skills/expense-report/SKILL.md would write.
_FOLLOW_EXPENSE_SKILL = """
from pathlib import Path
rules = []
for line in Path('/skills/expense-report/reference/categories.md').read_text().splitlines():
    cells = [c.strip() for c in line.strip('|').split('|')]
    if len(cells) == 2 and cells[0] not in ('Category', '---'):
        rules.append((cells[0], [w.strip() for w in cells[1].split(',')]))

totals = {}
rows = Path('/skills/expense-report/sample-expenses.csv').read_text().splitlines()[1:]
for row in rows:
    date, description, amount = row.split(',')
    category = 'Other'
    for name, words in rules:
        if any(w in description.lower() for w in words):
            category = name
            break
    totals[category] = totals.get(category, 0.0) + float(amount)

lines = ['# Expense report']
for name in sorted(totals, key=lambda n: -totals[n]):
    lines.append(f'## {name}: ${totals[name]:.2f}')
lines.append(f'**Total: ${sum(totals.values()):.2f}**')
with open('/workspace/expense-report.md', 'w') as f:
    f.write('\\n'.join(lines))
Path('/workspace/expense-report.md').read_text()
"""


async def test_load_skills_reads_every_shipped_file():
    files = await ActivityEnvironment().run(load_skills)
    assert {
        "expense-report/SKILL.md",
        "expense-report/reference/categories.md",
        "expense-report/sample-expenses.csv",
        "meeting-notes/SKILL.md",
        "meeting-notes/template.md",
    } <= set(files)
    assert files["expense-report/SKILL.md"].startswith("---\nname: expense-report\n")


async def test_the_expense_report_skill_can_be_followed_over_the_seeded_filesystem():
    seeded = await ActivityEnvironment().run(load_skills)

    async def seed() -> dict[str, str]:
        return seeded

    skills = agent.InMemoryFileSystem(seed=seed)
    mounts = [
        agent.Mount("/skills", skills, read_only=True),
        agent.Mount("/workspace", agent.InMemoryFileSystem()),
    ]
    tool = agent.code_mode_tool([], name="run_code", mounts=mounts)
    run = monty_stepper.ScriptRun(
        _FOLLOW_EXPENSE_SKILL, tool.__code_mode_stubs__, answer_os=_no_os, mounts=mounts
    )
    await skills._seed_now()
    step = run.start()
    while not step.done:
        [op] = step.awaiting
        assert isinstance(op, FileOp)
        try:
            result = await perform(op, op.mount.backend)
        except Exception as e:
            result = e
        step = run.resume({op.call_id: result})
    assert step.error is None, step.error
    assert step.output.splitlines() == [
        "# Expense report",
        "## Travel: $768.30",
        "## Software: $149.00",
        "## Meals: $107.65",
        "## Other: $75.00",
        "## Office: $24.99",
        "**Total: $1124.94**",
    ]


def _no_os(name: str, args: tuple) -> object:
    raise NotImplementedError(name)
