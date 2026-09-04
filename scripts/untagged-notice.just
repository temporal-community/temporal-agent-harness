# Shared "you're on an untagged commit" notice, imported by the root justfile and by each
# examples/*/justfile so every entry point carries it.
#
# This is a variable assignment rather than a recipe dependency on purpose: just evaluates it once
# per invocation, before any recipe runs, so a single line per justfile covers every recipe. It is
# NOT evaluated for `just --list`, `just --help`, or `just -n`, so browsing recipes stays quiet.
#
# The notice goes to stderr, which is also why the variable ends up empty and nothing is injected
# into any recipe.
#
# Silent when there is no git metadata at all: that means an unpacked release archive, which is
# precisely the recommended setup — warning there would scold the people who got it right. Also
# silent when git is missing, or when HEAD sits exactly on a tag (including a shallow clone made
# with `git clone --depth 1 --branch <tag>`).
_untagged_notice := ```
    if command -v git >/dev/null 2>&1 \
       && git rev-parse --git-dir >/dev/null 2>&1 \
       && ! git describe --exact-match --tags HEAD >/dev/null 2>&1; then
        if [ -n "${NO_COLOR:-}" ]; then y=''; r=''; else y='\033[33m'; r='\033[0m'; fi
        printf "$y\n  Running from an untagged commit.\n  This project is experimental and main is a development branch - we do our best\n  to keep it working, but make no promises. For a more reliable setup, use a tagged\n  release: https://github.com/temporal-community/temporal-agent-harness/releases\n$r\n" >&2
    fi
    true
    ```
