"""Deterministic extraction and validation of model output.

Counting rules
  * Mentions are detected in answer_markdown (the prose view). Link targets and bare URLs are removed
    first, so a kasparro.com URL alone is a citation, not a mention. answer_text is used only when
    answer_markdown is missing. The two views are one response and are never counted twice.
  * Citations: every row in citations.jsonl is an occurrence. For "cited" flags we deduplicate per
    response on a normalised URL (lower-case host, no fragment, no utm_* params, no trailing slash).
"""
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

BRAND_RE = re.compile(r"\bkasparro\b", re.I)
KASPR_RE = re.compile(r"\bkaspr\b", re.I)
URL_RE = re.compile(r"https?://\S+")
MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
OWNED_DOMAINS = ("kasparro.com",)
NOT_COMPETITORS = {"kasparro", "kaspr", "chatgpt", "openai", "gemini", "claude", "perplexity", "copilot",
                   "google", "google ai mode", "ai mode", "google ai overviews", "bing", "microsoft copilot"}

# Used by the rule-based baseline and as a cross-check; built from reading the answers.
KNOWN_COMPETITORS = ["Profound", "Peec AI", "Otterly", "AthenaHQ", "Semrush", "Ahrefs", "Brand Radar", "HubSpot",
                     "SE Ranking", "Dageno", "Scrunch", "ZipTie", "Goodie", "Conductor", "AirOps", "Writesonic",
                     "Rankscale", "BrightEdge", "Similarweb", "seoClarity", "Brand24", "Gauge", "Siftly"]


def prose_view(answer_markdown: str | None, answer_text: str) -> str:
    text = answer_markdown if answer_markdown else answer_text
    text = MD_LINK_TARGET_RE.sub("]", text)
    return URL_RE.sub(" ", text)


def norm_ws(s: str) -> str:
    s = re.sub(r"[*_`#>|]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


QUOTE_MATCH_VERSION = "words-v2"
WORD_RE = re.compile(r"[a-z0-9]+")
MIN_QUOTE_WORDS = 4


def quote_in(quote: str, text: str) -> bool:
    """Verbatim-quote check on word sequences: the quote's words must appear contiguously, in order,
    in the answer. Punctuation, quote styles, markdown and escapes are ignored; any added, removed or
    changed word fails. (v1 compared characters and rejected true quotes; see tests/test_evidence_match.py.)"""
    q = WORD_RE.findall(quote.lower())
    if len(q) < MIN_QUOTE_WORDS:
        return False
    return f" {' '.join(q)} " in f" {' '.join(WORD_RE.findall(text.lower()))} "


INJECTION_RE = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard (the |all )?(previous|above)|"
    r"(system|developer) (note|prompt|message) to (the )?(classifier|model|assistant)|"
    r"kasparro_recommended|negative_characterisation|entity_confusion", re.I)


def injection_suspected(text: str) -> str | None:
    """Instruction-like text aimed at a labeller. Returns the matched phrase or None."""
    m = INJECTION_RE.search(text)
    return m.group(0) if m else None


def sentence_with(text: str, pattern: re.Pattern, max_len: int = 280) -> str | None:
    """Return the verbatim sentence/line around the first match, for evidence excerpts."""
    m = pattern.search(text)
    if not m:
        return None
    start = max(text.rfind("\n", 0, m.start()), text.rfind(". ", 0, m.start()) + 1, 0)
    end_candidates = [i for i in (text.find("\n", m.end()), text.find(". ", m.end())) if i != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    excerpt = text[start:end].strip()
    if len(excerpt) > max_len:
        lo = max(0, m.start() - start - max_len // 2)
        excerpt = excerpt[lo:lo + max_len].strip()
    return excerpt


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def citation_facts(citations: list[dict]) -> dict:
    unique = {normalise_url(c["url"]) for c in citations}
    owned = {u for u in unique if any(urlsplit(u).netloc.endswith(d) for d in OWNED_DOMAINS)}
    about = {u for u in unique if "kasparro" in u and u not in owned}
    return {
        "occurrences": len(citations),
        "unique_urls": len(unique),
        "owned_cited": bool(owned),
        "owned_urls": sorted(owned),
        "third_party_kasparro_urls": sorted(about),
    }


def deterministic_facts(answer_markdown: str | None, answer_text: str, citations: list[dict]) -> dict:
    prose = prose_view(answer_markdown, answer_text)
    return {
        "mentioned": bool(BRAND_RE.search(prose)),
        "mention_count": len(BRAND_RE.findall(prose)),
        "mention_excerpt": sentence_with(prose, BRAND_RE),
        "kaspr_named": bool(KASPR_RE.search(prose)),
        "dictionary_competitors": [c for c in KNOWN_COMPETITORS if re.search(r"\b" + re.escape(c) + r"\b", prose, re.I)],
        "citations": citation_facts(citations),
    }


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    recommended: str = ""
    negative: str = ""
    confusion: str = ""


class ModelLabels(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kasparro_recommended: bool
    negative_characterisation: bool
    entity_confusion: bool
    recommendation_position: int | None = Field(default=None, ge=1, le=50)
    competitors: list[str] = Field(default_factory=list, max_length=60)
    evidence: Evidence = Field(default_factory=Evidence)


class OutputInvalid(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def parse_model_output(raw: str) -> ModelLabels:
    if raw is None or not raw.strip():
        raise OutputInvalid("empty_output", "model returned no content")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OutputInvalid("malformed_json", str(e))
    if not isinstance(data, dict):
        raise OutputInvalid("schema_mismatch", "top level is not an object")
    try:
        return ModelLabels.model_validate(data)
    except ValidationError as e:
        raise OutputInvalid("schema_mismatch", "; ".join(x["msg"] for x in e.errors()[:3]))


def reconcile(facts: dict, labels: ModelLabels, prose: str) -> tuple[dict, dict, list[str]]:
    """Combine deterministic facts with validated model labels.
    Returns (final labels, evidence, guard notes). A model label survives only with a verbatim quote."""
    notes: list[str] = []
    evidence: dict[str, str] = {}
    mentioned = facts["mentioned"]
    if mentioned:
        evidence["mentioned"] = facts["mention_excerpt"]

    def keep(flag: bool, key: str, quote: str) -> bool:
        if not flag:
            return False
        if not mentioned and key != "confusion":
            notes.append(f"{key}: dropped, Kasparro not mentioned in answer")
            return False
        if not quote_in(quote, prose):
            notes.append(f"{key}: dropped, evidence quote not found verbatim in answer")
            return False
        evidence[key] = quote.strip()
        return True

    recommended = keep(labels.kasparro_recommended, "recommended", labels.evidence.recommended)
    negative = keep(labels.negative_characterisation, "negative", labels.evidence.negative)
    confusion = keep(labels.entity_confusion, "confusion", labels.evidence.confusion)

    position = labels.recommendation_position if recommended else None
    if labels.recommendation_position and not recommended:
        notes.append("position: dropped, Kasparro not recommended")

    competitors, rejected = [], []
    for name in dict.fromkeys(c.strip() for c in labels.competitors if c and c.strip()):
        if name.lower() in NOT_COMPETITORS or BRAND_RE.search(name):
            continue
        (competitors if re.search(re.escape(name), prose, re.I) else rejected).append(name)
    if rejected:
        notes.append(f"competitors not found in answer and dropped: {rejected[:10]}")

    final = {
        "absent": not mentioned,
        "mentioned": mentioned,
        "recommended": recommended,
        "negative": negative,
        "entity_confusion": confusion,
        "recommendation_position": position,
        "competitors": competitors,
    }
    return final, evidence, notes
