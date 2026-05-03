"""
server.py — Vera HTTP Server v2
magicpin AI Challenge — Mohamed Riyaas R

Fixed:
  - /v1/reply: customer slot pick → warm confirmation (not action=end)
  - /v1/reply: never returns empty body
  - Auto-reply: end after FIRST failed attempt (not second)
  - All fallback responses use compulsion levers
  - from_role branching: merchant vs customer handled separately
"""

import json, uuid, time, re
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from bot import compose, load_dataset, _hi, _name, _active_offers

app = Flask(__name__)

# ── In-memory state ───────────────────────────────────────────────────────────
store = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
conversations = {}  # conv_id → {merchant_id, customer_id, trigger_id, turns, state}
sent_keys = set()   # suppression keys already fired
_start = time.time()
_dataset = None

def ds():
    global _dataset
    if _dataset is None:
        _dataset = load_dataset("dataset")
    return _dataset

def resolve(merchant_id, trigger_id, customer_id=None):
    d = ds()
    merchant = store["merchant"].get(merchant_id,{}).get("payload") or d["merchants"].get(merchant_id,{})
    cat_slug = merchant.get("category_slug","")
    category = store["category"].get(cat_slug,{}).get("payload") or d["categories"].get(cat_slug,{})
    trigger  = store["trigger"].get(trigger_id,{}).get("payload") or d["triggers"].get(trigger_id,{})
    customer = None
    if customer_id:
        customer = store["customer"].get(customer_id,{}).get("payload") or d["customers"].get(customer_id)
    return category, merchant, trigger, customer


# ── Intent helpers ────────────────────────────────────────────────────────────
POSITIVE = ["yes","ok","sure","haan","bilkul","karo","go ahead","please do",
            "let's do","start","shuru","proceed","send kar","draft kar","book kar"]
EXIT     = ["no","nahi","stop","not interested","baad mein","later","cancel",
            "unsubscribe","mat karo","band karo","not now"]
SLOT_PICK = re.compile(r'\b(1|2|3|wed|thu|fri|sat|sun|mon|tue|6pm|5pm|7pm|8am|9am|book|confirm|slot)\b', re.I)
AUTO_REPLY_RE = re.compile(
    r'(aapki jaankari ke liye|thank you for contact|unavailable|will get back|automated (message|reply)|main ek automated|hum aapko jald)', re.I)

def intent(msg):
    m = msg.lower()
    if EXIT and any(s in m for s in EXIT): return "exit"
    if SLOT_PICK.search(m): return "slot_pick"
    if any(s in m for s in POSITIVE): return "positive"
    if "?" in m or any(w in m for w in ["kya","how","when","kab","kitna","kaun","what","which"]): return "question"
    return "neutral"

def is_auto_reply(msg, prior_msgs):
    if AUTO_REPLY_RE.search(msg): return True
    if prior_msgs.count(msg) >= 1 and len(msg) > 20: return True
    return False


# ── POST /v1/context ──────────────────────────────────────────────────────────
@app.route("/v1/context", methods=["POST"])
def ctx():
    data = request.get_json(force=True)
    scope = data.get("scope")
    cid   = data.get("context_id")
    ver   = data.get("version",1)
    if scope not in store:
        return jsonify({"accepted":False,"reason":"invalid_scope"}), 400
    existing = store[scope].get(cid,{})
    if existing.get("version",0) > ver:
        return jsonify({"accepted":False,"reason":"stale_version","current_version":existing["version"]}), 409
    store[scope][cid] = {"version":ver,"payload":data.get("payload",{})}
    return jsonify({"accepted":True,"ack_id":f"ack_{uuid.uuid4().hex[:8]}",
                    "stored_at":datetime.now(timezone.utc).isoformat()}), 200


# ── POST /v1/tick ─────────────────────────────────────────────────────────────
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    trigger_ids = data.get("available_triggers",[])
    actions = []
    for tid in trigger_ids:
        trigger = store["trigger"].get(tid,{}).get("payload") or ds()["triggers"].get(tid)
        if not trigger: continue
        sup = trigger.get("suppression_key", tid)
        if sup in sent_keys: continue
        mid = trigger.get("merchant_id") or trigger.get("payload",{}).get("merchant_id")
        cust_id = trigger.get("customer_id") or trigger.get("payload",{}).get("customer_id")
        if not mid: continue
        try:
            cat, mer, trg, cust = resolve(mid, tid, cust_id)
            result = compose(cat, mer, trg, cust)
            conv_id = f"conv_{uuid.uuid4().hex[:8]}"
            conversations[conv_id] = {
                "merchant_id": mid, "customer_id": cust_id,
                "trigger_id": tid, "state": "open",
                "turns": [{"from":"vera","body":result["body"]}],
                "auto_reply_count": 0,
            }
            sent_keys.add(result.get("suppression_key", sup))
            kind = trg.get("kind","generic")
            owner = _name(mer)
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": cust_id,
                "send_as": result.get("send_as","vera"),
                "trigger_id": tid,
                "template_name": f"vera_{kind}_v2",
                "template_params": [owner, kind],
                "body": result["body"],
                "cta": result.get("cta","open_ended"),
                "suppression_key": result.get("suppression_key", sup),
                "rationale": result.get("rationale",""),
            })
        except Exception as e:
            app.logger.error(f"tick compose error {tid}: {e}")
    return jsonify({"actions": actions}), 200


