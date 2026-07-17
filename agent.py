# ─── IMPORTS ──────────────────────────────────────────────────────────
import re
import asyncio
import logging
import sqlite3
from typing import List, Optional
from datetime import datetime

from db import get_session, create_session, update_session, save_lead
from generator import call_gemini, extract_lead_profile
from model import LeadProfile
from config import (
    CRITICAL_FIELDS, STAGES, BOOKING_OFFERED_FOR,
    BOOKING_PHRASE, STAGE_TEMPERATURE,
)
from scorer import score_lead
from notifier import send_slack_alert, log_to_notion
from booking import (
    generate_available_slots, format_slots_for_prompt,
    confirm_booking, parse_booking_choice,
)

# ─── Logging ──────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ─── Language detection ──────────────────────────────────────────────
def detect_language(text: str) -> str:
    if re.search(r'[\u0600-\u06FF]', text):
        return "ar"
    lower = text.lower()
    french_markers = ['é', 'è', 'ê', 'à', 'ù', 'ç', 'ô', 'â', 'î',
                       'bonjour', 'merci', 'salut', 'besoin', 'site web']
    if any(m in lower for m in french_markers):
        return "fr"
    return "en"

# ─── FALLBACK EXTRACTION (regex) ─────────────────────────────────────
def _fallback_extract(text: str) -> dict:
    result = {"need": "", "budget": "", "timeline": "", "contact": "", "name": ""}

    # ─── Budget ──────────────────────────────────────────────────────
    # WHAT: Only ever guess a budget when the message actually contains
    #       a genuine currency signal: a $ sign (before OR after the
    #       number), a multiplier word (k/thousand/grand), a written
    #       currency name (dollars, euros, dinars...), or an explicit
    #       budget/cost/price word within a short distance of the digits.
    # WHY (bug fix): this used to fall back to a bare `(\d+)` match with
    #       NO currency context requirement at all — meaning literally
    #       any digits in the message (a phone number, a quantity, an
    #       address) could be misread as a dollar budget. Confirmed in
    #       testing: a lead who only ever gave a phone number (never a
    #       real budget) ended up with a phantom budget_usd_estimate
    #       that flipped their score from "cold" to "warm" — a fake lead
    #       the business owner would waste time chasing. This function
    #       is only ever a SUPPLEMENT to Gemini's context-aware
    #       extraction (see generator.py), so it's far better for it to
    #       stay silent (return "") than to guess wrong: a missed budget
    #       just means the bot asks again next turn; a WRONG one
    #       silently pollutes scoring and the business owner's CRM.
    budget_context = r'(?:budget|cost|price|spend|afford|pay|invest)'
    currency_word = r'(?:dollars?|usd|euros?|€|dinars?|\bda\b)'
    has_budget_context = bool(re.search(budget_context, text, re.IGNORECASE))

    budget_patterns = [
        r'\$\s?(?P<num>\d[\d,]*\.?\d*)\s*(?P<mult>k|thousand|grand|grands)?',   # "$5,000" / "$5k"
        r'(?P<num>\d[\d,]*\.?\d*)\s*(?P<mult>k|thousand|grand|grands)?\s?\$',   # "5,000$" / "5k$" (trailing symbol)
        r'(?P<num>\d[\d,]*\.?\d*)\s*(?P<mult>k|thousand|grand|grands)\b',        # "5k" / "2 grand" — no $ needed
        rf'(?P<num>\d[\d,]*\.?\d*)\s*{currency_word}',                           # "500 dollars" / "50000 DA"
        rf'{budget_context}\D{{0,15}}(?P<num>\d[\d,]*\.?\d*)',                   # "budget is 500" — last resort
    ]
    for i, pattern in enumerate(budget_patterns):
        # The last pattern is the ONLY one that accepts a bare number with
        # no $ sign, multiplier word, or currency name nearby — and it's
        # only tried at all when an explicit budget/cost/price word is
        # actually present in the message. Without that guard, this is
        # exactly the pattern that used to misread phone numbers as budgets.
        if i == len(budget_patterns) - 1 and not has_budget_context:
            continue
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            num_str = match.group('num')
            if not num_str or not num_str[0].isdigit():
                continue
            num = float(num_str.replace(',', ''))
            mult = match.groupdict().get('mult')
            if mult and mult.lower() in ('k', 'thousand', 'grand', 'grands'):
                num *= 1000
            result["budget"] = f"${int(num)}"
            break

    # ─── Timeline ────────────────────────────────────────────────────
    timeline_match = re.search(r'(\d+)\s*(week|month|day|year|weeks|months|days|years)', text, re.IGNORECASE)
    if timeline_match:
        num = timeline_match.group(1)
        unit = timeline_match.group(2)
        if unit.endswith('s'):
            unit = unit[:-1]
        result["timeline"] = f"{num} {unit}{'s' if int(num) > 1 else ''}"
    elif re.search(r'\b(ASAP|immediate|now|today)\b', text, re.IGNORECASE):
        result["timeline"] = "ASAP"

    # ─── Contact ────────────────────────────────────────────────────
    email = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    if email:
        result["contact"] = email.group(0)
    else:
        phone = re.search(r'(\+?[\d\s\-\(\)]{10,15})', text)
        if phone:
            result["contact"] = phone.group(0)

    # ─── Name ──────────────────────────────────────────────────────
    # WHAT: A conservative, English/French-only safety net for explicit
    #       self-introductions. Requires a capitalized word right after
    #       an introduction phrase, and stops after at most 3 words, so
    #       it doesn't grab a whole sentence.
    # WHY: this is deliberately narrow — it will miss plenty of real
    #       phrasing (especially Arabic, or casual "it's Sarah btw").
    #       That's an acceptable tradeoff for a fallback: Gemini's
    #       extraction (generator.py) is the real, robust path for
    #       capturing a name; this only needs to catch the most obvious
    #       cases when that call fails.
    name_patterns = [
        r"(?:[Mm]y name is|[Ii]'?m|[Ii] am)\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,2})",
        r"(?:[Jj]e m'appelle|[Mm]on nom est)\s+([A-Z][a-zA-ZÀ-ÿ'-]+(?:\s+[A-Z][a-zA-ZÀ-ÿ'-]+){0,2})",
    ]
    for pattern in name_patterns:
        name_match = re.search(pattern, text)
        if name_match:
            result["name"] = name_match.group(1).strip()
            break

    # ─── Need ──────────────────────────────────────────────────────
    need_keywords = {
        "website": "Website",
        "automation": "Automation",
        "booking": "Booking System",
        "restaurant": "Restaurant Management",
        "app": "Mobile App",
    }
    for keyword, value in need_keywords.items():
        if keyword in text.lower():
            result["need"] = value
            break

    return result


