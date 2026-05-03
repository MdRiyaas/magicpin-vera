"""
conversation_handlers.py — Multi-Turn Conversation Handling v2
magicpin AI Challenge — Mohamed Riyaas R

Fixed in v2:
  - Auto-reply: end after FIRST failed attempt (server.py aligned)
  - Added slot_pick intent for customer booking confirmations
  - Added customer branch (from_role awareness)
  - 3-strike dormancy exit stays the same
"""

import re
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: str
    from_role: str = "merchant"          # "merchant" | "customer"
    turns: list = field(default_factory=list)
    status: str = "open"                 # open | ended | waiting
    unanswered_count: int = 0
    auto_reply_count: int = 0


# ── Auto-reply detector ───────────────────────────────────────────────────────
AUTO_REPLY_RE = re.compile(
    r"(aapki jaankari ke liye|thank you for contact|i am (currently )?unavailable"
    r"|will get back to you|out of office|automated (message|reply|response)"
    r"|main ek automated|hum aapko jald)", re.I
)

def is_auto_reply(message: str, prior_msgs: list) -> bool:
    if AUTO_REPLY_RE.search(message):
        return True
    if prior_msgs.count(message) >= 1 and len(message) > 20:
        return True
    return False


# ── Intent detector ───────────────────────────────────────────────────────────
POSITIVE = [
    "yes", "ok", "sure", "haan", "bilkul", "karo", "go ahead",
    "please do", "let's do", "start", "shuru", "proceed",
    "send kar", "draft kar", "book kar"
]

EXIT = [
    "no", "nahi", "stop", "not interested", "baad mein",
    "later", "cancel", "unsubscribe", "mat karo", "band karo", "not now"
]

SLOT_RE = re.compile(
    r'\b(1|2|3|wed|thu|fri|sat|sun|mon|6pm|5pm|7pm|8am|9am|book|confirm|slot)\b',
    re.I
)

def detect_intent(message: str) -> str:
    m = message.lower()

    if any(s in m for s in EXIT):
        return "exit"

    if SLOT_RE.search(m):
        return "slot_pick"

    if any(s in m for s in POSITIVE):
        return "positive"

    if "?" in m or any(
        w in m for w in [
            "kya", "how", "when", "kab", "kitna",
            "kaun", "what", "which"
        ]
    ):
        return "question"

    return "neutral"


# ── Language detector ─────────────────────────────────────────────────────────
def detect_language(message: str) -> str:
    hi_chars = sum(1 for c in message if '\u0900' <= c <= '\u097F')

    if hi_chars > 3:
        return "hi"

    hi_words = [
        "karo", "haan", "nahi", "kya",
        "aap", "main", "mujhe", "hai", "tha", "ek", "ki"
    ]

    if sum(1 for w in hi_words if w in message.lower()) >= 2:
        return "hi-en"

    return "en"