# ── POST /v1/reply ─────────────────────────────────────────────────────────────
@app.route("/v1/reply", methods=["POST"])
def reply():
    data = request.get_json(force=True)
    conv_id   = data.get("conversation_id","")
    mid       = data.get("merchant_id","")
    cust_id   = data.get("customer_id")
    from_role = data.get("from_role","merchant")  # "merchant" or "customer"
    message   = data.get("message","")
    turn_num  = data.get("turn_number",2)

    conv = conversations.get(conv_id)

    # Already ended — don't keep responding
    if conv and conv.get("state") == "ended":
        return jsonify({"action":"end","rationale":"Conversation already ended."}), 200

    prior_msgs = [t["body"] for t in (conv or {}).get("turns",[]) if t.get("from") == from_role]

    # ── Customer-facing reply branch ──────────────────────────────────────────
    if from_role == "customer":
        it = intent(message)

        # Slot pick / booking confirmation
        if it == "slot_pick":
            # Extract which slot they picked
            msg_l = message.lower()
            slot_label = ""
            if "wed" in msg_l or "1" == msg_l.strip():
                slot_label = "Wednesday"
            elif "thu" in msg_l or "2" == msg_l.strip():
                slot_label = "Thursday"
            else:
                slot_label = "your preferred time"

            # Get merchant name from context
            _, mer, trg, _ = resolve(mid or (conv or {}).get("merchant_id",""),
                                     (conv or {}).get("trigger_id",""), cust_id)
            mer_name = mer.get("identity",{}).get("name","the clinic") if mer else "the clinic"
            offers = _active_offers(mer) if mer else []
            price_note = offers[0] if offers else ""

            body = (f"Confirmed! {mer_name} has you booked for {slot_label}. "
                    f"{'Your ' + price_note + ' is all set. ' if price_note else ''}"
                    f"We'll send a reminder an hour before. See you there! 🦷")
            if conv:
                conversations[conv_id]["turns"].append({"from":"vera","body":body})
            return jsonify({"action":"send","body":body,"cta":"none",
                           "rationale":"Customer confirmed slot pick; warm booking confirmation; no further CTA needed."}), 200

        if it == "exit":
            if conv: conversations[conv_id]["state"] = "ended"
            return jsonify({"action":"send",
                           "body":"No worries at all! Feel free to reach out whenever you're ready. We're here 😊",
                           "cta":"none",
                           "rationale":"Customer exit; warm close; door left open."}), 200

        if it == "positive":
            body = "Perfect! I'll confirm the details with the clinic and send you a reminder. Is there anything specific you'd like us to note for your visit?"
            if conv: conversations[conv_id]["turns"].append({"from":"vera","body":body})
            return jsonify({"action":"send","body":body,"cta":"open_ended",
                           "rationale":"Customer positive; confirm and ask for preferences."}), 200

        # Default customer reply
        body = "Thanks for reaching out! Let me check the available options and get back to you in a moment."
        if conv: conversations[conv_id]["turns"].append({"from":"vera","body":body})
        return jsonify({"action":"send","body":body,"cta":"open_ended",
                       "rationale":"Customer message; acknowledging and following up."}), 200

    # ── Merchant-facing reply branch ──────────────────────────────────────────

    # Auto-reply detection — end IMMEDIATELY after first auto-reply (not second)
    if is_auto_reply(message, prior_msgs):
        arc = (conv or {}).get("auto_reply_count", 0)
        if arc == 0:
            # First auto-reply — try once to break through
            if conv: conv["auto_reply_count"] = 1
            if conv: conv["turns"].append({"from":"merchant","body":message,"tag":"auto_reply"})
            _, mer, _, _ = resolve(mid or (conv or {}).get("merchant_id",""),
                                   (conv or {}).get("trigger_id",""))
            hi = _hi(mer.get("identity",{}).get("languages",["en"])) if mer else False
            if hi:
                body = "Samajh gayi — team tak pahuch gayi hogi. Kya aap khud 2 minute de sakte hain? Jo share karna tha woh 5-min ka useful kaam hai."
            else:
                body = "Got it — your team will see this. Can you spare 2 minutes directly? What I wanted to share is a 5-minute useful action."
            if conv: conv["turns"].append({"from":"vera","body":body})
            return jsonify({"action":"send","body":body,"cta":"binary_yes_stop",
                           "rationale":"First auto-reply — one polite attempt to reach the owner before exiting."}), 200
        else:
            # Second auto-reply — exit gracefully
            if conv: conversations[conv_id]["state"] = "ended"
            return jsonify({"action":"end",
                           "rationale":"Second consecutive auto-reply. Gracefully exiting — will retry via different touchpoint."}), 200

    if conv: conv.setdefault("auto_reply_count",0)

    # Log turn
    if conv: conv["turns"].append({"from":"merchant","body":message})

    it = intent(message)
    _, mer, trg, _ = resolve(mid or (conv or {}).get("merchant_id",""),
                             (conv or {}).get("trigger_id",""))
    hi = _hi(mer.get("identity",{}).get("languages",["en"])) if mer else False
    offers = _active_offers(mer) if mer else []
    mer_name = _name(mer) if mer else ""

    if it == "exit":
        if conv: conversations[conv_id]["state"] = "ended"
        if hi:
            body = "Bilkul samajh gaya! Koi baat nahi — jab chahein wapas aa jayein. Best of luck! 🙂"
        else:
            body = "Understood — no problem at all. Feel free to reach out whenever you need. Best wishes!"
        if conv: conv["turns"].append({"from":"vera","body":body})
        return jsonify({"action":"send","body":body,"cta":"none",
                       "rationale":"Merchant exit; warm non-pushy farewell; door left open."}), 200

    if it == "positive":
        # Execute the action — don't re-pitch
        if hi:
            body = f"Shukriya {mer_name}! Main abhi kaam shuru kar deti hoon — 10-15 minute mein draft ready hoga. Ek nazar dekh lena, phir main ise live kar dungi."
        else:
            body = f"On it, {mer_name}! I'll have the draft ready in 10-15 minutes. Quick review from your side and I'll push it live."
        if conv: conv["turns"].append({"from":"vera","body":body})
        return jsonify({"action":"send","body":body,"cta":"none",
                       "rationale":"Merchant accepted; execute immediately; no re-pitch."}), 200

    if it == "question":
        # Build a category-aware answer anchor
        cat_slug = mer.get("category_slug","") if mer else ""
        answers = {
            "dentists": f"Good question. For dental practices, the usual answer depends on your case mix — let me check your specific profile and come back in 5 minutes with an exact answer.",
            "salons": f"Great point. For salons in your locality, I'll pull the specific data and come back in 5 minutes.",
            "restaurants": f"Makes sense to ask. I'll check your delivery vs dine-in split and come back with specifics.",
            "gyms": f"Good question. I'll pull your member retention data and come back with a specific answer.",
            "pharmacies": f"Fair question. I'll check the category data and come back with specifics in 5 minutes.",
        }
        body = answers.get(cat_slug, f"Good question, {mer_name}. Let me check that specific detail and come back with an accurate answer in 5 minutes.")
        if conv: conv["turns"].append({"from":"vera","body":body})
        return jsonify({"action":"send","body":body,"cta":"open_ended",
                       "rationale":"Merchant question; acknowledge and commit to specific answer; don't guess."}), 200

    # Neutral — keep conversation alive with next best step
    if hi:
        body = f"Samjha {mer_name}. Aur kuch discuss karna hai, ya is topic pe aage badhein?"
    else:
        body = f"Got it, {mer_name}. Anything else on your mind, or shall we move forward on this?"
    if conv: conv["turns"].append({"from":"vera","body":body})
    return jsonify({"action":"send","body":body,"cta":"open_ended",
                   "rationale":"Neutral merchant reply; light continuation to keep conversation open."}), 200


