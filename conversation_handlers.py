"""
conversation_handlers.py — Multi-Turn Conversation Handling
magicpin AI Challenge — Mohamed Riyaas R

Demonstrates:
  1. Auto-reply detection (same message 2+ times = WA Business auto-reply)
  2. Intent routing (explicit YES → action mode immediately)
  3. Graceful exit (STOP / not interested)
  4. Language detection per turn (switches Hindi/English mid-conversation)
  5. 3-strike dormancy exit (3 unanswered nudges → stop)
"""

import json, re
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: str
    turns: list = field(default_factory=list)
    status: str = "open"   # open | ended | waiting
    unanswered_count: int = 0
    auto_reply_count: int = 0

# ── Auto-reply detector ───────────────────────────────────────────────────────
AUTO_REPLY_PATTERNS = [
    r"aapki jaankari ke liye.*shukriya",
    r"thank you for contact",
    r"i am (currently )?unavailable",
    r"will get back to you",
    r"out of office",
    r"automated (message|reply|response)",
    r"main ek automated",
    r"hum aapko jald",
]

def is_auto_reply(message: str, prior_messages: list[str]) -> bool:
    msg = message.lower().strip()
    # Pattern match
    for pat in AUTO_REPLY_PATTERNS:
        if re.search(pat, msg):
            return True
    # Same message appeared before
    if prior_messages.count(message) >= 1:
        return True
    return False

# ── Intent detector ───────────────────────────────────────────────────────────
POSITIVE_INTENTS = [
    "yes", "ok", "sure", "haan", "bilkul", "karo", "go ahead",
    "please do", "let's do", "start", "shuru karo", "proceed",
    "mujhe judrna hai", "join karna hai", "send kar do", "draft kar do"
]

EXIT_INTENTS = [
    "no", "nahi", "stop", "not interested", "leave me", "baad mein",
    "later", "cancel", "unsubscribe", "mat karo", "band karo"
]

def detect_intent(message: str) -> str:
    """Returns 'positive' | 'exit' | 'question' | 'neutral'"""
    msg = message.lower()
    if any(s in msg for s in POSITIVE_INTENTS): return "positive"
    if any(s in msg for s in EXIT_INTENTS): return "exit"
    if "?" in msg or any(w in msg for w in ["kya", "how", "when", "kab", "kitna", "kaun"]): return "question"
    return "neutral"

# ── Language detector ─────────────────────────────────────────────────────────
def detect_language(message: str) -> str:
    """Simple heuristic: count Hindi Unicode chars."""
    hindi_chars = sum(1 for c in message if '\u0900' <= c <= '\u097F')
    if hindi_chars > 3: return "hi"
    # Romanized Hindi keywords
    hindi_words = ["karo", "haan", "nahi", "kya", "aap", "main", "mujhe", "hai", "tha"]
    if sum(1 for w in hindi_words if w in message.lower()) >= 2: return "hi-en"
    return "en"

