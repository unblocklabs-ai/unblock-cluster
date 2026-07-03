#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ALLOWED_SIZES = {5000, 50000, 100000}
SOURCE_TYPES = [
    "support_ticket",
    "product_review",
    "social_comment",
    "survey_response",
    "chat_transcript",
    "marketplace_question",
]
PRODUCTS = ["Sleep Drops", "Focus Gummies", "Greens Powder", "Electrolyte Mix", "Collagen Capsules"]
SENTIMENTS = ["positive", "neutral", "negative"]

TOPICS: dict[str, dict[str, list[str]]] = {
    "subscription_cancellation_errors": {
        "subjects": ["subscription cancellation", "account portal", "recurring order"],
        "problems": ["keeps failing", "will not save", "shows an error"],
        "asks": ["cancel before renewal", "stop the next shipment", "get confirmation"],
    },
    "gummy_melting_shipping": {
        "subjects": ["focus gummies", "summer delivery", "shipping box"],
        "problems": ["arrived melted", "stuck together", "lost their shape"],
        "asks": ["replace the bottle", "improve the cold pack", "refund the order"],
    },
    "taste_complaints": {
        "subjects": ["greens powder", "daily scoop", "new flavor"],
        "problems": ["tastes bitter", "has a chalky finish", "is too sweet"],
        "asks": ["suggest a mix-in", "swap flavors", "share serving tips"],
    },
    "efficacy_questions": {
        "subjects": ["sleep drops", "serving size", "expected results"],
        "problems": ["does not feel different yet", "works inconsistently", "takes too long"],
        "asks": ["know when it should work", "adjust the dose", "compare formulas"],
    },
    "refund_friction": {
        "subjects": ["refund request", "return label", "support follow-up"],
        "problems": ["has taken too long", "was denied", "needs another approval"],
        "asks": ["speed up the refund", "send a label", "escalate the case"],
    },
    "adverse_event_mentions": {
        "subjects": ["new supplement", "first week", "reaction concern"],
        "problems": ["made me nauseous", "caused headaches", "upset my stomach"],
        "asks": ["ask if this is normal", "report the reaction", "pause the subscription"],
    },
    "december_energy_crash_spike": {
        "subjects": ["holiday energy bundle", "December routine", "afternoon crash"],
        "problems": ["energy drops hard", "does not last through work", "wears off after lunch"],
        "asks": ["find a stronger option", "understand the crash", "change timing"],
    },
    "november_creatine_questions": {
        "subjects": ["new creatine launch", "November bundle", "strength stack"],
        "problems": ["needs loading guidance", "asks about water weight", "worries about mixing"],
        "asks": ["compare with gummies", "explain the protocol", "confirm safety"],
    },
    "midyear_vanishing_packaging": {
        "subjects": ["old pouch seal", "powder packaging", "scoop bag"],
        "problems": ["seal splits open", "zipper will not close", "powder spills"],
        "asks": ["replace the pouch", "send a canister", "fix packaging"],
    },
    "discount_code_failures": {
        "subjects": ["discount code", "checkout", "promo email"],
        "problems": ["code is invalid", "discount disappeared", "cannot combine offers"],
        "asks": ["apply the sale", "honor the email", "fix checkout"],
    },
    "delivery_delay": {
        "subjects": ["delivery date", "carrier scan", "tracking page"],
        "problems": ["has not moved", "missed the promise date", "went to the wrong city"],
        "asks": ["reship the package", "check tracking", "expedite replacement"],
    },
    "serving_size_confusion": {
        "subjects": ["label directions", "scoop size", "daily serving"],
        "problems": ["directions conflict", "scoop is missing", "serving seems high"],
        "asks": ["confirm the right amount", "send a scoop", "explain the label"],
    },
    "bundle_customization": {
        "subjects": ["monthly bundle", "flavor mix", "subscription box"],
        "problems": ["cannot swap items", "wants a different flavor", "needs fewer bottles"],
        "asks": ["customize the bundle", "skip one item", "change flavors"],
    },
    "allergen_concerns": {
        "subjects": ["ingredient list", "allergy check", "shared facility"],
        "problems": ["contains coconut", "may include soy", "mentions tree nuts"],
        "asks": ["confirm allergens", "get a certificate", "find an alternative"],
    },
    "international_shipping": {
        "subjects": ["international order", "customs form", "cross-border shipping"],
        "problems": ["customs held it", "duties were high", "shipping is unavailable"],
        "asks": ["estimate duties", "ship to Canada", "update paperwork"],
    },
    "loyalty_points": {
        "subjects": ["loyalty account", "points balance", "reward redemption"],
        "problems": ["points are missing", "reward will not apply", "tier did not update"],
        "asks": ["restore the points", "apply the reward", "merge accounts"],
    },
    "ingredient_sourcing": {
        "subjects": ["ingredient sourcing", "testing certificate", "quality page"],
        "problems": ["wants more detail", "cannot find the COA", "asks about origin"],
        "asks": ["share testing proof", "explain sourcing", "send documentation"],
    },
    "texture_clumping": {
        "subjects": ["powder texture", "opened tub", "mixability"],
        "problems": ["clumps in water", "turned hard", "sticks to the scoop"],
        "asks": ["prevent clumping", "replace the tub", "mix it smoothly"],
    },
    "gift_order_changes": {
        "subjects": ["gift order", "shipping address", "gift note"],
        "problems": ["used my address", "forgot the note", "needs anonymous billing"],
        "asks": ["change the address", "add a note", "hide the price"],
    },
    "app_login_issue": {
        "subjects": ["mobile app", "login code", "account access"],
        "problems": ["code never arrives", "gets signed out", "cannot reset password"],
        "asks": ["restore access", "send a new code", "merge accounts"],
    },
}