# ─── Per-session concurrency guard ────────────────────────────────────
# WHAT: Ensures messages for the SAME session_id are processed one at a
#       time, even if two requests for that session arrive concurrently
#       (a double-tap send, a client-side network retry, or any
#       integration that doesn't serialize its own requests the way
#       widget.html's isProcessing flag does for normal widget use).
# WHY: get_session() -> mutate in memory -> update_session() is a
#      read-modify-write with no locking of its own. Two concurrent
#      calls for the same session_id can each read the same starting
#      state, and whichever one calls update_session() LAST silently
#      overwrites the other's changes. Confirmed directly: racing "I
#      need a website" against "my budget is $4000" for the same
#      session_id lost the "need" field entirely, no error, no trace.
#      A per-session asyncio.Lock serializes processing for that one
#      session_id while every OTHER session stays fully concurrent —
#      this doesn't reduce overall throughput, it just makes each
#      individual conversation behave like a queue of one.
# HOW: the lock is created lazily per session_id and reference-counted,
#      so the dict doesn't grow forever — once no coroutine is waiting
#      on or holding a given session's lock, its entry is removed.
_session_locks: dict[str, asyncio.Lock] = {}
_session_lock_refcounts: dict[str, int] = {}
_session_locks_guard = asyncio.Lock()


async def _acquire_session_lock(session_id: str) -> asyncio.Lock:
    async with _session_locks_guard:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
            _session_lock_refcounts[session_id] = 0
        _session_lock_refcounts[session_id] += 1
        return _session_locks[session_id]


async def _release_session_lock(session_id: str) -> None:
    async with _session_locks_guard:
        _session_lock_refcounts[session_id] -= 1
        if _session_lock_refcounts[session_id] <= 0:
            _session_locks.pop(session_id, None)
            _session_lock_refcounts.pop(session_id, None)


# ─── MAIN ENTRY POINT ──────────────────────────────────────────────────
async def process_message(session_id: str, user_message: str) -> str:
    """
    Public entry point. Wraps the real logic in a per-session lock so
    concurrent requests for the SAME session_id are processed strictly
    one at a time — see the module-level comment above for why.
    """
    lock = await _acquire_session_lock(session_id)
    try:
        async with lock:
            return await _process_message_locked(session_id, user_message)
    finally:
        await _release_session_lock(session_id)


