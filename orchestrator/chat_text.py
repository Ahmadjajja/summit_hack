"""Chat helpers shared by the ASI:One agent and the web UI."""
import re

HELP = """**Hi, I'm TripCheck.** I plan your trip, a separate AI checks the plan, and nothing is booked or paid until you say **approve**.

**Try it (demo data covers Japan, Nov 15–22, Tokyo and Osaka):**
1. `Japan Nov 15-22, Tokyo then Osaka for the dev events, budget $3,500`
2. `approve`
3. `Stay 2 more nights in Osaka`, then `approve`
4. `Cancel my Osaka hotel`, then `approve`"""

_MENTION = re.compile(r"@agent1[0-9a-z]{20,}\s*", re.I)
_HELP = re.compile(r"^\s*(hi|hello|hey|help|start|menu|what can you do|what do you do|who are you|"
                   r".*(created to do|what are you|how does this work|how do i use).*)\s*[?!.]*\s*$", re.I)


def clean(text: str) -> str:
    """Drop ASI:One's '@agent1…' mention and extra spaces."""
    return _MENTION.sub("", text or "").strip()


def is_help(text: str) -> bool:
    return bool(_HELP.match(text or ""))
