from flask import Flask, request, jsonify
import uuid, time
from datetime import datetime, timezone

from bot import compose, load_dataset
from conversation_handlers import ConversationState, respond as conv_respond

app = Flask(__name__)

# Dataset loaded once at startup for compose() calls
ds = load_dataset("dataset")

# In-memory state
store = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
conversations = {}
sent_keys = set()
_start = time.time()


# ── POST /v1/context ──────────────────────────────────────────────────────────
@app.route("/v1/context", methods=["POST"])
def ctx():
    data = request.get_json(force=True)

    scope = (data.get("scope") or "").strip().lower()
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
    print("STORE DEBUG:", scope, list(store[scope].keys()))
    return jsonify({
        "accepted": True,
        "ack_id": f"ack_{uuid.uuid4().hex[:8]}",
        "stored_at": datetime.now(timezone.utc).isoformat()
    }), 200


# POST /v1/tick
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    trigger_ids = data.get("available_triggers", [])
    actions = []

    for tid in trigger_ids:
        trigger_payload = store["trigger"].get(tid, {}).get("payload")

        if not trigger_payload:
            continue

        # FIX I-05: suppression_key added only ONCE (was added twice before)
        suppression_key = trigger_payload.get("suppression_key", tid)

        if suppression_key in sent_keys:
            continue

        merchant_id = trigger_payload.get("merchant_id")

        if not merchant_id:
            continue

        merchant_payload = store["merchant"].get(merchant_id, {}).get("payload", {})

        # FIX I-07: resolve full category object from slug
        cat_slug = merchant_payload.get("category_slug", "")
        category_obj = (
            store["category"].get(cat_slug, {}).get("payload")
            or ds["categories"].get(cat_slug, {})
        )

        # FIX I-01 & I-04: call compose() with real context objects
        try:
            result = compose(category_obj, merchant_payload, trigger_payload)
        except Exception as e:
            owner = (
                merchant_payload.get("identity", {}).get("owner_first_name")
                or merchant_payload.get("identity", {}).get("name")
                or merchant_payload.get("name", "there")
            )
            result = {
                "body": (
                    f"Hi {owner} — Vera here. There's an update worth your attention. "
                    f"Reply YES and I'll share the details."
                ),
                "cta": "binary_yes_stop",
                "send_as": "vera",
                "suppression_key": suppression_key,
                "rationale": f"Compose fallback (error: {e})"
            }

        conv_id = f"conv_{uuid.uuid4().hex[:8]}"

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "state": "open",
            "turns": [{"from": "vera", "body": result["body"]}],
            "auto_reply_count": 0,
            "unanswered_count": 0
        }

        sent_keys.add(suppression_key)  # FIX I-05: only once

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "send_as": result.get("send_as", "vera"),
            "body": result["body"],
            "cta": result.get("cta", "binary_yes_stop"),
            "suppression_key": suppression_key,
            "rationale": result.get("rationale", "")
        })

    return jsonify({"actions": actions}), 200


# POST /v1/reply
@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(force=True)

    conversation_id = data.get("conversation_id", "")
    merchant_id = data.get("merchant_id", "")
    from_role = (data.get("from_role") or "merchant").lower()
    message = data.get("message", "")

    conv = conversations.get(conversation_id, {})

    # Resolve merchant name for slot confirmation enrichment
    merchant_payload = store["merchant"].get(merchant_id, {}).get("payload", {})
    merchant_name = (
        merchant_payload.get("identity", {}).get("name")
        or merchant_payload.get("name", "")
    )

    # FIX I-02 & I-03: hydrate ConversationState and call conv_respond()
    state = ConversationState(
        conversation_id=conversation_id or str(uuid.uuid4()),
        merchant_id=merchant_id,
        customer_id=data.get("customer_id"),
        trigger_id=conv.get("trigger_id", ""),
        from_role=from_role,
        turns=conv.get("turns", []),
        status=conv.get("status", "open"),
        auto_reply_count=conv.get("auto_reply_count", 0),
        unanswered_count=conv.get("unanswered_count", 0),
        merchant_name=merchant_name
    )

    result = conv_respond(state, message)

    # Persist updated state back to conversations store
    conversations[conversation_id] = {
        **conv,
        "turns": state.turns,
        "status": state.status,
        "auto_reply_count": state.auto_reply_count,
        "unanswered_count": state.unanswered_count
    }

    return jsonify(result), 200


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
