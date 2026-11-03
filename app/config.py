"""Runtime configuration, read once from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the project needs no extra dependency. Real env vars win."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Identity:
    name: str
    role: str  # "analyst" | "reviewer"


@dataclass
class Settings:
    db_path: str = "kasparro.db"
    pack_dir: str = ""
    model_mode: str = "replay"  # replay | stub | ollama
    replay_path: str = "replay/qwen2.5-3b_prompt-v1.jsonl"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    model_timeout_s: float = 60.0
    model_max_retries: int = 2
    model_max_calls: int = 200
    model_max_output_chars: int = 6000
    tokens: dict[str, Identity] = field(default_factory=dict)
    demo_login: bool = False  # local convenience only: lets the web page pick a role without pasting a token


def load_settings() -> Settings:
    _load_dotenv()
    env = os.environ
    tokens = {}
    if env.get("ANALYST_TOKEN"):
        tokens[env["ANALYST_TOKEN"]] = Identity(env.get("ANALYST_NAME", "analyst@local"), "analyst")
    if env.get("REVIEWER_TOKEN"):
        tokens[env["REVIEWER_TOKEN"]] = Identity(env.get("REVIEWER_NAME", "reviewer@local"), "reviewer")
    return Settings(
        db_path=env.get("DB_PATH", "kasparro.db"),
        pack_dir=env.get("PACK_DIR", ""),
        model_mode=env.get("MODEL_MODE", "replay"),
        replay_path=env.get("REPLAY_PATH", "replay/qwen2.5-3b_prompt-v1.jsonl"),
        ollama_url=env.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=env.get("OLLAMA_MODEL", "qwen2.5:3b"),
        model_timeout_s=float(env.get("MODEL_TIMEOUT_S", "60")),
        model_max_retries=int(env.get("MODEL_MAX_RETRIES", "2")),
        model_max_calls=int(env.get("MODEL_MAX_CALLS", "200")),
        model_max_output_chars=int(env.get("MODEL_MAX_OUTPUT_CHARS", "6000")),
        tokens=tokens,
        demo_login=env.get("DEMO_LOGIN", "false").lower() == "true",
    )
