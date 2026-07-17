# calendar_utils.py
import os
import json
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# === Chargement des variables d'environnement ===
load_dotenv()  # Lit le fichier .env

# === Configuration ===
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

# WHAT: The business's local timezone, used both to generate sensible
#       10am/2pm local slots and to tell Google Calendar which timezone
#       the event's start/end times are expressed in.
# WHY: This used to be hardcoded to "Africa/Algiers". Any other
#      deployment (a different client, a different country) needed a
#      code change to fix. BUSINESS_TIMEZONE is optional — unset, it
#      keeps the original default — but now a straightforward env var.
#      We validate at import time (fail fast, like GEMINI_API_KEY in
#      config.py) rather than discovering a typo the first time someone
#      tries to book a slot.
BUSINESS_TZ_NAME = os.getenv("BUSINESS_TIMEZONE", "Africa/Algiers")
try:
    BUSINESS_TZ = pytz.timezone(BUSINESS_TZ_NAME)
except pytz.exceptions.UnknownTimeZoneError:
    raise ValueError(
        f"❌ Invalid BUSINESS_TIMEZONE: '{BUSINESS_TZ_NAME}'. Must be a valid "
        "IANA timezone name, e.g. 'America/New_York', 'Europe/Paris', 'Africa/Algiers'. "
        "See the full list at https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
    )

# ─── Cached service singleton ──────────────────────────────────────
# WHAT: Module-level cache for the built Calendar API client.
# WHY: A single booking confirmation can call _get_calendar_service()
#      up to 3 times (list slots, freebusy check, create event). Each
#      call previously re-parsed the service account JSON, re-built an
#      RSA signer, and re-ran API discovery from scratch — all pure
#      overhead, since none of that depends on the current request.
#      Building it once and reusing it is strictly cheaper and behaves
#      identically (the client library handles token refresh internally
#      regardless of how long the object has been alive).
_calendar_service = None


def _get_calendar_service():
    """Construit (une seule fois) et retourne le service Google Calendar."""
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service

    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("❌ GOOGLE_CREDENTIALS_JSON non défini dans .env")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    _calendar_service = build("calendar", "v3", credentials=creds)
    return _calendar_service

def _get_busy_times(days_ahead=7):
    """Récupère les créneaux occupés depuis Google Calendar."""
    service = _get_calendar_service()
    now = datetime.now(BUSINESS_TZ)
    end = now + timedelta(days=days_ahead)

    body = {
        "timeMin": now.astimezone().isoformat(),
        "timeMax": end.astimezone().isoformat(),
        "items": [{"id": CALENDAR_ID}],
    }
    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][CALENDAR_ID]["busy"]
    return {(b["start"], b["end"]) for b in busy}

def _slot_is_busy(slot_dt: datetime, busy_times: set) -> bool:
    """Vérifie si un créneau de 15 minutes est occupé."""
    slot_end = slot_dt + timedelta(minutes=15)
    for start, end in busy_times:
        busy_start = datetime.fromisoformat(start)
        busy_end = datetime.fromisoformat(end)
        if slot_dt < busy_end and slot_end > busy_start:
            return True
    return False

def generate_available_slots(slot_count=6, days_ahead=7):
    """Génère les créneaux disponibles (jours ouvrés, 10h et 14h)."""
    busy_times = _get_busy_times(days_ahead)
    available = []
    index = 1

    current = datetime.now(BUSINESS_TZ).date() + timedelta(days=1)
    max_attempts = days_ahead * 5
    attempts = 0

    while len(available) < slot_count and attempts < max_attempts:
        attempts += 1
        if current.weekday() in [5, 6]:  # samedi, dimanche
            current += timedelta(days=1)
            continue

        for hour in [10, 14]:
            # WHAT: Attach the business timezone via .localize(), not by
            #       passing tzinfo=BUSINESS_TZ directly to datetime().
            # WHY (bug fix): this is a well-known pytz trap. A pytz tzinfo
            #       object's UTC offset depends on the specific instant
            #       (DST, or for older zones, historical Local Mean Time)
            #       — it can only compute the CORRECT offset when attached
            #       via .localize() (or via .fromutc(), which is what
            #       datetime.now(tz) uses internally, so that call is
            #       fine as-is). Passing tzinfo= directly to the datetime()
            #       constructor bypasses that entirely and silently uses
            #       the zone's raw/LMT offset instead of its real modern
            #       one. Confirmed directly: for Africa/Algiers this was
            #       off by 48 minutes — a slot generated and displayed as
            #       "2:00 PM" was actually booked in Google Calendar at
            #       2:48 PM, because the UTC conversion downstream used
            #       the wrong offset from the moment the slot was created.
            slot_dt = BUSINESS_TZ.localize(datetime(
                current.year, current.month, current.day,
                hour, 0
            ))
            if not _slot_is_busy(slot_dt, busy_times):
                available.append({
                    "slot": slot_dt.isoformat(),
                    "display": slot_dt.strftime("%a %I:%M %p"),
                    "index": index,
                })
                index += 1
                if len(available) >= slot_count:
                    break
        current += timedelta(days=1)

    return available


# calendar_utils.py - fonction create_calendar_event (version corrigée)

# calendar_utils.py - fonction create_calendar_event (version corrigée avec conversion UTC)

def create_calendar_event(slot_iso: str, lead_email: str, lead_name: str = "", lead_need: str = ""):
    """Crée un événement dans Google Calendar (sans lien Meet, sans invité)."""
    service = _get_calendar_service()
    start = datetime.fromisoformat(slot_iso)
    end = start + timedelta(minutes=15)

    # ✅ Conversion en UTC pour éviter les problèmes de fuseau horaire
    start_utc = start.astimezone(pytz.UTC)
    end_utc = end.astimezone(pytz.UTC)

    summary = f"Discovery call - {lead_name or 'New lead'}"
    if lead_need:
        summary += f" ({lead_need[:30]})"

    event = {
        "summary": summary,
        "description": f"15-minute intro call booked via chatbot.\nLead: {lead_email}\nNeed: {lead_need or 'Not specified'}",
        "start": {
            "dateTime": start_utc.isoformat(),  # Envoyer en UTC
            "timeZone": BUSINESS_TZ_NAME         # Mais afficher dans ce fuseau
        },
        "end": {
            "dateTime": end_utc.isoformat(),
            "timeZone": BUSINESS_TZ_NAME
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    created = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
        sendUpdates="none",
    ).execute()

    return created.get("htmlLink", "")