async def _process_message_locked(session_id: str, user_message: str) -> str:
    session = get_session(session_id)
    if not session:
        create_session(session_id)
        session = get_session(session_id)

    # ─── Early exit: closed ──────────────────────────────────────────
    if session.get("stage") == "closed":
        closing = {
            "en": "Thanks for reaching out! We'll be in touch soon. 👋",
            "ar": "شكراً للتواصل معنا! سنتابع معك قريباً. 👋",
            "fr": "Merci pour votre message ! Nous reviendrons vers vous bientôt. 👋",
        }
        lang = session.get("language", "en") or "en"
        return closing.get(lang, closing["en"])

    language = session.get("language", "")
    stage = session.get("stage", "greeting")
    history = session.get("history", [])
    profile_fields = session.get("profile_fields", {})

    if not language:
        language = detect_language(user_message)
        update_session(session_id, language=language)

    history.append({"role": "user", "content": user_message})

    # ─── Gemini extraction (best effort) ──────────────────────────────
    # _ discards the intermediate LeadProfile returned by _update_lead_profile;
    # we rebuild it from the merged profile_fields a few lines below.
    if stage not in ("scoring", "booking", "closed"):
        try:
            _, profile_fields = await _update_lead_profile(
                session_id, history, language, profile_fields, stage
            )
        except Exception as e:
            logger.warning(f"Gemini extraction failed: {e}")

    # ─── Always apply fallback for any missing fields ──────────────────
    fallback = _fallback_extract(user_message)
    for key in ["need", "budget", "timeline", "contact", "name"]:
        if not profile_fields.get(key) and fallback.get(key):
            profile_fields[key] = fallback[key]

    if fallback.get("budget") and not profile_fields.get("budget_usd_estimate"):
        # fallback["budget"] is already a fully-resolved "$<amount>" string
        # (any k/thousand/grand multiplier is applied inside _fallback_extract
        # itself now), so this just needs to pull the number back out.
        num_match = re.search(r'\$?(\d[\d,]*)', fallback["budget"])
        if num_match:
            profile_fields["budget_usd_estimate"] = float(num_match.group(1).replace(',', ''))

    if fallback.get("timeline") and not profile_fields.get("timeline_weeks_estimate"):
        num_match = re.search(r'(\d+)', fallback["timeline"])
        if num_match:
            profile_fields["timeline_weeks_estimate"] = float(num_match.group(1))

    update_session(session_id, profile_fields=profile_fields)

    lead_profile = LeadProfile(
        language=language,
        need=profile_fields.get("need", ""),
        budget=profile_fields.get("budget", ""),
        timeline=profile_fields.get("timeline", ""),
        contact=profile_fields.get("contact", ""),
        score="unknown",
        stage=stage,   # reflects the real session stage, not a hardcoded placeholder
    )

    # ─── If complete, score immediately ──────────────────────────────
    if stage not in ("scoring", "booking", "closed") and _is_complete(lead_profile):
        score = score_lead(lead_profile, profile_fields)
        lead_profile.score = score

        try:
            save_lead(session_id, lead_profile)
        except sqlite3.IntegrityError:
            logger.warning(f"Lead already saved for session {session_id} — skipping duplicate insert.")

        if score == "hot":
            _fire_and_forget(send_slack_alert(lead_profile, session_id), label="slack_alert")
        _fire_and_forget(log_to_notion(lead_profile, session_id), label="notion_log")

        if score in BOOKING_OFFERED_FOR:
            profile_fields["score"] = score
            profile_fields["slots_presented"] = False
            update_session(session_id, stage="booking", profile_fields=profile_fields)
            stage = "booking"
        else:
            update_session(session_id, stage="closed")
            cold_close = {
                "en": "Thanks for your interest! We'll be in touch soon. 👋",
                "ar": "شكراً لتواصلك معنا! سنعود إليك قريباً. 👋",
                "fr": "Merci pour votre intérêt ! Nous reviendrons vers vous bientôt. 👋",
            }
            return cold_close.get(language, cold_close["en"])

    # ─── Advance stage based on what's filled ──────────────────────────
    if stage not in ("scoring", "booking", "closed"):
        next_stage = _get_next_stage(stage, lead_profile)
        if next_stage != stage:
            update_session(session_id, stage=next_stage)
            stage = next_stage

    # ─── Handle booking stage ─────────────────────────────────────────
    if stage == "booking":
        # 1. Récupérer les créneaux proposés (les générer si absents)
        # NOTE: generate_available_slots() makes a live network call to the
        # Google Calendar freebusy API. Wrapped in asyncio.to_thread so it
        # doesn't block the event loop — without this, one visitor waiting
        # on Google Calendar would stall every other visitor's conversation.
        available_slots = profile_fields.get("offered_slots")
        if not available_slots:
            available_slots = await asyncio.to_thread(generate_available_slots)
            profile_fields["offered_slots"] = available_slots
            profile_fields["slots_presented"] = False
            update_session(session_id, profile_fields=profile_fields)

        # 2. Vérifier que l'utilisateur a bien fourni un email (contact)
        lead_email = profile_fields.get("contact", "")
        if not lead_email:
            system_prompt = _build_system_prompt("contact", language)
            bot_reply = await asyncio.to_thread(
                call_gemini,
                system_prompt=system_prompt,
                user_message=user_message,
                history=history,
                temperature=0.3,
            )
            history.append({"role": "model", "content": bot_reply})
            update_session(session_id, history=history)
            return bot_reply

        # 3. Vérifier si les créneaux ont déjà été présentés
        slots_presented = profile_fields.get("slots_presented", False)

        # 4. Créneaux déjà présentés → parser le choix de l'utilisateur
        if slots_presented:
            # Use the tested parse_booking_choice from booking.py (returns ISO string),
            # then resolve to the index that confirm_booking expects.
            slot_iso = parse_booking_choice(user_message, available_slots)
            slot_index = (
                next((s["index"] for s in available_slots if s["slot"] == slot_iso), None)
                if slot_iso else None
            )

            if slot_index is None:
                fallback_message = {
                    "en": "I didn't understand which slot you want. Please reply with the number (1, 2, 3, ...) or say the day and time (e.g., 'Thursday 10am').",
                    "fr": "Je n'ai pas compris quel créneau vous voulez. Veuillez répondre avec le numéro (1, 2, 3, ...) ou indiquer le jour et l'heure (ex: 'jeudi 10h').",
                    "ar": "لم أفهم أي موعد تريد. الرجاء الرد برقم (1، 2، 3، ...) أو ذكر اليوم والوقت (مثال: 'الخميس 10 صباحاً')."
                }
                bot_reply = fallback_message.get(language, fallback_message["fr"])
                history.append({"role": "model", "content": bot_reply})
                update_session(session_id, history=history)
                return bot_reply

            # Choix valide — confirmer la réservation
            # known_slots=available_slots is the fix for a real booking bug:
            # without it, confirm_booking() re-fetches a FRESH slot list from
            # Google Calendar and looks up slot_index in THAT list — which
            # can silently differ from what the user was actually shown (see
            # booking.py's confirm_booking docstring for the full story).
            # Passing the cached list guarantees the index means what the
            # user saw. Also run off the event loop: this makes a live
            # freebusy check + a calendar event creation, both blocking
            # network calls.
            result = await asyncio.to_thread(
                confirm_booking,
                slot_index=slot_index,
                lead_email=lead_email,
                lead_name=profile_fields.get("name", "Lead"),
                lead_need=profile_fields.get("need", ""),
                language=language,
                session_id=session_id,   # passed through for the DB race guard
                known_slots=available_slots,
            )

            if result["status"] == "success":
                update_session(session_id, stage="closed")
                history.append({"role": "model", "content": result["message"]})
                update_session(session_id, history=history)
                return result["message"]
            else:
                if "disponible" in result["message"].lower() or "available" in result["message"].lower():
                    # Créneau pris → régénérer les créneaux et remettre le flag à False
                    profile_fields["offered_slots"] = await asyncio.to_thread(generate_available_slots)
                    profile_fields["slots_presented"] = False
                    update_session(session_id, profile_fields=profile_fields)
                history.append({"role": "model", "content": result["message"]})
                update_session(session_id, history=history)
                return result["message"]

        # 5. Créneaux pas encore présentés → les afficher
        translations = {
            "en": {
                "intro": "I have a few time slots available for a 15-minute call to discuss your project in detail.",
                "ask": "Please pick a slot by number (1, 2, 3, ...) or say the day and time (e.g., 'Thursday 10am')."
            },
            "fr": {
                "intro": "J'ai quelques créneaux disponibles pour un appel de 15 minutes afin de discuter de votre projet en détail.",
                "ask": "Veuillez choisir un créneau par numéro (1, 2, 3, ...) ou indiquer le jour et l'heure (ex: 'jeudi 10h')."
            },
            "ar": {
                "intro": "لدي بعض المواعيد المتاحة لمكالمة مدتها 15 دقيقة لمناقشة مشروعك بالتفصيل.",
                "ask": "الرجاء اختيار موعد برقم (1، 2، 3، ...) أو ذكر اليوم والوقت (مثال: 'الخميس 10 صباحاً')."
            }
        }
        t = translations.get(language, translations["en"])
        slot_text = format_slots_for_prompt(available_slots)
        bot_reply = f"{t['intro']}\n\n{slot_text}\n\n{t['ask']}"

        profile_fields["slots_presented"] = True
        update_session(session_id, profile_fields=profile_fields)

        history.append({"role": "model", "content": bot_reply})
        update_session(session_id, history=history)
        return bot_reply

    # ─── Build system prompt ──────────────────────────────────────────
    system_prompt = _build_system_prompt(stage, language)

    # ─── Stage-appropriate temperature (from config) ──────────────────
    temperature = STAGE_TEMPERATURE.get(stage, 0.5)

    # ─── Call Gemini ──────────────────────────────────────────────────
    bot_reply = await asyncio.to_thread(
        call_gemini,
        system_prompt=system_prompt,
        user_message=user_message,
        history=history,
        temperature=temperature,
    )

    history.append({"role": "model", "content": bot_reply})
    update_session(session_id, history=history)

    return bot_reply


