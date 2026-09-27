"""UserPromptSubmit / UserPromptExpansion hook for True View terminals.

    python -m clicker4ai.quiet_hook <sid>

terminal.claude_argv adds it with --settings (on top of the user's own
hooks). Exit code 2 makes Claude Code drop the prompt and show stderr; any
other outcome lets it through, so a broken hook never locks the terminal.
Kept to the stdlib and quiet.py: it runs once per prompt.
"""

from __future__ import annotations

import re
import sys

from .config import SESSIONS_DIR
from .quiet import TERM_EXEMPT_FILE, refusal


def main() -> int:
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"[0-9a-f]{1,32}", sid):
        return 0
    exempt = (SESSIONS_DIR / sid / TERM_EXEMPT_FILE).exists()
    reason = refusal({"quiet_exempt": exempt})
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
