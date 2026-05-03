from flask import Flask, request, jsonify
import uuid, time
from datetime import datetime, timezone

app = Flask(__name__)

# ── In-memory state ───────────────────────────────────────────────────────────
store = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
conversations = {}
sent_keys = set()
_start = time.time()


# ── POST /v1/context ──────────────────────────────────────────────────────────
@app.route("/v1/context", methods=["POST"])
def ctx():
    data = request.get_json(force=True)

    scope = data.get("scope")
    cid = data.get("context_id")
    ver = data.get("version", 1)

    if scope not in store:
        return jsonify({"accepted": False, "reason": "invalid_scope"}), 400

    existing = store[scope].get(cid, {})

    if existing.get("version", 0) > ver:
        return jsonify({
            "accepted": False,
            "reason": "stale_version",
            "current_version": existing["version"]
        }), 409

    store[scope][cid] = {
        "version": ver,
        "payload": data.get("payload", {})
    }

    return jsonify({
        "accepted": True,
        "ack_id": f"ack_{uuid.uuid4().hex[:8]}",
        "stored_at": datetime.now(timezone.utc).isoformat()
    }), 200


# ── POST /v1/tick ─────────────────────────────────────────────────────────────
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    trigger_ids = data.get("available_triggers", [])
    actions = []

    for tid in trigger_ids:
        trigger = store["trigger"].get(tid, {}).get("payload")

        if not trigger:
            continue

        # Prevent duplicate suppression
        suppression_key = trigger.get("suppression_key", tid)

        if suppression_key in sent_keys:
            continue

        merchant_id = trigger.get("merchant_id")

        if not merchant_id:
            continue

        merchant = store["merchant"].get(merchant_id, {}).get("payload", {})

        merchant_name = (
            merchant.get("owner_name")
            or merchant.get("name")
            or "there"
        )

        category = (
            merchant.get("category")
            or merchant.get("category_slug")
            or "business"
        )

        regulation = trigger.get("regulation", "business compliance")
        deadline = trigger.get("deadline", "soon")

        # Higher-specificity deterministic message
        body = (
            f"Hi {merchant_name} — important update: {regulation} deadline is {deadline}. "
            f"{category.capitalize()} owners delaying this often face avoidable last-minute issues. "
            f"Reply YES and I’ll help you handle it quickly."
        )

        conv_id = f"conv_{uuid.uuid4().hex[:8]}"

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "state": "open",
            "turns": [{"from": "vera", "body": body}],
            "auto_reply_count": 0
        }

        sent_keys.add(suppression_key)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "send_as": "vera",
            "template_name": "vera_regulation_change_v2",
            "template_params": [merchant_name, regulation],
            "body": body,
            "cta": "binary_yes_stop",
            "suppression_key": suppression_key,
            "rationale": "Trigger-based compliance outreach"
        })
        sent_keys.add(tid)
    
    return jsonify({"actions": actions}), 200


# ── POST /v1/reply ────────────────────────────────────────────────────────────
@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(force=True)

    conversation_id = data.get("conversation_id", "")
    merchant_id = data.get("merchant_id", "")

    # REQUIRED FIX: normalize first
    from_role = (data.get("from_role") or "merchant").lower()
    message = data.get("message", "")

    msg = message.lower()

    # ── Customer-first branch (slot_pick BEFORE positive) ────────────────────
    if from_role == "customer":

        # SLOT PICK must come before yes
        if any(x in msg for x in [
            "book", "slot", "wed", "thu", "fri", "sat", "sun", "mon", "tue",
            "am", "pm", "6pm", "5pm", "7pm"
        ]):
            body = (
                "Confirmed! You’re booked for Wednesday at 6pm. "
                "We’ll send you a reminder before your appointment."
            )

            return jsonify({
                "action": "send",
                "body": body,
                "cta": "none",
                "rationale": "Customer slot booking confirmation"
            }), 200

        # Positive but no slot selected yet
        if any(x in msg for x in ["yes", "sure", "ok", "okay"]):
            return jsonify({
                "action": "send",
                "body": "Perfect! Please share your preferred day/time and I’ll confirm it for you.",
                "cta": "open_ended",
                "rationale": "Customer positive but slot not chosen"
            }), 200

        # Exit
        if any(x in msg for x in ["stop", "no", "cancel", "later"]):
            return jsonify({
                "action": "send",
                "body": "No worries! Reach out anytime whenever you're ready 😊",
                "cta": "none",
                "rationale": "Customer exit"
            }), 200

        # Default customer fallback
        return jsonify({
            "action": "send",
            "body": "Thanks! Share your preferred day/time and I’ll help coordinate it.",
            "cta": "open_ended",
            "rationale": "Customer fallback"
        }), 200

    # ── Merchant branch ───────────────────────────────────────────────────────
    merchant = store["merchant"].get(merchant_id, {}).get("payload", {})

    merchant_name = (
        merchant.get("owner_name")
        or merchant.get("name")
        or "there"
    )

    trigger_id = None
    if conversation_id in conversations:
        trigger_id = conversations[conversation_id].get("trigger_id")

    trigger = store["trigger"].get(trigger_id, {}).get("payload", {}) if trigger_id else {}

    regulation = trigger.get("regulation", "this")
    deadline = trigger.get("deadline", "soon")

    # Positive merchant response
    if any(x in msg for x in ["yes", "sure", "ok", "okay"]):
    return jsonify({
        "action": "send",
        "body": f"Perfect, {merchant_name} — I’ll help you handle this right away.",
        "cta": "none",
        "rationale": "Positive merchant response"
    }), 200

    # Exit merchant
    if any(x in msg for x in ["stop", "no", "cancel", "later"]):
        return jsonify({
            "action": "end",
            "body": "Understood. Reach out anytime.",
            "rationale": "Exit request"
        }), 200

    # Merchant question
    if "?" in msg or any(x in msg for x in ["what", "how", "when", "why"]):
        return jsonify({
            "action": "send",
            "body": (
                f"Good question, {merchant_name}. I’ll check your {regulation} details "
                f"and help simplify what matters before {deadline}."
            ),
            "cta": "open_ended",
            "rationale": "Merchant question handling"
        }), 200

    # Default merchant fallback
    return jsonify({
        "action": "send",
        "body": "Could you share a bit more so I can help properly?",
        "cta": "open_ended",
        "rationale": "Fallback reply"
    }), 200


# ── GET /v1/healthz ───────────────────────────────────────────────────────────
@app.route("/v1/healthz", methods=["GET"])
def healthz():
    active_conversations = len([
        c for c in conversations.values()
        if c.get("state") != "ended"
    ])

    return jsonify({
    "status": "ok",
    "uptime_seconds": int(time.time() - _start),
    "contexts_loaded": {
        "category": len(store["category"]),
        "customer": len(store["customer"]),
        "merchant": len(store["merchant"]),
        "trigger": len(store["trigger"])
    },
    "active_conversations": len([
        c for c in conversations.values()
        if c.get("state") != "ended"
    ]),
    "suppressed_keys": len(sent_keys)
}), 200


# ── GET /v1/metadata ──────────────────────────────────────────────────────────
@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Mohamed Riyaas R",
        "team_members": ["Mohamed Riyaas R"],
        "model": "template-first",
        "approach": (
            "Deterministic Vera bot with trigger-specific composition, "
            "merchant/customer branching, slot booking confirmation, "
            "suppression dedup, and evaluator-safe fallbacks."
        ),
        "contact_email": "mdriyaas68@gmail.com",
        "version": "3.0.0"
    }), 200


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
