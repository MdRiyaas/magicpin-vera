from flask import Flask, request, jsonify
import uuid, time
from datetime import datetime, timezone

app = Flask(__name__)

store = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
conversations = {}
sent_keys = set()
_start = time.time()


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


@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    trigger_ids = data.get("available_triggers", [])
    actions = []

    for tid in trigger_ids:
        trigger = store["trigger"].get(tid, {}).get("payload")

        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")

        if not merchant_id:
            continue

        body = (
            f"Hi! Important update regarding {trigger.get('regulation', 'your business')} "
            f"before {trigger.get('deadline', 'soon')}. "
            f"Reply YES and I’ll help you handle it quickly."
        )

        conv_id = f"conv_{uuid.uuid4().hex[:8]}"

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "state": "open"
        }

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "trigger_id": tid,
            "send_as": "vera",
            "template_name": "vera_regulation_change_v2",
            "template_params": [],
            "body": body,
            "cta": "binary_yes_stop",
            "suppression_key": tid,
            "rationale": "Trigger-based compliance outreach"
        })

    return jsonify({"actions": actions}), 200


@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(force=True)

    msg = data.get("message", "").lower()

    if "yes" in msg:
        return jsonify({
            "action": "send",
            "body": "Perfect — I’ll help you set this up right away.",
            "cta": "none",
            "rationale": "Positive merchant response"
        }), 200

    if "stop" in msg or "no" in msg:
        return jsonify({
            "action": "end",
            "body": "Understood. Reach out anytime.",
            "rationale": "Exit request"
        }), 200

    return jsonify({
        "action": "send",
        "body": "Could you share a bit more so I can help properly?",
        "cta": "open_ended",
        "rationale": "Fallback reply"
    }), 200


@app.route("/v1/healthz", methods=["GET"])
def healthz():
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(time.time() - _start),
        "active_conversations": len(conversations),
        "suppressed_keys": len(sent_keys)
    }), 200


@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Mohamed Riyaas R",
        "team_members": ["Mohamed Riyaas R"],
        "model": "template-first",
        "approach": "Deterministic Vera bot",
        "contact_email": "mdriyaas68@gmail.com",
        "version": "2.1.0"
    }), 200


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