# ─── Background (fire-and-forget) task helper ─────────────────────────
# WHAT: Runs a coroutine (Slack alert, Notion log) without making the
#       caller wait for it.
# WHY: These are pure side-effects for the BUSINESS owner's benefit
#      (an internal notification, a CRM record) — they have nothing to
#      do with answering the lead. Previously both were awaited before
#      the bot's reply was returned, so a slow third-party API (Slack
#      or Notion having an off day, not even a failure) directly delayed
#      the lead's chat response. Measured directly: 2s each, awaited
#      sequentially, added 4s to a hot lead's reply time. Neither
#      function needs to be awaited to do its job — send_slack_alert()
#      and log_to_notion() already catch their own exceptions and log
#      success/failure internally (see notifier.py), so firing them in
#      the background loses nothing except the (unused) return value.
# HOW: asyncio.create_task() alone risks the task being garbage
#      collected mid-flight if nothing holds a reference to it — a
#      known asyncio gotcha. Keeping it in a module-level set (and
#      removing it on completion via add_done_callback) is the
#      standard fix, straight from the asyncio docs.
_background_tasks: set[asyncio.Task] = set()


async def _run_background(coro, label: str) -> None:
    """
    Runs a background coroutine and logs (rather than silently drops, or
    lets asyncio dump as a raw, context-free "Task exception was never
    retrieved" warning) any exception that escapes it. send_slack_alert()
    and log_to_notion() already catch their own errors internally and
    shouldn't raise — this is a defense-in-depth backstop for the day a
    future change in notifier.py (or a library update) breaks that
    assumption, confirmed worth having by testing exactly this case.
    """
    try:
        await coro
    except Exception as e:
        logger.error(f"Background task '{label}' failed unexpectedly: {e}", exc_info=True)


