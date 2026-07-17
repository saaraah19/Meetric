import os
from typing import Dict, Set
from dotenv import load_dotenv
load_dotenv()

# ─── Gemini ──────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise ValueError(
        "GEMINI_API_KEY is not set. Please set it in your .env file or environment."
    )
GEMINI_MODEL = "gemini-2.5-flash-lite"

# ─── Supported languages ──────────────────────────────────────────────
SUPPORTED_LANGUAGES = ["en", "ar", "fr"]

# ─── Qualifying question stages (order matters) ──────────────────────
# NOTE ON "scoring": this stage is intentionally never written to
# sessions.stage in practice. By the time all CRITICAL_FIELDS are known
# (agent.py's _is_complete check), agent.py scores the lead and moves it
# straight to "booking" or "closed" in the SAME request — there's no
# in-between turn where a session sits in "scoring". It's kept in STAGES
# (with its own prompt config and temperature entry) as an explicit,
# defensive placeholder — future changes that add an async/manual review
# step between "all fields known" and "route the lead" have a stage ready
# to use — not because it's reachable today. If you're debugging and
# expected to see stage == "scoring" in the database, you won't; that's
# expected, not a bug.
STAGES = ["greeting", "need", "budget", "timeline", "contact", "scoring", "booking", "closed"]

# ─── Lead scoring thresholds ──────────────────────────────────────────
HOT_BUDGET_THRESHOLD = 1000      # was MINIMUIM_BUDGET
HOT_TIMELINE_WEEKS = 4           # was MINIMUIM_TIMELINE_WEEKS

# ─── Critical fields (kept exactly as you wrote) ─────────────────────
CRITICAL_FIELDS = ["language", "need", "budget", "timeline", "contact"]

# ─── Score labels (fixed the leading space, made lowercase for code) ──
SCORE_LABELS = ["hot", "warm", "cold"]   # was SCORE = ["Hot"," Warm","Cold"]

# ─── Booking config (upgraded from your bool) ────────────────────────
BOOKING_OFFERED_FOR: Set[str] = {"hot", "warm"}  # which scores get a booking option
BOOKING_PHRASE = {
    "hot": "let's grab time now",                          # pushed
    "warm": "if you'd like, we can set up a quick call",  # offered
}
BOOKING_SLOT_COUNT = 3

# ─── Gemini temperature per stage ────────────────────────────────────
# WHAT: How creative/random Gemini should be at each stage.
# WHY: Greeting can be warm and varied (0.7); extracting contact info
#      or confirming a booking must be precise and consistent (0.0–0.1).
#      Kept here alongside the other stage config so changes to the
#      stage list and temperature policy stay in one place.
STAGE_TEMPERATURE: Dict[str, float] = {
    "greeting": 0.7,
    "need":     0.5,
    "budget":   0.3,
    "timeline": 0.3,
    "contact":  0.3,
    "scoring":  0.0,
    "booking":  0.1,
    "closed":   0.0,
}

# ─── History cap ──────────────────────────────────────────────────────
# WHAT: Max number of past turns sent to Gemini on each call.
# WHY: history is appended to forever and re-sent in full every turn.
#      A lead who chats for a while (or rambles) would otherwise mean
#      growing latency and token cost on every single message, including
#      the structured extraction call which runs on the FULL history
#      every turn regardless of stage. A qualification flow only needs
#      8 critical facts — it doesn't need the whole transcript once
#      that conversation runs long. 16 turns (~8 exchanges) comfortably
#      covers a normal greeting → contact flow with room for tangents.
MAX_HISTORY_TURNS = 16

# ─── Slack & Notion ──────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
