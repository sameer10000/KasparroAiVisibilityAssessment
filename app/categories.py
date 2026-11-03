"""Deterministic prompt categories, inferred from the 50 supplied questions.

Two axes:
  * brand_scope: "branded" if the question names Kasparro, else "unbranded". Inclusion means very
    different things on the two sides, so they are never averaged together.
  * intent: first matching rule wins (order matters). Rules were written by reading all 50 prompts.
"""
import re

CATEGORY_VERSION = "cat-v1"

INTENT_RULES: list[tuple[str, str]] = [
    ("competitor_comparison", r"\b(better than|compare|compared to|vs\.?|between|faster than|"
                              r"profound|otterly|athenahq|peec|dageno)\b"),
    ("discovery_tools", r"(what (software )?tools|what is the best|what kind of tool|how do .* track|how can|"
                        r"why isn't|alternatives to)"),
    ("pricing_commercial", r"\b(cost|price|priced|overpriced|worth the money|worth it|retainer|fees?|value|cancel|lock you)\b"),
    ("onboarding_access", r"\b(setup|set up|onboarding|sign up|self serve|how fast|how long|turnkey|available)\b"),
    ("trust_efficacy", r"\b(legit|trustworthy|actually work|actually help|hype|gimmick|results|safe|skeptical|"
                       r"effective|empty promises|doesn't increase|real .* users|prompt hacks?|spamming|unpredictable)\b"),
    ("capability_features", r"\b(track|tracks|support|features?|audit|score|missing|measure|help)\b"),
    ("discovery_tools", r".*"),
]


def brand_scope(prompt_text: str) -> str:
    return "branded" if re.search(r"\bkasparro", prompt_text, re.I) else "unbranded"


def intent(prompt_text: str) -> str:
    text = prompt_text.lower()
    for name, pattern in INTENT_RULES:
        if re.search(pattern, text):
            return name
    return "discovery_tools"


def categorise(prompt_text: str) -> dict:
    return {"brand_scope": brand_scope(prompt_text), "intent": intent(prompt_text), "version": CATEGORY_VERSION}