def _fire_and_forget(coro, label: str = "notification") -> None:
    task = asyncio.create_task(_run_background(coro, label))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ─── HELPER FUNCTIONS ──────────────────────────────────────────────────
def _build_system_prompt(stage: str, language: str) -> str:
    lang_label = language.upper() if language else "EN"

    stage_configs = {
        "greeting": {
            "task": "Greet the user warmly and ask ONE question: what do they need help with?",
            "forbidden": "Do not ask about budget, timeline, or contact yet.",
        },
        "need": {
            "task": "Ask the user to briefly describe what they need (e.g., a website, an automation tool, a booking system).",
            "forbidden": "Do not ask about budget, timeline, or contact yet. Do not ask follow-up questions about their business.",
        },
        "budget": {
            "task": "Ask the user for their rough budget range. A ballpark is fine.",
            "forbidden": "Do not ask about timeline or contact. Do not ask why they have that budget or how they arrived at it.",
        },
        "timeline": {
            "task": "Ask the user when they need this done — a rough timeline is enough.",
            "forbidden": "Do not ask about contact details. Do not ask why the deadline exists or what happens if it's missed.",
        },
        "contact": {
            "task": "Ask the user for their best contact email so the team can follow up.",
            "forbidden": "Do not ask for phone numbers, LinkedIn, or any other contact form. One email is enough.",
        },
        "scoring": {
            "task": "Acknowledge the user briefly. Say you have everything you need.",
            "forbidden": "Do not ask any questions. Do not summarise the conversation. One sentence only.",
        },
        "booking": {
            "task": (
                "Explain that these are time slots for a 15‑minute call with the business owner "
                "to discuss the project in detail. Then present the available slots and ask "
                "the user to pick one by number (1, 2, or 3)."
            ),
            "forbidden": (
                "CRITICAL — do NOT: ask discovery questions, ask about their team or POS system, "
                "ask about their decision‑making process, ask about risks or deadlines, "
                "pretend to send calendar invites (you cannot), or say anything other than "
                "presenting the slots and asking for a number."
            ),
        },
        "closed": {
            "task": "Thank the user and say goodbye. One or two sentences only.",
            "forbidden": "Do not ask any questions. Do not offer more help. The conversation is over.",
        },
    }

    config = stage_configs.get(stage, {
        "task": "Ask the user about their needs.",
        "forbidden": "Do not ask multiple questions at once.",
    })

    prompt = f"""You are a lead qualification assistant for a small business.
Reply in {lang_label}.

YOUR TASK: {config["task"]}

RULES:
- Reply in 1–3 short sentences maximum.
- Ask at most ONE question per reply.
- Never ask for information you already have.
- Never invent actions you cannot perform (sending emails, booking meetings, etc.).

FORBIDDEN: {config["forbidden"]}"""

    return prompt.strip()


