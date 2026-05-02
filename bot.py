"""
bot.py — Vera Message Composer
magicpin AI Challenge — Mohamed Riyaas R
"""

import os, json, re
from typing import Optional
import requests

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("VERA_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

SYSTEM_PROMPT = """You are Vera — magicpin's merchant AI assistant composing WhatsApp messages to merchants and their customers in India.

OUTPUT FORMAT — return ONLY valid JSON, nothing else:
{
  "body": "<WhatsApp message>",
  "cta": "open_ended" | "binary_yes_stop" | "none",
  "send_as": "vera" | "merchant_on_behalf",
  "suppression_key": "<from trigger>",
  "rationale": "<1-2 sentences>"
}

RULES:
1. SPECIFICITY — anchor every message on a real number, date, source, or stat from the context. "₹299 cleaning" beats "great deal". "38% fewer caries" beats "better results".
2. VOICE — dentists/pharmacies: peer/clinical/calm. salons: warm/practical. restaurants: casual/energetic. gyms: motivational. NEVER promotional hype for clinical categories.
3. CTA — binary YES/STOP for action triggers. open_ended for info/curiosity. none for pure-info. ALWAYS in the last sentence.
4. HINDI-ENGLISH — if merchant languages include "hi", naturally mix Hindi. Don't force it.
5. NO FABRICATION — only cite sources in digest. Never invent competitor names. Never invent offers.
6. NO PREAMBLE — never "I hope you're doing well". Get to the point.
7. SEND_AS — "vera" for merchant-facing. "merchant_on_behalf" ONLY when customer_context is present AND trigger scope is customer.
8. COMPULSION — use 1-2: specificity, loss aversion, social proof, effort externalization, curiosity, reciprocity, binary commit.
9. ANTI-PATTERNS (penalized): generic "10% off", multiple CTAs, long preambles, hallucinated data, promotional tone for clinical.

Return ONLY the JSON object."""

ROUTING = {
    "research_digest": "Lead with the specific research finding + trial size + source page. Anchor on merchant's patient cohort. Offer to pull abstract + draft patient content. CTA: open_ended.",
    "regulation_change": "Lead with change + deadline. Loss aversion on non-compliance. Offer to help comply. CTA: binary_yes_stop.",
    "recall_due": "CUSTOMER-FACING. Name customer, state months since last visit, offer 2 specific slots + price. Hindi-English if language_pref=hi. CTA: reply 1/2 for slots.",
    "perf_dip": "Show exact numbers that dropped + delta%. Offer specific fix. Not alarmist. CTA: binary_yes_stop.",
    "perf_spike": "Celebrate with exact numbers. Capitalize momentum: run campaign now. Effort externalization. CTA: binary_yes_stop.",
    "renewal_due": "Anchor on value delivered (views/calls/leads from performance). CTA: binary_yes_stop.",
    "festival_upcoming": "Name festival + days remaining. Specific category offer. Effort externalization. CTA: binary_yes_stop.",
    "review_theme_emerged": "Name the specific theme (from review_themes). Frame as insight. Offer action. CTA: open_ended.",
    "milestone_reached": "Celebrate milestone. Capitalize: shareable post or follow-on campaign. Brief. CTA: binary_yes_stop.",
    "dormant_with_vera": "Ask one genuine category-specific question about their business. NO pitch. Re-engage. CTA: open_ended.",
    "competitor_opened": "Name distance of competitor. Defensive move offer (reviews, profile, offer refresh). Social proof. CTA: binary_yes_stop.",
    "winback_eligible": "Name what they're missing. Category-specific hook. CTA: binary_yes_stop.",
    "curious_ask_due": "Ask ONE genuine business question. No sell. CTA: open_ended.",
    "active_planning_intent": "Effort externalization: 'I've drafted X already'. Move them to action. CTA: binary_yes_stop.",
    "customer_lapsed_hard": "CUSTOMER-FACING. Warm re-engagement. Something new or incentive. CTA: open_ended.",
    "trial_followup": "CUSTOMER-FACING. Ask how trial went + present next step. CTA: open_ended.",
    "supply_alert": "Name drug + action needed. Factual, calm. CTA: binary_yes_stop.",
    "chronic_refill_due": "CUSTOMER-FACING. Brief, practical. Name medication. CTA: open_ended.",
    "gbp_unverified": "Lead with missed searches impact. 5-min verification offer. CTA: binary_yes_stop.",
    "cde_opportunity": "Name event, date, topic. Peer tone. CTA: binary_yes_stop.",
    "ipl_match_today": "Match-day energy. Combo/offer. 'I can push it now'. CTA: binary_yes_stop.",
    "seasonal_perf_dip": "Exact numbers. Counter-seasonal campaign. CTA: binary_yes_stop.",
    "category_seasonal": "Seasonal product/service to stock or promote. CTA: binary_yes_stop.",
    "wedding_package_followup": "CUSTOMER-FACING. Wedding date + days remaining. Next bridal step. CTA: binary_yes_stop.",
}

def build_prompt(category, merchant, trigger, customer):
    kind = trigger.get("kind", "")
    routing_hint = ROUTING.get(kind, "Compose context-appropriate message. Be specific. Use compulsion levers.")

    # Resolve digest item
    digest_item = None
    top_id = trigger.get("payload", {}).get("top_item_id")
    if top_id:
        for item in category.get("digest", []):
            if item.get("id") == top_id:
                digest_item = item
                break

    cat_ctx = {
        "slug": category.get("slug"),
        "voice": category.get("voice", {}),
        "peer_stats": category.get("peer_stats", {}),
        "offers_sample": category.get("offer_catalog", [])[:5],
        "digest_item": digest_item,
        "seasonal_beats": category.get("seasonal_beats", []),
        "trend_signals": category.get("trend_signals", []),
    }

    perf = merchant.get("performance", {})
    mer_ctx = {
        "name": merchant.get("identity", {}).get("name"),
        "owner_first_name": merchant.get("identity", {}).get("owner_first_name"),
        "city": merchant.get("identity", {}).get("city"),
        "locality": merchant.get("identity", {}).get("locality"),
        "languages": merchant.get("identity", {}).get("languages", ["en"]),
        "subscription": merchant.get("subscription", {}),
        "performance": {
            "views_30d": perf.get("views"),
            "calls_30d": perf.get("calls"),
            "ctr": perf.get("ctr"),
            "leads_30d": perf.get("leads"),
            "delta_7d": perf.get("delta_7d", {}),
        },
        "active_offers": [o["title"] for o in merchant.get("offers", []) if o.get("status") == "active"],
        "signals": merchant.get("signals", []),
        "review_themes": merchant.get("review_themes", []),
        "customer_aggregate": merchant.get("customer_aggregate", {}),
        "last_2_turns": merchant.get("conversation_history", [])[-2:],
    }

    cust_ctx = None
    if customer:
        cust_ctx = {
            "name": customer.get("identity", {}).get("name"),
            "language_pref": customer.get("identity", {}).get("language_pref"),
            "state": customer.get("state"),
            "last_visit": customer.get("relationship", {}).get("last_visit"),
            "visits_total": customer.get("relationship", {}).get("visits_total"),
            "services": customer.get("relationship", {}).get("services_received", []),
            "preferences": customer.get("preferences", {}),
        }

    return f"""TRIGGER KIND: {kind.upper()}
ROUTING: {routing_hint}

CATEGORY:
{json.dumps(cat_ctx, ensure_ascii=False, indent=2)}

MERCHANT:
{json.dumps(mer_ctx, ensure_ascii=False, indent=2)}

TRIGGER:
{json.dumps({"id": trigger.get("id"), "kind": kind, "scope": trigger.get("scope"),
              "urgency": trigger.get("urgency"), "payload": trigger.get("payload", {}),
              "suppression_key": trigger.get("suppression_key")}, ensure_ascii=False, indent=2)}

CUSTOMER (null if merchant-facing):
{json.dumps(cust_ctx, ensure_ascii=False, indent=2) if cust_ctx else "null"}

Compose the WhatsApp message. Return ONLY the JSON object."""


def call_llm(prompt):
    if not OPENROUTER_API_KEY:
        raise ValueError("Set OPENROUTER_API_KEY env variable. Free key at openrouter.ai")
    resp = requests.post(OPENROUTER_URL, json={
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": 700,
    }, headers={
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mdriyaas/magicpin-vera",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def validate_result(result, trigger, customer):
    errors = []
    if not result.get("body"):
        errors.append("empty body")
    if result.get("cta") not in {"open_ended", "binary_yes_stop", "none"}:
        errors.append(f"bad cta: {result.get('cta')}")
    if result.get("send_as") not in {"vera", "merchant_on_behalf"}:  # BUG FIX: was outdented, causing SyntaxError
        errors.append(f"bad send_as: {result.get('send_as')}")
    if customer and trigger.get("scope") == "customer" and result.get("send_as") != "merchant_on_behalf":
        errors.append("customer trigger needs merchant_on_behalf")
    if not customer and result.get("send_as") == "merchant_on_behalf":
        errors.append("no customer but send_as=merchant_on_behalf")
    return errors


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    prompt = build_prompt(category, merchant, trigger, customer)
    for attempt in range(2):
        try:
            raw = call_llm(prompt if attempt == 0 else prompt + "\n\nRETRY: Return ONLY valid JSON.")
            clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            result = json.loads(clean)
            errs = validate_result(result, trigger, customer)
            if errs and attempt == 0:
                continue
            if not result.get("suppression_key"):
                result["suppression_key"] = trigger.get("suppression_key", trigger.get("id", ""))
            return result
        except Exception:
            if attempt == 1:
                return {"body": "[compose failed]", "cta": "none", "send_as": "vera",
                        "suppression_key": trigger.get("suppression_key", ""), "rationale": "parse error"}


def load_dataset(base="dataset"):
    """
    Load seed dataset from the dataset directory.
    Expected files:
      dataset/categories.json  — list of category dicts with a 'slug' field
      dataset/merchants.json   — {"merchants": [...]}  OR a list
      dataset/customers.json   — {"customers": [...]}  OR a list  (optional)
      dataset/triggers.json    — {"triggers": [...]}   OR a list
    """
    import pathlib
    b = pathlib.Path(base)

    # ── categories ────────────────────────────────────────────────────────────
    cats = {}
    cat_file = b / "categories.json"
    if cat_file.exists():
        raw = json.loads(cat_file.read_text("utf-8"))
        # Accept either a list or a dict
        if isinstance(raw, list):
            for d in raw:
                cats[d["slug"]] = d
        elif isinstance(raw, dict):
            # Could be {"categories": [...]} or slug-keyed
            if "categories" in raw:
                for d in raw["categories"]:
                    cats[d["slug"]] = d
            else:
                cats = raw  # already slug-keyed
    # Also scan any per-category JSON files in a categories/ subdir
    cat_dir = b / "categories"
    if cat_dir.is_dir():
        for f in cat_dir.glob("*.json"):
            d = json.loads(f.read_text("utf-8"))
            if isinstance(d, dict) and "slug" in d:
                cats[d["slug"]] = d

    # ── merchants ─────────────────────────────────────────────────────────────
    merchants = {}
    for fname in ("merchants.json", "merchants_seed.json"):
        mf = b / fname
        if mf.exists():
            raw = json.loads(mf.read_text("utf-8"))
            items = raw.get("merchants", raw) if isinstance(raw, dict) else raw
            merchants = {m["merchant_id"]: m for m in items}
            break

    # ── customers ─────────────────────────────────────────────────────────────
    customers = {}
    for fname in ("customers.json", "customers_seed.json"):
        cf = b / fname
        if cf.exists():
            raw = json.loads(cf.read_text("utf-8"))
            items = raw.get("customers", raw) if isinstance(raw, dict) else raw
            customers = {c["customer_id"]: c for c in items}
            break

    # ── triggers ──────────────────────────────────────────────────────────────
    triggers = {}
    for fname in ("triggers.json", "triggers_seed.json"):
        tf = b / fname
        if tf.exists():
            raw = json.loads(tf.read_text("utf-8"))
            items = raw.get("triggers", raw) if isinstance(raw, dict) else raw
            triggers = {t["id"]: t for t in items}
            break

    return {"categories": cats, "merchants": merchants, "customers": customers, "triggers": triggers}


if __name__ == "__main__":
    import sys
    ds = load_dataset("dataset")
    tid = sys.argv[1] if len(sys.argv) > 1 else list(ds["triggers"].keys())[0]
    t = ds["triggers"][tid]
    m = ds["merchants"][t["merchant_id"]]
    c = ds["categories"][m["category_slug"]]
    cu = ds["customers"].get(t.get("customer_id") or "")
    print(f"\nTrigger: {tid} | Merchant: {m['identity']['name']}\n")
    r = compose(c, m, t, cu)
    print(json.dumps(r, ensure_ascii=False, indent=2))
