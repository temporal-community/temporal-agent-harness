"""Interactive terminal transcript for the agent CLI."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment, Segments
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme


class TerminalChat:
    def __init__(self, label: str, *, console: Console | None = None) -> None:
        self.console = console or Console(
            stderr=True, highlight=False, theme=Theme({"markdown.code": "bold cyan"})
        )
        self.label = label
        self.block: tuple[str, str] | None = None
        self.text = ""
        self.live: Live | None = None
        self.tools: dict[tuple[str, str], str] = {}
        self.spinner = Spinner("dots", text="Working…", style="cyan")

    def start(self, session: str, *, working: bool = True) -> None:
        self.console.print()
        self.console.rule(
            Text(f"temporal agent · {self.label}", style="bold cyan"), align="left"
        )
        self.console.print(Text(f"Session  {session}", style="dim"))
        self.console.print(
            Text("Ctrl+C to stop following · the agent keeps running", style="dim")
        )
        self.console.print()
        if working:
            self.wait("Working…")

    def user(self, text: str, *, working: bool = True) -> None:
        self.flush()
        self.console.print(Text("❯ You", style="bold"))
        self.console.print(Padding(Text(text), (0, 2)))
        self.console.print()
        if working:
            self.wait("Working…")

    def body(self) -> Padding:
        content = (
            Text(self.text, style="dim italic")
            if self.block[0] == "thought_summary"
            else Markdown(self.text, code_theme="monokai")
        )
        return Padding(content, (0, 2))

    def viewport(self) -> Segments:
        content = Group(
            *(
                Text(f"  ⋯ {name} · running", style="cyan")
                for name in self.tools.values()
            ),
            self.body() if self.block else self.spinner,
        )
        # Keep the live region within the screen so it can always be erased.
        lines = self.console.render_lines(content, self.console.options, pad=False)
        height = max(1, self.console.height - 4)
        return Segments(
            segment for line in lines[-height:] for segment in [*line, Segment.line()]
        )

    def begin_live(self) -> None:
        if self.live is None:
            self.live = Live(
                console=self.console,
                get_renderable=self.viewport,
                refresh_per_second=8,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start(refresh=True)

    def flush(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None
        if self.block:
            self.console.print(self.body())
            self.console.print()
        self.block = None
        self.text = ""

    def wait(self, text: str) -> None:
        self.flush()
        self.spinner.update(text=Text(text, style="dim"))
        self.begin_live()

    def progress(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        text = data["text"]
        agent = data.get("agent", "")
        if "session" in data:
            self.start(data["session"], working=not data.get("idle", False))
        elif event in ("reply_delta", "thought_summary"):
            if not text:
                return
            key = (event, agent)
            if self.block != key:
                self.flush()
                role = "Thinking" if event == "thought_summary" else self.label
                self.console.print(
                    Text(
                        f"● {role}",
                        style="dim" if event == "thought_summary" else "bold cyan",
                    )
                )
                self.block = key
            self.text += text
            self.begin_live()
        elif event in ("message_handler_start", "model_interaction_started"):
            self.wait(text)
        elif event in ("tool_start", "tool_end", "tool_error"):
            self.flush()
            key = (agent, data["tool_id"])
            name = text.removeprefix(event + ": ")
            if event == "tool_start":
                self.tools[key] = name
            else:
                self.tools.pop(key, None)
                symbol, style = ("✓", "green") if event == "tool_end" else ("✗", "red")
                self.console.print(Text(f"  {symbol} {name}", style=style))
            self.wait("Working…")
        elif event == "tool_approval_requested":
            self.flush()
            content = []
            for name, value in data["tool_input"].items():
                script = name == "script" and isinstance(value, str)
                content.extend(
                    [
                        Text(name, style="dim"),
                        Syntax(
                            value
                            if script
                            else json.dumps(value, indent=2, ensure_ascii=False),
                            "python" if script else "json",
                            line_numbers=script,
                            word_wrap=True,
                            background_color="default",
                        ),
                    ]
                )
            self.console.print(
                Panel(
                    Group(*content),
                    title=Text(f"Approval required: {data['tool_name']}"),
                    border_style="yellow",
                )
            )
        else:
            self.flush()
            self.console.print(Text(text, style="yellow"))

    def emit(
        self, kind: str, data: dict[str, Any], reply_text: str | None = None
    ) -> None:
        if kind == "user":
            self.user(data["text"], working=False)
            return
        if kind == "progress":
            self.progress(data)
            return
        self.flush()
        if "error" in data:
            self.console.print(Text(f"✗ {data['error']}", style="bold red"))
            return
        response = data.get("result", data)
        if response.keys() == {"text"} and isinstance(response["text"], str):
            if kind == "history" or response["text"] != reply_text:
                self.console.print(Text(f"● {self.label}", style="bold cyan"))
                self.console.print(
                    Padding(Markdown(response["text"], code_theme="monokai"), (0, 2))
                )
        else:
            self.console.print_json(json.dumps(response, ensure_ascii=False))
        if kind != "history":
            self.console.print(Text("✓ Done", style="dim"))