def _random_2025(rng: random.Random) -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(
        days=rng.randrange(365),
        seconds=rng.randrange(86400),
    )


def _special_topic_and_time(index: int, rng: random.Random) -> tuple[str | None, datetime | None]:
    if index % 20 == 0:
        day = rng.randrange(1, 32)
        return "december_energy_crash_spike", datetime(
            2025,
            12,
            day,
            rng.randrange(24),
            rng.randrange(60),
            tzinfo=UTC,
        )
    if index % 50 == 1:
        month = 11 if rng.random() < 0.65 else 12
        max_day = 30 if month == 11 else 31
        return "november_creatine_questions", datetime(
            2025,
            month,
            rng.randrange(1, max_day + 1),
            rng.randrange(24),
            rng.randrange(60),
            tzinfo=UTC,
        )
    if index % 30 == 2:
        month = rng.randrange(1, 7)
        max_day = 28 if month == 2 else 30
        return "midyear_vanishing_packaging", datetime(
            2025,
            month,
            rng.randrange(1, max_day + 1),
            rng.randrange(24),
            rng.randrange(60),
            tzinfo=UTC,
        )
    return None, None


def _choose_regular_topic(timestamp: datetime, rng: random.Random) -> str:
    excluded = {
        "december_energy_crash_spike",
        "november_creatine_questions",
        "midyear_vanishing_packaging",
    }
    choices = [topic for topic in TOPICS if topic not in excluded]
    if timestamp.month == 12 and rng.random() < 0.08:
        return "december_energy_crash_spike"
    return rng.choice(choices)


def _customer_text(topic_id: str, rng: random.Random) -> tuple[str, str]:
    pools = TOPICS[topic_id]
    subject = rng.choice(pools["subjects"])
    problem = rng.choice(pools["problems"])
    ask = rng.choice(pools["asks"])
    title = f"{subject.title()} - {problem}"
    text = f"My {subject} {problem}. I need help to {ask}."
    if rng.random() < 0.45:
        text += f" This is for {rng.choice(PRODUCTS)} and I expected a smoother experience."
    return title, text


def _source_fields(source_type: str, index: int, rng: random.Random) -> tuple[str, str, str]:
    source_name = {
        "support_ticket": "zendesk",
        "product_review": "shopify_reviews",
        "social_comment": rng.choice(["instagram", "facebook", "tiktok"]),
        "survey_response": "post_purchase_survey",
        "chat_transcript": "gorgias_chat",
        "marketplace_question": "amazon_questions",
    }[source_type]
    source_record_id = f"{source_name}-{index:06d}-{rng.randrange(1000, 9999)}"
    return source_type, source_name, source_record_id


def make_record(index: int, rng: random.Random) -> dict[str, Any]:
    topic_id, timestamp = _special_topic_and_time(index, rng)
    timestamp = timestamp or _random_2025(rng)
    topic_id = topic_id or _choose_regular_topic(timestamp, rng)
    source_type = SOURCE_TYPES[index % len(SOURCE_TYPES)]
    source_type, source_name, source_record_id = _source_fields(source_type, index, rng)
    title, customer_text = _customer_text(topic_id, rng)
    product = rng.choice(PRODUCTS)
    rating = None if source_type == "social_comment" else rng.choice([1, 2, 3, 4, 5])
    sentiment = rng.choices(SENTIMENTS, weights=[0.25, 0.35, 0.40], k=1)[0]
    tags = [topic_id.split("_")[0], source_type]
    metadata = {
        "groundTruthTopicId": topic_id,
        "syntheticSeedTopic": topic_id,
        "temporalPattern": (
            "december_spike"
            if topic_id == "december_energy_crash_spike"
            else "november_first_seen"
            if topic_id == "november_creatine_questions"
            else "midyear_vanishing"
            if topic_id == "midyear_vanishing_packaging"
            else "baseline"
        ),
    }
    record: dict[str, Any] = {
        "recordId": f"rec_{index:06d}",
        "sourceType": source_type,
        "sourceName": source_name,
        "sourceRecordId": source_record_id,
        "title": None if source_type == "social_comment" else title,
        "customerText": customer_text,
        "recordUrl": f"https://example.test/{source_name}/{source_record_id}",
        "product": product,
        "sku": product.upper().replace(" ", "-")[:12],
        "rating": rating,
        "sentiment": sentiment,
        "tags": tags,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "metadata": metadata,
    }
    return record


def generate_records(size: int, seed: int) -> list[dict[str, Any]]:
    if size not in ALLOWED_SIZES:
        raise ValueError("--size must be one of 5000, 50000, or 100000")
    rng = random.Random(seed)
    return [make_record(index, rng) for index in range(size)]


def write_records(records: list[dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic synthetic supplement-DTC records."
    )
    parser.add_argument("--size", type=int, choices=sorted(ALLOWED_SIZES), default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/synthetic-5k.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    write_records(generate_records(args.size, args.seed), args.out)


if __name__ == "__main__":
    main()
