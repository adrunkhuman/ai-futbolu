"""Post-hoc Polish fixes on a finished transcript (models keep making these errors).

    uv run pipeline/postfix.py transcripts/episode-1.jsonl
Runs automatically at the end of room.py. Applied to `text` and, for cut lines, `interrupted_full`.
"""

import json
import re
import sys
from pathlib import Path

FIXES = [
    (r"\bSolnej\b", "Solnie"),   # locative of Solna: "w Solnie", not "w Solnej"
]


def fix_text(text: str) -> str:
    for pat, repl in FIXES:
        text = re.sub(pat, repl, text)
    return text


def fix_file(path: Path) -> int:
    entries = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    changed = 0
    for e in entries:
        for key in ("text", "interrupted_full"):
            if key in e and (new := fix_text(e[key])) != e[key]:
                e[key], changed = new, changed + 1
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
    return changed


if __name__ == "__main__":
    print(f"fixed {fix_file(Path(sys.argv[1]))} fields")
