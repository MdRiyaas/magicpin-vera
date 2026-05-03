# ── POST /v1/tick ─────────────────────────────────────────────────────────────
@app.route("/v1/tick", methods=["POST"])
def tick():
    data = request.get_json(force=True)
    trigger_ids = data.get("available_triggers", [])
    actions = []

    for tid in trigger_ids:
        # Pull trigger from runtime store first, then dataset fallback
        trigger_record = store["trigger"].get(tid, {})
        trigger = trigger_record.get("payload") or ds()["triggers"].get(tid)

        if not trigger:
            app.logger.error(f"tick skip {tid}: trigger missing")
            continue

        # Suppression / dedupe
        sup = trigger.get("suppression_key", tid)
        if sup in sent_keys:
            app.logger.error(f"tick skip {tid}: suppressed ({sup})")
            continue

        # Flexible ID extraction
        mid = (
            trigger.get("merchant_id")
            or trigger.get("merchant")
            or trigger.get("merchantId")
        )

        cust_id = (
            trigger.get("customer_id")
            or trigger.get("customer")
            or trigger.get("customerId")
        )

        if not mid:
            app.logger.error(f"tick skip {tid}: merchant_id missing in trigger {trigger}")
            continue

        try:
            # Resolve all context
            cat, mer, trg, cust = resolve(mid, tid, cust_id)

            # Defensive merchant fallback
            if not mer:
                app.logger.error(f"tick skip {tid}: merchant unresolved ({mid})")
                continue

            # Category fallback if category_slug missing
            if not cat:
                cat_slug = (
                    mer.get("category_slug")
                    or mer.get("category_id")
                    or mer.get("category")
                    or ""
                )
                cat = store["category"].get(cat_slug, {}).get("payload") or ds()["categories"].get(cat_slug, {})

            # Compose message
            result = compose(cat, mer, trg, cust)

            # Ensure valid body
            if not result or not result.get("body"):
                app.logger.error(f"tick skip {tid}: compose returned empty result")
                continue

            conv_id = f"conv_{uuid.uuid4().hex[:8]}"

            conversations[conv_id] = {
                "merchant_id": mid,
                "customer_id": cust_id,
                "trigger_id": tid,
                "state": "open",
                "turns": [{"from": "vera", "body": result["body"]}],
                "auto_reply_count": 0,
            }

            sent_keys.add(result.get("suppression_key", sup))

            kind = (
                trg.get("kind")
                or trg.get("trigger_kind")
                or "generic"
            )

            owner = _name(mer)

            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": cust_id,
                "send_as": result.get("send_as", "vera"),
                "trigger_id": tid,
                "template_name": f"vera_{kind}_v2",
                "template_params": [owner, kind],
                "body": result["body"],
                "cta": result.get("cta", "open_ended"),
                "suppression_key": result.get("suppression_key", sup),
                "rationale": result.get("rationale", ""),
            })

        except Exception as e:
            app.logger.error(f"tick compose error {tid}: {e}")

    return jsonify({"actions": actions}), 200