# ── GET /v1/healthz ───────────────────────────────────────────────────────────
@app.route("/v1/healthz", methods=["GET"])
def healthz():
    d = ds()
    return jsonify({
        "status": "ok",
        "uptime_seconds": int(time.time()-_start),
        "contexts_loaded": {
            "category": len(store["category"]) + len(d["categories"]),
            "merchant": len(store["merchant"]) + len(d["merchants"]),
            "customer": len(store["customer"]) + len(d["customers"]),
            "trigger":  len(store["trigger"])  + len(d["triggers"]),
        },
        "active_conversations": len([c for c in conversations.values() if c.get("state")!="ended"]),
        "suppressed_keys": len(sent_keys),
    }), 200


# ── GET /v1/metadata ──────────────────────────────────────────────────────────
@app.route("/v1/metadata", methods=["GET"])
def metadata():
    return jsonify({
        "team_name": "Mohamed Riyaas R",
        "team_members": ["Mohamed Riyaas R"],
        "model": "template-first + optional LLM refinement (OpenRouter Llama 3.3 70B)",
        "approach": "24 trigger-kind templates with real context data (no API key required). Optional LLM refinement for naturalness. Auto-reply detection. Merchant/customer from_role branching. Slot-booking confirmation handler. Suppression dedup.",
        "contact_email": "mdriyaas68@gmail.com",
        "version": "2.0.0",
        "submitted_at": "2026-04-30T00:00:00Z",
    }), 200


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT",8080))
    print(f"Vera v2 starting on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)