def _is_complete(lead_profile: LeadProfile) -> bool:
    fields = {
        "language": lead_profile.language,
        "need": lead_profile.need,
        "budget": lead_profile.budget,
        "timeline": lead_profile.timeline,
        "contact": lead_profile.contact,
    }
    for field in CRITICAL_FIELDS:
        value = fields.get(field, "")
        if not value or value.strip() == "":
            return False
    return True


def _get_next_stage(current_stage: str, lead_profile: LeadProfile) -> str:
    stage_to_field = {
        "need": "need",
        "budget": "budget",
        "timeline": "timeline",
        "contact": "contact",
    }
    if current_stage in ("greeting", "need"):
        if lead_profile.need and lead_profile.budget and lead_profile.timeline:
            return "contact"
        elif lead_profile.need and lead_profile.budget:
            return "timeline"
        elif lead_profile.need:
            return "budget"
    if current_stage in stage_to_field:
        field = stage_to_field[current_stage]
        if getattr(lead_profile, field, ""):
            try:
                current_index = STAGES.index(current_stage)
                if current_index + 1 < len(STAGES):
                    return STAGES[current_index + 1]
            except ValueError:
                pass
    return current_stage


# ─── LEAD PROFILE EXTRACTION ──────────────────────────────────────────
async def _update_lead_profile(
    session_id: str,
    history: list,
    language: str,
    profile_fields: dict,
    stage: str = "greeting",
) -> tuple[LeadProfile, dict]:
    extracted = await asyncio.to_thread(extract_lead_profile, history)
    merged = {
        "name": (extracted.name if extracted and extracted.name else profile_fields.get("name", "")),
        "need": (extracted.need if extracted and extracted.need else profile_fields.get("need", "")),
        "budget": (extracted.budget if extracted and extracted.budget else profile_fields.get("budget", "")),
        "timeline": (extracted.timeline if extracted and extracted.timeline else profile_fields.get("timeline", "")),
        "contact": (extracted.contact if extracted and extracted.contact else profile_fields.get("contact", "")),
    }
    if extracted and extracted.budget_usd_estimate is not None:
        profile_fields["budget_usd_estimate"] = extracted.budget_usd_estimate
    if extracted and extracted.timeline_weeks_estimate is not None:
        profile_fields["timeline_weeks_estimate"] = extracted.timeline_weeks_estimate
    profile_fields.update(merged)
    update_session(session_id, profile_fields=profile_fields)
    lead_profile = LeadProfile(
        language=language,
        need=merged["need"],
        budget=merged["budget"],
        timeline=merged["timeline"],
        contact=merged["contact"],
        score="unknown",
        stage=stage,
    )
    return lead_profile, profile_fields