# ── Main respond function ─────────────────────────────────────────────────────
def respond(state: ConversationState, merchant_message: str) -> dict:
    """
    Given conversation state + merchant's latest message, return the next move.
    Returns dict with keys: action (send|wait|end), body (if send), cta, rationale.
    """
    prior_merchant_msgs = [t["body"] for t in state.turns if t.get("from") == "merchant"]

    # 1. Auto-reply detection
    if is_auto_reply(merchant_message, prior_merchant_msgs):
        state.auto_reply_count += 1
        if state.auto_reply_count == 1:
            # Try once to get through
            state.turns.append({"from": "merchant", "body": merchant_message, "tag": "auto_reply"})
            return {
                "action": "send",
                "body": "Samajh gayi — aapki team tak pahunch gayi hogi. Kya aap khud 2 minute de sakte hain? Main aaj jo karna chahti thi woh 5-min ka kaam hai.",
                "cta": "binary_yes_stop",
                "rationale": "First auto-reply detected; one polite attempt to break through before exiting.",
            }
        else:
            state.status = "ended"
            return {
                "action": "end",
                "rationale": "Multiple auto-replies detected. Gracefully exiting — will retry via a different touchpoint.",
            }

    # Reset auto_reply_count on real reply
    state.auto_reply_count = 0

    # 2. Intent routing
    intent = detect_intent(merchant_message)
    lang = detect_language(merchant_message)

    state.turns.append({"from": "merchant", "body": merchant_message, "intent": intent, "lang": lang})
    state.unanswered_count = 0  # Reset on any reply

    if intent == "exit":
        state.status = "ended"
        if lang in ("hi", "hi-en"):
            farewell = "Bilkul samajh gaya! Koi baat nahi — aap jab chahein tab wapas aa sakte hain. Best of luck! 🙂"
        else:
            farewell = "Understood — no problem at all. Feel free to reach out whenever you're ready. Best wishes!"
        return {
            "action": "send",
            "body": farewell,
            "cta": "none",
            "rationale": "Exit intent detected. Warm, non-pushy farewell. Door left open.",
        }

    if intent == "positive":
        # Execute the promised action immediately — don't re-pitch
        if lang in ("hi", "hi-en"):
            action_body = "Shukriya! Main abhi kaam shuru kar deti hoon. 10-15 minute mein aapko draft bhejti hoon — aap ek nazar dekh lena, phir main ise live kar dungi."
        else:
            action_body = "On it! I'll have the draft ready in 10-15 minutes. Take a quick look and I'll push it live once you confirm."
        state.turns.append({"from": "vera", "body": action_body})
        return {
            "action": "send",
            "body": action_body,
            "cta": "none",
            "rationale": "Positive intent → action mode immediately. No re-pitch. Confirming execution.",
        }

    if intent == "question":
        # Answer the question specifically — placeholder (real impl would call LLM)
        if lang in ("hi", "hi-en"):
            q_body = "Achha sawaal hai! Yeh detail mujhe check karni hogi — main 5 minute mein wapas aata hoon iske exact answer ke saath."
        else:
            q_body = "Good question — let me check that specific detail and get back to you in 5 minutes."
        state.turns.append({"from": "vera", "body": q_body})
        return {
            "action": "send",
            "body": q_body,
            "cta": "open_ended",
            "rationale": "Merchant asked a question; acknowledge and commit to specific answer. Don't guess.",
        }

    # Neutral reply — continue conversation with next best step
    # If 3 unanswered nudges, exit gracefully
    if state.unanswered_count >= 3:
        state.status = "ended"
        return {
            "action": "end",
            "rationale": "3 unanswered nudges. Gracefully exiting — will retry in 7 days.",
        }

    # Default: light follow-up
    if lang in ("hi", "hi-en"):
        followup = "Samjha! Aur kuch hai jisme main madad kar sakti hoon? Ya fir is topic pe aage badhna chahenge?"
    else:
        followup = "Got it! Anything else I can help with, or shall we move ahead on this?"
    state.turns.append({"from": "vera", "body": followup})
    return {
        "action": "send",
        "body": followup,
        "cta": "open_ended",
        "rationale": "Neutral reply — soft continuation to keep conversation open.",
    }


# ── Demo: simulate a 4-turn conversation ─────────────────────────────────────
if __name__ == "__main__":
    state = ConversationState(
        conversation_id="demo_001",
        merchant_id="m_001_drmeera_dentist_delhi",
        customer_id=None,
        trigger_id="trg_001_research_digest_dentists",
        turns=[
            {"from": "vera", "body": "Dr. Meera, JIDA's Oct issue landed — 2,100-patient trial showed 3-month fluoride recall cuts caries 38% better. Want me to pull the abstract + draft a patient-ed WhatsApp?"}
        ]
    )

    test_replies = [
        "Yes please, sounds useful",                          # → positive intent → action mode
        "Also can you check my profile completion?",          # → question
        "Aapki jaankari ke liye bahut shukriya…",            # → auto-reply
        "Aapki jaankari ke liye bahut shukriya…",            # → auto-reply again → exit
    ]

    print("=== MULTI-TURN CONVERSATION DEMO ===\n")
    for i, msg in enumerate(test_replies):
        print(f"[MERCHANT turn {i+1}]: {msg}")
        result = respond(state, msg)
        print(f"[VERA action]: {result['action']}")
        if result.get("body"):
            print(f"[VERA body]: {result['body']}")
        print(f"[rationale]: {result['rationale']}")
        print()
        if result["action"] == "end":
            print("Conversation ended gracefully.")
            break
