#!/usr/bin/env python3
"""
Compile the Python embedded in GitHub workflow heredocs.

Several workflows here run inline scripts:

    run: |
      python3 - <<'PY'
      ...
      PY

YAML does not care whether that block is valid Python, and neither does
anything else until the workflow runs on a real trigger. A stray indentation
error therefore surfaces only after a push, a dispatch, and however long the
preceding steps take -- which is exactly how it surfaced.

This compiles every such block so the failure is caught locally in about a
second instead.

    python3 tools/check_workflow_python.py
"""

import glob
import os
import re
import sys

# Matches `python3 - <<'DELIM'` ... `DELIM`, capturing the body. The delimiter
# is quoted in all our workflows (so the shell does not expand the body), and
# backreferencing it keeps two adjacent blocks from being merged into one.
# The `-` (read program from stdin) is optional in practice: workflows here
# use both `python3 - <<'PY'` and `python3 << 'PYEOF'`. Missing the second form
# silently checked a third of the blocks and reported success.
BLOCK = re.compile(
    r"python3\s+(?:-u\s+)?(?:-\s+)?<<\s*'(?P<delim>\w+)'\n(?P<body>.*?)\n\s*(?P=delim)\s*$",
    re.S | re.M,
)


def dedent_yaml_block(body: str) -> str:
    """Strip the uniform YAML indentation the block sits at."""
    lines = body.split("\n")
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    if not indents:
        return body
    pad = min(indents)
    return "\n".join(l[pad:] if len(l) >= pad else l for l in lines)


def main() -> int:
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = sorted(glob.glob(os.path.join(root, ".github", "workflows", "*.yml")))
    failures, checked = [], 0

    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for m in BLOCK.finditer(text):
            checked += 1
            line_no = text[: m.start()].count("\n") + 1
            code = dedent_yaml_block(m.group("body"))
            try:
                compile(code, path, "exec")
            except SyntaxError as e:
                failures.append((path, line_no, e))

    name = os.path.relpath
    for path, line_no, e in failures:
        print(f"{name(path, root)}:{line_no}: {type(e).__name__}: {e.msg} "
              f"(block line {e.lineno})")

    if failures:
        print(f"\n{len(failures)} of {checked} embedded blocks failed to compile")
        return 1
    print(f"{checked} embedded Python blocks compile cleanly "
          f"across {len(paths)} workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
