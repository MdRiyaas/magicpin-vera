"""
server.py — Vera HTTP Server
magicpin AI Challenge — Mohamed Riyaas R

Exposes the 5 endpoints required by the judge harness:
  POST /v1/context   — receive context push
  POST /v1/tick      — periodic wake-up, bot decides what to send
  POST /v1/reply     — receive merchant/customer reply, respond
  GET  /v1/healthz   — liveness probe
  GET  /v1/metadata  — bot identity

Run: python server.py
     (default port 8080)
"""

import json
import uuid
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from bot import compose, load_dataset, MODEL

app = Flask(__name__)

# ── In-memory state store ─────────────────────────────────────────────────────
# contexts: {scope: {context_id: {version, payload}}}
store = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}

# Active conversations: {conversation_id: {merchant_id, customer_id, trigger_id, turns: []}}
conversations = {}

# Suppression log: set of suppression_keys already sent
sent_suppression_keys = set()

# Load seed dataset on startup
_dataset = None

def get_dataset():
    global _dataset
    if _dataset is None:
        _dataset = load_dataset("dataset")
    return _dataset

def resolve_context(merchant_id: str, trigger_id: str, customer_id: str | None):
    """Resolve all 4 contexts from store (or fall back to seed dataset)."""
    ds = get_dataset()

    # Category
    merchant_payload = store["merchant"].get(merchant_id, {}).get("payload") or \
                       ds["merchants"].get(merchant_id, {})
    cat_slug = merchant_payload.get("category_slug", "")
    category = store["category"].get(cat_slug, {}).get("payload") or \
               ds["categories"].get(cat_slug, {})

    # Merchant
    merchant = merchant_payload

    # Trigger
    trigger = store["trigger"].get(trigger_id, {}).get("payload") or \
              ds["triggers"].get(trigger_id, {})

    # Customer
    customer = None
    if customer_id:
        customer = store["customer"].get(customer_id, {}).get("payload") or \
                   ds["customers"].get(customer_id)

    return category, merchant, trigger, customer


# ── POST /v1/context ──────────────────────────────────────────────────────────
@app.route("/v1/context", methods=["POST"])
def receive_context():
    data = request.get_json(force=True)
    scope = data.get("scope")
    context_id = data.get("context_id")
    version = data.get("version", 1)
    payload = data.get("payload", {})

    if scope not in ("category", "merchant", "customer", "trigger"):
        return jsonify({"accepted": False, "reason": "invalid_scope", "details": f"unknown scope: {scope}"}), 400

    existing = store[scope].get(context_id, {})
    if existing.get("version", 0) > version:
        return jsonify({"accepted": False, "reason": "stale_version",
                        "current_version": existing["version"]}), 409

    store[scope][context_id] = {"version": version, "payload": payload}
    ack_id = f"ack_{uuid.uuid4().hex[:8]}"
    return jsonify({
        "accepted": True,
        "ack_id": ack_id,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }), 200


# ── POST /v1/tick ─────────────────────────────────────────────────────────────
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    available_trigger_ids = data.get("available_triggers", [])
    now_str = data.get("now", datetime.now(timezone.utc).isoformat())

    actions = []

    ds = get_dataset()

    for trigger_id in available_trigger_ids:
        # Get trigger
        trigger = store["trigger"].get(trigger_id, {}).get("payload") or \
                  ds["triggers"].get(trigger_id)
        if not trigger:
            continue

        # Suppression check
        sup_key = trigger.get("suppression_key", trigger_id)
        if sup_key in sent_suppression_keys:
            continue

        merchant_id = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
        customer_id = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")

        if not merchant_id:
            continue

        try:
            category, merchant, trigger_ctx, customer = resolve_context(merchant_id, trigger_id, customer_id)
            result = compose(category, merchant, trigger_ctx, customer)

            conv_id = f"conv_{uuid.uuid4().hex[:8]}"
            conversations[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trigger_id,
                "turns": [{"from": "vera", "body": result["body"]}],
                "state": "open",
            }

            sent_suppression_keys.add(result.get("suppression_key", sup_key))

            # Determine template name from trigger kind
            kind = trigger_ctx.get("kind", "generic")
            template_name = f"vera_{kind}_v1"
            merchant_name = merchant.get("identity", {}).get("owner_first_name", "there")

            actions.append({
                "conversation_id": conv_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": result.get("send_as", "vera"),
                "trigger_id": trigger_id,
                "template_name": template_name,
                "template_params": [merchant_name, kind],
                "body": result.get("body", ""),
                "cta": result.get("cta", "open_ended"),
                "suppression_key": result.get("suppression_key", sup_key),
                "rationale": result.get("rationale", ""),
            })
        except Exception as e:
            app.logger.error(f"Compose failed for {trigger_id}: {e}")
            continue

    return jsonify({"actions": actions}), 200


