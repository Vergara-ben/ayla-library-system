"""Catch the escaping bug that silently kills a whole inline <script> block.

Writing `\\'` inside a single-quoted JS string produces an escaped backslash
followed by an unescaped quote, which terminates the string early and makes the
rest of the block a syntax error. The page still loads; every function in that
block is simply undefined. Same for `\\"` inside a double-quoted string.

A general JS lexer here would false-positive on regex literals and template
strings, so this checks only for that specific, high-damage sequence — plus
`\\n`, which renders a literal backslash-n instead of a line break.
"""
import glob
import io
import os
import re
import sys

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)

# A backslash-escaped backslash immediately before a quote or an n.
SUSPECT = re.compile(r"\\\\(['\"n])")

problems = []
templates = sorted(glob.glob("templates/**/*.html", recursive=True))

for path in templates:
    text = io.open(path, encoding="utf-8").read()
    for block in SCRIPT_RE.finditer(text):
        body = block.group(1)
        base_line = text[: block.start(1)].count("\n")
        for offset, line in enumerate(body.split("\n")):
            for match in SUSPECT.finditer(line):
                kind = ("quote — terminates the string early, breaking the whole block"
                        if match.group(1) in "'\""
                        else "newline — renders as a literal \\n in the dialog")
                problems.append((path.replace("\\", "/"),
                                 base_line + offset + 1, kind, line.strip()[:96]))

for path, lineno, kind, snippet in problems:
    print("%s:%d\n    double-escaped %s\n    %s" % (path, lineno, kind, snippet))

print("\n%d templates scanned — %d problem(s)" % (len(templates), len(problems)))
sys.exit(1 if problems else 0)