# ── Main respond function ─────────────────────────────────────────────────────
def respond(state: ConversationState, message: str) -> dict:
    """
    Given conversation state + latest message, return the bot's next move.
    Returns dict:
    {
        action: send | wait | end,
        body?,
        cta?,
        rationale
    }
    """

    prior_msgs = [
        t["body"]
        for t in state.turns
        if t.get("from") == state.from_role
    ]

    # ── CUSTOMER BRANCH ───────────────────────────────────────────────────────
    if state.from_role == "customer":
        it = detect_intent(message)

        state.turns.append({
            "from": "customer",
            "body": message,
            "intent": it
        })

        if it == "slot_pick":
            m = message.lower()

            slot = (
                "Wednesday"
                if ("wed" in m or m.strip() == "1")
                else "Thursday"
                if ("thu" in m or m.strip() == "2")
                else "your preferred time"
            )

            body = (
                f"Confirmed! Your appointment is booked for {slot}. "
                f"We'll send a reminder an hour before. See you there! 😊"
            )

            state.turns.append({
                "from": "vera",
                "body": body
            })

            return {
                "action": "send",
                "body": body,
                "cta": "none",
                "rationale": "Customer confirmed slot; warm booking confirmation; no further CTA."
            }

        if it == "exit":
            state.status = "ended"

            return {
                "action": "send",
                "body": "No worries at all! Feel free to reach out whenever you're ready. We're here 😊",
                "cta": "none",
                "rationale": "Customer exit; warm close; door open."
            }

        if it == "positive":
            body = "Perfect! I'll confirm the details and send you a reminder closer to the date."

            state.turns.append({
                "from": "vera",
                "body": body
            })

            return {
                "action": "send",
                "body": body,
                "cta": "none",
                "rationale": "Customer confirmed; booking acknowledged."
            }

        body = "Thanks! Let me check the available options and get back to you shortly."

        state.turns.append({
            "from": "vera",
            "body": body
        })

        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Customer neutral; acknowledging and following up."
        }

    # ── MERCHANT BRANCH ───────────────────────────────────────────────────────

    # Auto-reply: try ONCE, then end
    if is_auto_reply(message, prior_msgs):

        if state.auto_reply_count == 0:
            state.auto_reply_count = 1

            state.turns.append({
                "from": "merchant",
                "body": message,
                "tag": "auto_reply"
            })

            body = (
                "Samajh gayi — aapki team tak pahunch gayi hogi. "
                "Kya aap khud 2 minute de sakte hain? "
                "Jo share karna tha woh 5-min ka useful kaam hai."
            )

            state.turns.append({
                "from": "vera",
                "body": body
            })

            return {
                "action": "send",
                "body": body,
                "cta": "binary_yes_stop",
                "rationale": "First auto-reply — one attempt to reach owner directly before exiting."
            }

        else:
            state.status = "ended"

            return {
                "action": "end",
                "rationale": "Second auto-reply. Gracefully exiting — will retry via different touchpoint."
            }

    # Reset on real reply
    state.auto_reply_count = 0

    it = detect_intent(message)
    lng = detect_language(message)

    state.turns.append({
        "from": "merchant",
        "body": message,
        "intent": it,
        "lang": lng
    })

    state.unanswered_count = 0

    # Exit intent
    if it == "exit":
        state.status = "ended"

        body = (
            "Bilkul samajh gaya! Koi baat nahi — jab chahein wapas aa jayein. Best of luck! 🙂"
            if lng in ("hi", "hi-en")
            else
            "Understood — no problem at all. Feel free to reach out whenever. Best wishes!"
        )

        state.turns.append({
            "from": "vera",
            "body": body
        })

        return {
            "action": "send",
            "body": body,
            "cta": "none",
            "rationale": "Merchant exit; warm farewell; door left open."
        }

    # Positive intent
    if it == "positive":

        body = (
            "Shukriya! Main abhi kaam shuru kar deti hoon. "
            "10-15 minute mein draft ready hoga — ek nazar dekh lena, phir live kar dungi."
            if lng in ("hi", "hi-en")
            else
            "On it! Draft will be ready in 10-15 minutes. Quick look from your side and I'll push it live."
        )

        state.turns.append({
            "from": "vera",
            "body": body
        })

        return {
            "action": "send",
            "body": body,
            "cta": "none",
            "rationale": "Merchant accepted; execute immediately; no re-pitch."
        }

    # Question intent
    if it == "question":

        body = (
            "Achha sawaal hai! Yeh detail check karni hogi — 5 minute mein exact answer ke saath wapas aata hoon."
            if lng in ("hi", "hi-en")
            else
            "Good question — let me check that specific detail and come back in 5 minutes with an accurate answer."
        )

        state.turns.append({
            "from": "vera",
            "body": body
        })

        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Merchant question; commit to specific answer; don't guess."
        }

    # 3-strike dormancy exit
    state.unanswered_count += 1

    if state.unanswered_count >= 3:
        state.status = "ended"

        return {
            "action": "end",
            "rationale": "3 unanswered nudges. Gracefully exiting — retry in 7 days."
        }

    # Neutral continuation
    body = (
        "Samjha! Aur kuch hai jisme main madad kar sakti hoon?"
        if lng in ("hi", "hi-en")
        else
        "Got it! Anything else, or shall we move forward on this?"
    )

    state.turns.append({
        "from": "vera",
        "body": body
    })

    return {
        "action": "send",
        "body": body,
        "cta": "open_ended",
        "rationale": "Neutral reply; light continuation to keep conversation open."
    }


# ── Demo ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    print("=== TEST 1: Merchant conversation ===\n")

    state = ConversationState(
        conversation_id="demo_001",
        merchant_id="m_001",
        customer_id=None,
        trigger_id="trg_001",
        from_role="merchant",
        turns=[
            {
                "from": "vera",
                "body": "Dr. Meera, JIDA's Oct issue — 38% caries reduction. Want me to pull it?"
            }
        ]
    )

    test_messages = [
        "Yes please",
        "Can you also check my profile?",
        "Aapki jaankari ke liye bahut shukriya…",
        "Aapki jaankari ke liye bahut shukriya…"
    ]

    for msg in test_messages:
        print(f"[MERCHANT]: {msg}")
        result = respond(state, msg)

        print(f"[VERA ACTION]: {result['action']}")

        if result.get("body"):
            print(f"[VERA BODY]: {result['body']}")

        print(f"[RATIONALE]: {result['rationale']}\n")

        if result["action"] == "end":
            break