# ── POST /v1/reply ────────────────────────────────────────────────────────────
@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(force=True)
    conv_id = data.get("conversation_id")
    merchant_id = data.get("merchant_id")
    customer_id = data.get("customer_id")
    from_role = data.get("from_role", "merchant")
    message = data.get("message", "")
    turn_number = data.get("turn_number", 2)

    conv = conversations.get(conv_id)

    # Auto-reply detection: same message seen 2+ times = auto-reply
    if conv:
        prior_merchant_msgs = [t["body"] for t in conv["turns"] if t.get("from") == "merchant"]
        if prior_merchant_msgs.count(message) >= 1 and len(message) > 20:
            # Likely auto-reply — try once more then exit gracefully
            if prior_merchant_msgs.count(message) >= 2:
                conversations[conv_id]["state"] = "auto_reply_exit"
                return jsonify({
                    "action": "end",
                    "rationale": "Detected WhatsApp Business auto-reply (3rd identical message). Gracefully exiting to avoid wasting turns.",
                }), 200

    # Intent detection — explicit "yes/go/do it/join" signals
    positive_signals = ["yes", "ok", "sure", "go ahead", "let's do", "karo", "haan", "bilkul",
                        "please proceed", "do it", "start", "shuru", "i want to join", "judrna hai"]
    exit_signals = ["no", "nahi", "stop", "not interested", "baad mein", "later", "leave me",
                    "not now", "cancel", "unsubscribe"]

    msg_lower = message.lower()

    is_positive = any(sig in msg_lower for sig in positive_signals)
    is_exit = any(sig in msg_lower for sig in exit_signals)

    # Graceful exit
    if is_exit or (conv and conv.get("state") == "auto_reply_exit"):
        if conv:
            conversations[conv_id]["state"] = "ended"
        return jsonify({
            "action": "end",
            "rationale": "Merchant signaled not interested or stop. Gracefully exiting per conversation design.",
        }), 200

    # Build follow-up context
    if conv:
        trigger_id = conv.get("trigger_id", "")
        conv["turns"].append({"from": from_role, "body": message})
    else:
        # New conversation started by merchant
        trigger_id = ""
        conversations[conv_id] = {
            "merchant_id": merchant_id, "customer_id": customer_id,
            "trigger_id": "", "turns": [{"from": from_role, "body": message}], "state": "open",
        }

    ds = get_dataset()
    try:
        category, merchant, trigger_ctx, customer = resolve_context(
            merchant_id or (conv or {}).get("merchant_id", ""),
            trigger_id,
            customer_id or (conv or {}).get("customer_id"),
        )

        # Build a mini follow-up prompt
        history_text = ""
        if conv:
            for turn in conv["turns"][-4:]:
                history_text += f"[{turn.get('from','?').upper()}]: {turn.get('body','')}\n"

        action_hint = ""
        if is_positive:
            action_hint = "The merchant said YES/agreed. Now EXECUTE the promised action — don't re-pitch, just do it and confirm. Use effort externalization."
        else:
            action_hint = f"The merchant replied: '{message}'. Respond helpfully, move conversation forward. If they asked a question, answer it specifically. Don't re-introduce yourself."

        follow_up_prompt = f"""FOLLOW-UP CONVERSATION TURN {turn_number}

MERCHANT: {merchant.get("identity", {}).get("name", "")}
CATEGORY: {merchant.get("category_slug", "")}

CONVERSATION HISTORY:
{history_text}

LATEST MERCHANT MESSAGE: "{message}"

INSTRUCTION: {action_hint}

Return JSON: {{"action": "send", "body": "<reply>", "cta": "open_ended"|"binary_yes_stop"|"none", "rationale": "<1 sentence>"}}
OR: {{"action": "end", "rationale": "<reason>"}}
OR: {{"action": "wait", "wait_seconds": 1800, "rationale": "<reason>"}}

Return ONLY valid JSON."""

        import requests as req
        from bot import SYSTEM_PROMPT, OPENROUTER_API_KEY, OPENROUTER_URL, MODEL
        resp = req.post(OPENROUTER_URL, json={
            "model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": follow_up_prompt}],
            "temperature": 0, "max_tokens": 500,
        }, headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }, timeout=30)

        import re
        raw = resp.json()["choices"][0]["message"]["content"]
        clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        result = json.loads(clean)

        if result.get("action") == "send":
            if conv:
                conversations[conv_id]["turns"].append({"from": "vera", "body": result.get("body", "")})
        elif result.get("action") == "end":
            if conv:
                conversations[conv_id]["state"] = "ended"

        return jsonify(result), 200

    except Exception as e:
        app.logger.error(f"Reply compose failed: {e}")
        fallback_body = "Samjha! Main iska kaam kar deti hoon aur aapko update karti hoon." \
            if is_positive else "Thanks for your message. Is there anything specific I can help you with today?"
        return jsonify({
            "action": "send",
            "body": fallback_body,
            "cta": "open_ended",
            "rationale": f"Fallback response due to compose error: {e}",
        }), 200


# ── GET /v1/healthz ───────────────────────────────────────────────────────────
_start_time = time.time()

@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(time.time() - _start_time),
        "contexts_loaded": {
            "category": len(store["category"]) + len(get_dataset()["categories"]),
            "merchant": len(store["merchant"]) + len(get_dataset()["merchants"]),
            "customer": len(store["customer"]) + len(get_dataset()["customers"]),
            "trigger": len(store["trigger"]) + len(get_dataset()["triggers"]),
        },
        "active_conversations": len(conversations),
        "suppressed_keys": len(sent_suppression_keys),
    }), 200


# ── GET /v1/metadata ──────────────────────────────────────────────────────────
@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Mohamed Riyaas R",
        "team_members": ["Mohamed Riyaas R"],
        "model": "meta-llama/llama-3.3-70b-instruct:free via OpenRouter",
        "approach": "Trigger-kind routing (24 variants) → structured 4-context prompt → LLM compose → schema validation with retry. Auto-reply detection. Intent routing (positive/exit). Suppression dedup.",
        "contact_email": "mdriyaas68@gmail.com",
        "version": "1.0.0",
        "submitted_at": "2026-04-29T00:00:00Z",
    }), 200


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    print(f"Vera server starting on port {port}...")
    print(f"LLM: {MODEL}")
    app.run(host="0.0.0.0", port=port, debug=False)
