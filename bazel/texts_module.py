"""Writes a module of string constants from text files, for harness_texts (bazel/texts.bzl).

    texts_module <label> <update target> <out> <text file>...

Each file becomes a constant named after it in upper case (`no_filesystem.txt` is
`NO_FILESYSTEM`), holding its contents without the final newline. The language follows the
output's extension: `.py` or `.ts`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(label: str, update: str, out: Path, texts: list[Path]) -> None:
    constants = {
        path.stem.upper(): path.read_text().removesuffix("\n") for path in sorted(texts)
    }
    comment = "#" if out.suffix == ".py" else "//"
    lines = [
        f"{comment} GENERATED from {texts[0].parent.as_posix()}/ by {label} (harness_texts).",
        f"{comment} Do not edit: change the text files and run `bazel run {update}`.",
        "",
    ]
    for name, text in constants.items():
        # A JSON string is also a valid Python and TypeScript string literal.
        literal = json.dumps(text, ensure_ascii=False)
        lines.append(f"{name} = {literal}" if out.suffix == ".py" else f"export const {name} = {literal};")
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], Path(sys.argv[3]), [Path(p) for p in sys.argv[4:]])
