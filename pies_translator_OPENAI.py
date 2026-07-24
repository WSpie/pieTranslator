# pies_translator_OPENAI.py
# -*- coding: utf-8 -*-

import os
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, List, Tuple, Set
from collections import defaultdict, deque

import yaml
import discord
from discord import Intents, app_commands
from dotenv import load_dotenv

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

import db

# ---- TLS fallback ----
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

# ========== Config ==========
# Secrets: env vars take precedence (Heroku-style). config.yaml is a fallback
# for local development. Non-secret runtime knobs stay in .env.
load_dotenv()

_cfg: dict = {}
CONFIG_FILE = Path("config.yaml")
if CONFIG_FILE.exists():
    try:
        _cfg = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"Failed to parse {CONFIG_FILE}: {e}")


def _secret(key: str) -> str:
    return (os.getenv(key) or str(_cfg.get(key, "") or "")).strip()


# Provider switch: "1" = OpenAI (default), "2" = TAMU AI Chat.
API_OPTION = (os.getenv("API_OPTION") or str(_cfg.get("API_OPTION", "") or "") or "1").strip()

DISCORD_TOKEN  = _secret("DISCORD_TOKEN")
OPENAI_API_KEY = _secret("OPENAI_API_KEY")
TAMU_API_KEY   = _secret("TAMU_API_KEY")
DATABASE_URL   = _secret("DATABASE_URL")

DEBUG_MODE             = os.getenv("PIES_DEBUG", "0").strip() == "1"
FLAG_EPHEMERAL_SECONDS = int(os.getenv("FLAG_EPHEMERAL_SECONDS", "60"))
OPENAI_MODEL           = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# TAMU AI Chat is OpenAI-compatible (powered by Open WebUI).
# - Endpoint is institution-specific; Texas A&M University = https://chat-api.tamu.ai
#   (see https://docs.tamus.ai/docs/prod/api-tool/api-endpoints/ for other campuses).
# - The OpenAI SDK appends /chat/completions and /models to base_url, so it ends in /api.
# - TAMU model ids are namespaced with a "protected." prefix, e.g. protected.gpt-4.1-mini.
#   List them: curl -H "Authorization: Bearer $TAMU_API_KEY" https://chat-api.tamu.ai/api/models
TAMU_API_ENDPOINT = os.getenv("TAMU_API_ENDPOINT", "https://chat-api.tamu.ai").strip().rstrip("/")
TAMU_BASE_URL     = TAMU_API_ENDPOINT + "/api"
TAMU_MODEL        = os.getenv("TAMU_MODEL", "protected.gpt-5.4-mini").strip()

USE_TAMU     = API_OPTION == "2"
ACTIVE_MODEL = TAMU_MODEL if USE_TAMU else OPENAI_MODEL
PROVIDER     = "TAMU" if USE_TAMU else "OpenAI"

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN: set env var or add it to config.yaml")
if USE_TAMU:
    if not TAMU_API_KEY:
        raise SystemExit("API_OPTION=2 but TAMU_API_KEY is missing: set env var or add it to config.yaml")
else:
    if not OPENAI_API_KEY:
        raise SystemExit("API_OPTION=1 but OPENAI_API_KEY is missing: set env var or add it to config.yaml")
if not DATABASE_URL:
    raise SystemExit(
        "Missing DATABASE_URL: set env var (Heroku Postgres injects it) "
        "or add it to config.yaml for local dev."
    )

NO_MENTIONS = discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False)
MENTION_USER = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)

# ========== LOG ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("PieTrans")
if DEBUG_MODE:
    log.setLevel(logging.DEBUG)

# ========== LLM client (OpenAI-compatible) ==========
# 说明：
# - timeout/max_retries 在这里做默认兜底
# - 单次请求仍可用 asyncio.wait_for 进一步收紧
# - API_OPTION=2 时指向 TAMU AI Chat（OpenAI 兼容），否则用官方 OpenAI。
if USE_TAMU:
    client_ai = AsyncOpenAI(api_key=TAMU_API_KEY, base_url=TAMU_BASE_URL, timeout=60.0, max_retries=2)
    log.info(f"LLM provider: TAMU AI Chat | base_url={TAMU_BASE_URL} | model={ACTIVE_MODEL}")
else:
    client_ai = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=60.0, max_retries=2)
    log.info(f"LLM provider: OpenAI | model={ACTIVE_MODEL}")

# ========== Storage ==========
# All runtime state lives in Postgres (via db.py) so the bot can run on
# ephemeral hosts like Heroku. profile/ stays on disk — it's human-edited
# game profile that ships with the code.
PROFILE_DIR = Path("profile")
PROFILE_DIR.mkdir(exist_ok=True)

# In-memory caches mirror the DB for hot-read paths. Writes update the
# cache synchronously and schedule a DB upsert via _fire_db().
relay_map: Dict[str, Dict[str, int]] = {}
relay_origin: Dict[str, int] = {}
reverse_relay: Dict[str, Dict[str, int]] = {}
usage: Dict[str, int] = {}


def _fire_db(coro) -> None:
    """Schedule a DB write from sync code. Errors are logged, not raised."""
    try:
        asyncio.create_task(_safe_run_db(coro))
    except RuntimeError:
        # No running loop (shouldn't happen in normal flow — all mutators
        # are called from coroutine context). Drop and warn.
        try:
            coro.close()
        except Exception:
            pass
        log.warning("_fire_db: no running event loop; dropping write")


async def _safe_run_db(coro) -> None:
    try:
        await coro
    except Exception as e:
        log.warning(f"DB write failed: {e}")


async def load_reverse_relay():
    global reverse_relay
    try:
        reverse_relay = await db.load_relay_reverse()
    except Exception as e:
        log.warning(f"load_reverse_relay failed: {e}")
        reverse_relay = {}


def reverse_set(relayed_msg_id: int, src_msg_id: int, src_channel_id: int):
    reverse_relay[str(relayed_msg_id)] = {
        "src_msg_id": int(src_msg_id),
        "src_channel_id": int(src_channel_id),
    }
    _fire_db(db.upsert_relay_reverse(int(relayed_msg_id), int(src_msg_id), int(src_channel_id)))

def reverse_get(relayed_msg_id: int) -> Optional[Tuple[int, int]]:
    d = reverse_relay.get(str(relayed_msg_id))
    if not d:
        return None
    return int(d.get("src_msg_id", 0) or 0), int(d.get("src_channel_id", 0) or 0)

# ----------------------------------------------------------

def user_label(u: discord.abc.User) -> str:
    name = u.display_name if isinstance(u, discord.Member) else u.name
    return f"{name} ({u.id})"

def bump(u: discord.abc.User):
    key = user_label(u)
    usage[key] = usage.get(key, 0) + 1
    _fire_db(db.bump_user_usage(key, 1))


async def load_usage():
    global usage
    try:
        usage = await db.load_user_usage()
    except Exception as e:
        log.warning(f"load_usage failed: {e}")
        usage = {}

# ========== Discord Client ==========
intents = Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.reactions = True

class Bot(discord.Client):
    def __init__(self, *, intents: Intents, **kwargs):
        super().__init__(intents=intents, **kwargs)
        self.tree = app_commands.CommandTree(self)

bot = Bot(intents=intents)


class BoundedIdSet:
    """FIFO-bounded set for tracking recent message IDs without unbounded growth."""
    __slots__ = ("_set", "_queue")

    def __init__(self, max_size: int = 20000):
        self._set: Set[int] = set()
        self._queue: deque = deque(maxlen=max_size)

    def add(self, item: int) -> None:
        if item in self._set:
            return
        if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
            self._set.discard(self._queue[0])
        self._queue.append(item)
        self._set.add(item)

    def __contains__(self, item: int) -> bool:
        return item in self._set


our_message_ids: BoundedIdSet = BoundedIdSet(max_size=20000)
processed_ids: BoundedIdSet = BoundedIdSet(max_size=20000)

# ===== Inflight control =====
MAX_INFLIGHT_TRANSLATES = 6
TRANSLATE_SEM = asyncio.Semaphore(MAX_INFLIGHT_TRANSLATES)
CHANNEL_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ========== Language meta ==========
LANG_RE  = re.compile(r"^[a-z]{2}(?:-[a-z]{4})?$", re.I)
LANG_NAME = {
    "en":"English","ja":"Japanese","zh":"Chinese","ko":"Korean","es":"Spanish",
    "fr":"French","ar":"Arabic","zh-hant":"Traditional Chinese"
}

def _norm_lang(code: Optional[str]) -> str:
    if not code:
        return ""
    code = code.strip().lower()
    aliases = {
        "en-us":"en","en-gb":"en","en-au":"en","en-ca":"en",
        "zh-cn":"zh","zh-hans":"zh","zh-sg":"zh",
        "zh-hant":"zh-hant","zh-tw":"zh-hant","zh-hk":"zh-hant","zh-mo":"zh-hant",
        "es-419":"es","es-mx":"es","es-es":"es","es-ar":"es","es-co":"es",
        "jp":"ja",
    }
    return aliases.get(code, code)

def lang_similarity_pct(src_code: Optional[str], tgt_code: Optional[str]) -> int:
    s = _norm_lang(src_code); t = _norm_lang(tgt_code)
    if not s or not t: return 0
    if s == t: return 100
    if (s.startswith("zh") and t.startswith("zh")):
        return 90 if ("hant" in s or "hant" in t) else 95
    if s.startswith("es") and t.startswith("es"): return 95
    return 0

LATIN_WORD = re.compile(r"[A-Za-z']+")
EN_STOP = {"the","and","to","of","in","is","it","you","for","on","with","that","this",
           "but","are","can","a","be","as","at","we","i","have","has","not","more",
           "from","or","by","if","your","our","they","will","about"}
def _ratio(pred: int, total: int) -> float: return 0.0 if total <= 0 else pred/float(total)

def english_like(text: str) -> bool:
    toks = [t.lower() for t in LATIN_WORD.findall(text)]
    if not toks: return False
    hits = sum(1 for t in toks if t in EN_STOP)
    ascii_ratio = _ratio(sum(1 for ch in text if ord(ch) < 128), len(text))
    return hits >= 2 or (len(toks) >= 5 and ascii_ratio >= 0.9)

def script_bucket(text: str) -> str:
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf')
    hira_kata = sum(1 for ch in text if '\u3040' <= ch <= '\u30ff')
    hangul = sum(1 for ch in text if '\uac00' <= ch <= '\ud7af')
    arabic = sum(1 for ch in text if '\u0600' <= ch <= '\u06ff' or '\u0750' <= ch <= '\u077f')
    total = max(len(text), 1)
    if _ratio(cjk, total) >= 0.2: return "zh"
    if _ratio(hira_kata, total) >= 0.2: return "ja"
    if _ratio(hangul, total) >= 0.2: return "ko"
    if _ratio(arabic, total) >= 0.2: return "ar"
    return "latin"

def fast_lang_agree(text: str, target_code: str) -> bool:
    t = _norm_lang(target_code); b = script_bucket(text)
    if t in {"zh","zh-hant"} and b == "zh": return True
    if t == "ja" and b == "ja": return True
    if t == "ko" and b == "ko": return True
    if t == "ar" and b == "ar": return True
    if t == "en" and english_like(text): return True
    return False

# ========= Text meaning detector =========
_EMOJI_CUSTOM = re.compile(r"<a?:\w+:\d+>")
_MENTION_ANY = re.compile(r"<[@#&!]\d+>")
_URL_ANY     = re.compile(r"https?://\S+", flags=re.I)

def text_is_meaningful(text: str) -> bool:
    if not text: return False
    s = _EMOJI_CUSTOM.sub("", text)
    s = _MENTION_ANY.sub("", s)
    s = _URL_ANY.sub("", s)
    s = s.strip()
    if not s: return False
    return any(ch.isalnum() for ch in s)

# ========== Rules ==========
Rules = Dict[str, Dict[str, object]]
rules: Rules = {}

async def load_rules():
    global rules
    try:
        rules = await db.load_rules()
    except Exception as e:
        log.warning(f"load_rules failed: {e}")
        rules = {}


def _persist_rule(cid: int) -> None:
    r = rules.get(str(cid)) or {}
    _fire_db(db.upsert_rule(
        int(cid),
        r.get("language"),
        bool(r.get("flag", False)),
        int(r["link_channel_id"]) if r.get("link_channel_id") is not None else None,
    ))


def get_rule(cid: int) -> Optional[dict]: return rules.get(str(cid))
def set_rule(cid: int, language: Optional[str], flag: bool = False, link_channel_id: Optional[int] = None):
    rules[str(cid)] = {"language": language, "flag": flag, "link_channel_id": link_channel_id}
    _persist_rule(cid)
def update_rule(cid: int, language: Optional[str] = None, flag: Optional[bool] = None):
    r = rules.get(str(cid))
    if not r: return False
    if language is not None: r["language"] = language
    if flag is not None: r["flag"] = flag
    _persist_rule(cid); return True
def link_channels(a: int, b: int):
    if a == b: return
    ra = rules.get(str(a)) or {"language": None, "flag": False, "link_channel_id": None}
    rb = rules.get(str(b)) or {"language": None, "flag": False, "link_channel_id": None}
    ra["link_channel_id"] = b; rb["link_channel_id"] = a
    rules[str(a)] = ra; rules[str(b)] = rb
    _persist_rule(a); _persist_rule(b)
def del_rule(cid: int):
    r = rules.get(str(cid))
    if r and r.get("link_channel_id"):
        other = str(int(r["link_channel_id"]))
        if other in rules and rules[other].get("link_channel_id") == cid:
            rules[other]["link_channel_id"] = None
            _persist_rule(int(other))
    rules.pop(str(cid), None)
    _fire_db(db.delete_rule(int(cid)))

# ========== Relay map/origin ==========
async def load_relay_map():
    global relay_map
    try:
        relay_map = await db.load_relay_map()
    except Exception as e:
        log.warning(f"load_relay_map failed: {e}")
        relay_map = {}

def map_set(src_msg_id: int, target_channel_id: int, target_msg_id: int):
    k = str(src_msg_id); ch = str(target_channel_id)
    d = relay_map.get(k) or {}; d[ch] = int(target_msg_id); relay_map[k] = d
    _fire_db(db.upsert_relay_map(int(src_msg_id), int(target_channel_id), int(target_msg_id)))

def map_get(src_msg_id: int, target_channel_id: int) -> Optional[int]:
    return (relay_map.get(str(src_msg_id)) or {}).get(str(target_channel_id))

async def load_relay_origin():
    global relay_origin
    try:
        relay_origin = await db.load_relay_origin()
    except Exception as e:
        log.warning(f"load_relay_origin failed: {e}")
        relay_origin = {}

def origin_set(relayed_msg_id: int, origin_channel_id: int):
    relay_origin[str(relayed_msg_id)] = int(origin_channel_id)
    _fire_db(db.upsert_relay_origin(int(relayed_msg_id), int(origin_channel_id)))

def origin_get(relayed_msg_id: int) -> Optional[int]:
    v = relay_origin.get(str(relayed_msg_id)); return int(v) if v is not None else None

# ========== OpenAI budget (optional but recommended) ==========
CST = ZoneInfo("America/Chicago")
DAILY_RESET_HOUR = 19

# 你可以保留这个预算逻辑，或改成 0/None 来禁用
BUDGET_DOLLARS_PER_DAY = float(os.getenv("OPENAI_BUDGET_DOLLARS_PER_DAY", "5.00"))

# 注意：这里的单价只是占位（不同模型不同价）
# 如果你要精确计费，请按你实际模型价格更新
COST_PER_1M_INPUT  = float(os.getenv("OPENAI_COST_PER_1M_INPUT",  "1.25"))
COST_PER_1M_OUTPUT = float(os.getenv("OPENAI_COST_PER_1M_OUTPUT", "10.00"))

def _now_cst() -> datetime: return datetime.now(tz=CST)
def _window_start(now: datetime) -> datetime:
    today_reset = now.replace(hour=DAILY_RESET_HOUR, minute=0, second=0, microsecond=0)
    return today_reset if now >= today_reset else today_reset - timedelta(days=1)
def _window_label(now: datetime) -> str: return now.strftime("%m/%d/%Y")

# Mirror of the DB usage_state row. Sync reads from memory; mutations fire
# a DB upsert.
_usage_state_mem: Dict[str, object] = dict(db.DEFAULT_USAGE_STATE)

async def load_usage_state_from_db():
    global _usage_state_mem
    try:
        _usage_state_mem = await db.load_usage_state()
    except Exception as e:
        log.warning(f"load_usage_state failed: {e}")
        _usage_state_mem = dict(db.DEFAULT_USAGE_STATE)

def _load_usage_state() -> dict:
    return dict(_usage_state_mem)

def _save_usage_state(state: dict):
    global _usage_state_mem
    _usage_state_mem = dict(state)
    _fire_db(db.save_usage_state(state))

def _reset_if_needed(state: dict) -> dict:
    now = _now_cst(); ws = _window_start(now); prev = state.get("window_start")
    if not prev or datetime.fromisoformat(prev) != ws:
        if prev:
            try:
                date_for_log = _window_label(datetime.fromisoformat(prev))
                used = int(state.get("total_tokens", 0))
                _fire_db(db.append_usage_log(date_for_log, used))
            except Exception:
                pass
        state = {"window_start": ws.isoformat(),"prompt_tokens": 0,"completion_tokens": 0,"total_tokens": 0,
                 "input_cost": 0.0,"output_cost": 0.0,"total_cost": 0.0}
        _save_usage_state(state)
    return state
def _append_usage(state: dict, prompt_toks: int, completion_toks: int) -> dict:
    state = _reset_if_needed(state)
    state["prompt_tokens"] += int(prompt_toks); state["completion_tokens"] += int(completion_toks)
    state["total_tokens"] = state["prompt_tokens"] + state["completion_tokens"]
    inc_input_cost  = (prompt_toks     / 1_000_000.0) * COST_PER_1M_INPUT
    inc_output_cost = (completion_toks / 1_000_000.0) * COST_PER_1M_OUTPUT
    state["input_cost"]  = round(state["input_cost"]  + inc_input_cost, 6)
    state["output_cost"] = round(state["output_cost"] + inc_output_cost, 6)
    state["total_cost"]  = round(state["input_cost"] + state["output_cost"], 6)
    _save_usage_state(state); return state
def _budget_ok(state: dict) -> bool:
    if BUDGET_DOLLARS_PER_DAY <= 0:
        return True
    state = _reset_if_needed(state)
    return state.get("total_cost", 0.0) < BUDGET_DOLLARS_PER_DAY

# ========== OpenAI chat wrapper ==========
def _parse_completion(r) -> Tuple[str, int, int]:
    """Extract (content, prompt_tokens, completion_tokens) from a chat completion.

    Handles the three shapes we can get back:
      - a parsed ChatCompletion object (official OpenAI),
      - a dict (some OpenAI-compatible gateways),
      - a raw JSON str (TAMU/Open WebUI sometimes returns the body unparsed).
    """
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            raise RuntimeError(f"Unexpected non-JSON response: {r[:200]!r}")

    if isinstance(r, dict):
        choices = r.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        reply = (msg.get("content") or "").strip()
        usage_obj = r.get("usage") or {}
        prompt_tokens = int(usage_obj.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage_obj.get("completion_tokens", 0) or 0)
        return reply, prompt_tokens, completion_tokens

    # Parsed SDK object.
    reply = (r.choices[0].message.content or "").strip()
    prompt_tokens = 0
    completion_tokens = 0
    try:
        if r.usage:
            prompt_tokens = int(getattr(r.usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(r.usage, "completion_tokens", 0) or 0)
    except Exception:
        pass
    return reply, prompt_tokens, completion_tokens


async def openai_chat(messages: List[dict],
                      model: Optional[str] = None,
                      temperature: float = 0.0,
                      max_tokens: Optional[int] = None,
                      timeout_sec: float = 60.0) -> Tuple[str, int, int]:
    state = _reset_if_needed(_load_usage_state())
    if not _budget_ok(state):
        raise RuntimeError("Daily budget exceeded")

    payload = {
        "model": model or ACTIVE_MODEL,
        "messages": messages,
        "temperature": float(temperature),
        # Explicit non-stream. TAMU/Open WebUI may otherwise stream and the SDK
        # then hands back the raw body as a str instead of a parsed object.
        "stream": False,
    }
    if max_tokens is not None:
        if USE_TAMU:
            # TAMU (Open WebUI) expects the standard OpenAI max_tokens field.
            payload["max_tokens"] = int(max_tokens)
        else:
            # Newer OpenAI models (gpt-4o family onward) require
            # max_completion_tokens; legacy max_tokens returns 400 on them.
            payload["max_completion_tokens"] = int(max_tokens)

    try:
        r = await asyncio.wait_for(client_ai.chat.completions.create(**payload), timeout=timeout_sec)
        reply, prompt_tokens, completion_tokens = _parse_completion(r)

        st = _append_usage(_load_usage_state(), prompt_tokens, completion_tokens)
        if not _budget_ok(st):
            log.warning("Budget reached or exceeded after this request.")
        return reply, prompt_tokens, completion_tokens

    except asyncio.TimeoutError:
        raise RuntimeError("OpenAI request timeout")
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}")

# ========== Game profile (desc.json + ABBR_MAP.json) ==========
# Together these two files specialize this generic translator engine for one
# game. desc.json carries the human-readable facts; ABBR_MAP.json carries the
# in-game shorthand. Swap them out to retarget a different game.
DEFAULT_DESC = {
    "name": "",
    "short_name": "Translator",
    "genre": "",
    "tone": "natural tone",
    "preserve": "names, codes, @mentions, URLs, emojis, numbers, and times",
    "language_styles": {},
    "activity": "Translator",
}
try:
    with open(PROFILE_DIR / "desc.json", "r", encoding="utf-8") as f:
        _desc_data = json.load(f) or {}
    DESC = {**DEFAULT_DESC, **_desc_data}
    # language_styles needs a deep-merge so partial profiles still get defaults
    DESC["language_styles"] = {**DEFAULT_DESC["language_styles"], **(_desc_data.get("language_styles") or {})}
except FileNotFoundError:
    log.info("profile/desc.json not found; running as a generic translator.")
    DESC = dict(DEFAULT_DESC)
except Exception as _e:
    log.warning(f"desc.json not loaded, falling back to generic: {_e}")
    DESC = dict(DEFAULT_DESC)

try:
    with open(PROFILE_DIR / "ABBR_MAP.json", "r", encoding="utf-8") as f:
        _abbr_data = json.load(f); ABBR_MAP = _abbr_data.get("ABBR_MAP", {}) or {}
except Exception as _e:
    log.warning(f"ABBR_MAP.json not loaded, fallback to empty: {_e}")
    ABBR_MAP = {}
_ABBR_PATTERN = re.compile(r'(?<![\w/@])(' + '|'.join(map(re.escape, ABBR_MAP.keys())) + r')(?![\w/])') if ABBR_MAP else None
def expand_abbrs(text: str) -> str:
    if not text or not ABBR_MAP or not _ABBR_PATTERN: return text
    return _ABBR_PATTERN.sub(lambda m: ABBR_MAP[m.group(1)], text)

# Prompts
SYSTEM_DETECT = "Return the ISO 639-1 language code of the user text (e.g., en, ja, zh, ko, es). Output only the code."


def _build_system_translate(desc: dict, has_abbrs: bool) -> str:
    parts: List[str] = []
    name = (desc.get("name") or "").strip()
    genre = (desc.get("genre") or "").strip()
    if name:
        subject = f"the {genre} '{name}'" if genre else f"'{name}'"
        parts.append(f"You are a translator for {subject}.")
    else:
        parts.append("You are a translator.")
    tone = (desc.get("tone") or "natural tone").strip()
    parts.append(f"Translate the user's text into the specified target language with a {tone}.")
    preserve = (desc.get("preserve") or "").strip()
    if preserve:
        parts.append(f"Preserve {preserve}.")
    parts.append("Do NOT add explanations, parentheses, or any extra notes.")
    parts.append("If the input is already in the target language, return it verbatim.")
    if has_abbrs:
        parts.append("If acronyms were expanded before translation, translate them naturally; do not re-annotate.")
    return " ".join(parts)


SYSTEM_TRANSLATE = _build_system_translate(DESC, bool(ABBR_MAP))

# ========= Limits & regex for heuristics =========
EMBED_DESC_LIMIT = 4000
MSG_LIMIT = 1900

_HIRA_KATA = re.compile(r"[\u3040-\u30ff]")
_CJK = re.compile(r"[\u3400-\u9fff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")

def _count_pat(pat: re.Pattern, s: str) -> int: return len(pat.findall(s or ""))

def is_already_target_heuristic(text: str, target_code: Optional[str]) -> bool:
    t = _norm_lang(target_code)
    if not text or not t: return False
    if t == "ja":
        if _count_pat(_HIRA_KATA, text) >= 1: return True
    elif t in {"zh","zh-hant"}:
        cjk = _count_pat(_CJK, text)
        if cjk >= 8 and _count_pat(_HIRA_KATA, text) == 0 and _count_pat(_HANGUL, text) == 0: return True
    elif t == "ko":
        if _count_pat(_HANGUL, text) >= 2: return True
    elif t == "en":
        if english_like(text): return True
    return False

def split_long_text(s: str, limit: int = MSG_LIMIT) -> List[str]:
    s = s or ""
    if len(s) <= limit: return [s]
    chunks: List[str] = []; remain = s
    breakers = ["\n\n", "\n", "。", ". ", " "]
    while len(remain) > limit:
        cut = limit
        for br in breakers:
            i = remain.rfind(br, 0, limit)
            if i >= max(200, limit // 2): cut = i + len(br); break
        chunks.append(remain[:cut].rstrip()); remain = remain[cut:].lstrip()
    if remain: chunks.append(remain)
    return chunks

# ---------- 启发式检测 & 决策 ----------
def heuristic_detect(text: str) -> str:
    b = script_bucket(text)
    if b == "zh": return "zh"
    if b == "ja": return "ja"
    if b == "ko": return "ko"
    if b == "ar": return "ar"
    if english_like(text): return "en"
    return "en"

def need_llm_detect(text: str, target_code: str) -> bool:
    t = _norm_lang(target_code)
    b = script_bucket(text)
    if t in {"zh","zh-hant","ja","ko","ar"}:
        return False
    if t == "en" and b != "latin":
        return False
    return b == "latin"

# ========= Translation core =========
async def detect_lang(text: str) -> str:
    """
    先本地启发式；只有在拉丁脚本且需要精细区分时再调 OpenAI。
    """
    try:
        code_h = heuristic_detect(text)
        if script_bucket(text) != "latin":
            return code_h

        cleaned = _URL_ANY.sub("", text or "")[:500]
        if not cleaned.strip():
            return code_h

        messages = [
            {"role": "system", "content": SYSTEM_DETECT},
            {"role": "user", "content": cleaned}
        ]
        reply, _, _ = await openai_chat(
            messages=messages,
            model=ACTIVE_MODEL,
            temperature=0.0,
            max_tokens=6,
            timeout_sec=15.0
        )
        code = reply.strip().lower().replace(" ", "").replace(".", "")
        if not LANG_RE.match(code):
            code = {
                "english": "en", "japanese": "ja", "japan": "ja",
                "chinese": "zh", "korean": "ko", "spanish": "es",
                "french": "fr",  "arabic": "ar"
            }.get(code, code_h)
        return code or code_h
    except Exception as e:
        log.debug(f"detect_lang fallback: {e}")
        return heuristic_detect(text)

async def translate_to(text: str, target_code: str) -> str:
    expanded = expand_abbrs(text); tnorm = _norm_lang(target_code)
    lang_style = (DESC.get("language_styles") or {}).get(tnorm, "")
    force_rule = (f"Output MUST be in {LANG_NAME.get(tnorm, 'the target language')} only. "
                  f"Never return the source text. No brackets, no explanations.")
    try:
        msgs = [{"role": "system", "content": SYSTEM_TRANSLATE},
                {"role": "system", "content": force_rule}]
        if lang_style: msgs.append({"role": "system", "content": lang_style})
        msgs.append({"role": "user", "content": json.dumps({"target_lang": tnorm, "text": expanded}, ensure_ascii=False)})

        async with TRANSLATE_SEM:
            reply, _, _ = await openai_chat(messages=msgs, temperature=0.0, timeout_sec=60.0)
        out = reply.strip()

        # 如果模型偷懒回了原文，再强制一次
        if out.strip() == text.strip():
            strict_rule = (f"Translate into {LANG_NAME.get(tnorm, 'the target language')} ONLY. "
                           f"Return ONLY the translated text.")
            msgs2 = [{"role":"system","content":strict_rule}]
            if lang_style: msgs2.insert(0, {"role":"system","content":lang_style})
            msgs2.append({"role":"user","content":expanded})
            async with TRANSLATE_SEM:
                out2, _, _ = await openai_chat(messages=msgs2, temperature=0.0, timeout_sec=60.0)
            if out2: out = out2.strip()

        return out or text
    except Exception as e:
        log.exception(f"translate failed: {e}"); return text

async def safe_translate(text: str, target_code: Optional[str]) -> Tuple[str, bool]:
    if not text or not target_code: return text or "", False
    tnorm = _norm_lang(target_code)
    # Cheap, deterministic short-circuits before any OpenAI call.
    if is_already_target_heuristic(text, tnorm): return text, False
    if fast_lang_agree(text, tnorm): return text, False
    # If we don't need LLM-level detection (script bucket already disambiguates),
    # we can skip detect_lang entirely and go straight to translation.
    if need_llm_detect(text, tnorm):
        try:
            code = await detect_lang(text)
            if lang_similarity_pct(code, tnorm) >= 90: return text, False
        except Exception:
            return text, False
    parts = split_long_text(text, limit=1800); outs: List[str] = []; changed = False
    for p in parts:
        out = await translate_to(p, tnorm)
        if out.strip() != p.strip(): changed = True
        outs.append(out); await asyncio.sleep(0)
    return ("\n".join(outs)).strip(), changed

# ========== Embeds ==========
def author_avatar_url(u: discord.abc.User) -> Optional[str]:
    try:
        avatar = getattr(u, "display_avatar", None) or getattr(u, "avatar", None)
        if not avatar: return None
        try: return str(avatar.with_size(128).url)
        except Exception: pass
        try: return str(avatar.replace(size=128).url)
        except Exception: pass
        try: return str(avatar.url)
        except Exception: return None
    except Exception:
        return None

def make_embed_card(author: discord.abc.User, translated_text: str, footer_autodelete_seconds: Optional[int] = None) -> discord.Embed:
    emb = discord.Embed(description=translated_text or "")
    name = author.display_name if isinstance(author, discord.Member) else author.name
    icon = author_avatar_url(author)
    emb.set_author(name=name, icon_url=icon or None)
    if footer_autodelete_seconds:
        emb.set_footer(text=f"Auto-delete in {footer_autodelete_seconds}s")
    return emb

# ========== Translation log (Postgres) ==========
# The bot writes every translation it sends to translation_msg in Postgres.
# Editing a row's `text` (via psql, Dataclips, etc.) makes the watcher edit
# the corresponding Discord message — replacing the old CSV-edit workflow.
def append_translation_row(msg: discord.Message, src_msg_id: int, origin_channel_id: Optional[int],
                           author: discord.abc.User, target_lang: Optional[str],
                           is_embed: bool, seq: int, text: str):
    created_dt = getattr(msg, "created_at", None)
    try:
        created_at = (created_dt.astimezone(CST) if isinstance(created_dt, datetime)
                      else datetime.now(tz=CST))
    except Exception:
        created_at = datetime.now(tz=CST)

    _fire_db(db.insert_translation_row(
        msg_id=msg.id,
        channel_id=msg.channel.id if msg.channel else None,
        guild_id=msg.guild.id if msg.guild else None,
        src_msg_id=src_msg_id,
        origin_channel_id=origin_channel_id,
        author_id=getattr(author, "id", None),
        author_name=getattr(author, "display_name", getattr(author, "name", "")) or "",
        target_lang=_norm_lang(target_lang) if target_lang else None,
        is_embed=bool(is_embed),
        seq=int(seq),
        text=text or "",
        created_at=created_at,
    ))

translation_cache: Dict[int, Dict[str, str]] = {}
_translation_watcher_since: Optional[datetime] = None


async def watch_translation_db_and_apply_edits():
    """Poll translation_msg for rows whose text changed (via SQL edit) and
    apply the edit to the corresponding Discord message."""
    global translation_cache, _translation_watcher_since
    await bot.wait_until_ready()
    try:
        rows = await db.fetch_all_translations()
    except Exception as e:
        log.warning(f"initial translation load failed: {e}")
        rows = []
    translation_cache = {
        int(r["msg_id"]): {"text": r.get("text") or "", "is_embed": "1" if r.get("is_embed") else "0"}
        for r in rows
    }
    _translation_watcher_since = datetime.now(tz=timezone.utc)

    while not bot.is_closed():
        try:
            await asyncio.sleep(3)
            since = _translation_watcher_since
            if since is None:
                continue
            try:
                changed = await db.fetch_translations_changed_since(since)
            except Exception as e:
                log.warning(f"poll changed translations failed: {e}")
                continue
            if not changed:
                continue
            for row in changed:
                upd_at = row.get("updated_at")
                if isinstance(upd_at, datetime) and (_translation_watcher_since is None or upd_at > _translation_watcher_since):
                    _translation_watcher_since = upd_at
                mid = int(row["msg_id"])
                new_text = row.get("text") or ""
                is_embed = bool(row.get("is_embed"))
                prev = translation_cache.get(mid)
                if prev is not None and prev.get("text", "") == new_text:
                    continue
                channel_id = int(row.get("channel_id") or 0)
                if channel_id <= 0:
                    translation_cache[mid] = {"text": new_text, "is_embed": "1" if is_embed else "0"}
                    continue
                try:
                    channel = await bot.fetch_channel(channel_id)
                    if not isinstance(channel, discord.TextChannel):
                        continue
                    async with CHANNEL_LOCKS[channel.id]:
                        try:
                            msg = await channel.fetch_message(mid)
                        except (discord.NotFound, discord.Forbidden):
                            translation_cache[mid] = {"text": new_text, "is_embed": "1" if is_embed else "0"}
                            continue
                        if is_embed:
                            old = msg.embeds[0] if msg.embeds else None
                            desc = new_text[:EMBED_DESC_LIMIT]
                            if old:
                                new_emb = discord.Embed(description=desc)
                                if old.author and (old.author.name or old.author.icon_url):
                                    new_emb.set_author(name=old.author.name or "",
                                                       icon_url=old.author.icon_url or None)
                                if old.footer and old.footer.text:
                                    new_emb.set_footer(text=old.footer.text)
                            else:
                                new_emb = discord.Embed(description=desc)
                            await msg.edit(embed=new_emb)
                        else:
                            await msg.edit(content=new_text[:MSG_LIMIT])
                        translation_cache[mid] = {"text": new_text, "is_embed": "1" if is_embed else "0"}
                        await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning(f"apply edit for msg {mid} failed: {e}")
        except Exception as e:
            log.warning(f"translation db watcher loop err: {e}")

# ========= backfill into translation_msg =========
async def _existing_translation_ids() -> Set[int]:
    try:
        return await db.fetch_existing_translation_ids()
    except Exception as e:
        log.warning(f"_existing_translation_ids failed: {e}")
        return set()

async def _backfill_channel(channel: discord.TextChannel, *, days: Optional[int],
                            max_msgs: Optional[int], default_lang: Optional[str]) -> Tuple[int, int]:
    scanned = added = 0
    after_dt = None
    if days and days > 0:
        after_dt = datetime.now(timezone.utc) - timedelta(days=days)

    history_kwargs = {"limit": (max_msgs if (isinstance(max_msgs, int) and max_msgs > 0) else None)}
    if after_dt: history_kwargs["after"] = after_dt

    existing_ids: Set[int] = await _existing_translation_ids()

    try:
        async for m in channel.history(**history_kwargs):
            scanned += 1
            if not (m.author and bot.user and m.author.id == bot.user.id):
                continue
            if m.id in existing_ids:
                continue
            if m.embeds:
                e = m.embeds[0]
                text_out = (e.description or "").strip()
                is_embed = True
            else:
                text_out = (m.content or "").strip()
                is_embed = False
            if not text_out:
                continue
            append_translation_row(m, src_msg_id=0, origin_channel_id=None,
                                   author=m.author, target_lang=default_lang,
                                   is_embed=is_embed, seq=0, text=text_out)
            translation_cache[m.id] = {"text": text_out, "is_embed": "1" if is_embed else "0"}
            added += 1
            await asyncio.sleep(0)
    except discord.Forbidden:
        log.warning(f"backfill: no permission to read history in #{channel.id}")
    except Exception as e:
        log.warning(f"backfill error in #{channel.id}: {e}")
    return scanned, added

# ========== Slash: add / update / link / del / add_flag ==========
LANG_UI  = ["EN","JP","KR","CN","SP"]
UI2CODE  = {"EN":"en","JP":"ja","KR":"ko","CN":"zh","SP":"es"}

@bot.tree.command(name="add", description="Add a rule for a channel.")
@app_commands.describe(channel="Select a text channel", language="Target language", flag="Enable flag-emoji in-channel translation")
@app_commands.choices(language=[app_commands.Choice(name=ui, value=ui) for ui in LANG_UI])
async def slash_add(interaction: discord.Interaction, channel: discord.TextChannel, language: app_commands.Choice[str], flag: Optional[bool] = False):
    if not await ensure_admin(interaction): return
    r = get_rule(channel.id)
    code = UI2CODE[language.value]
    if r:
        if r.get("language") is None:
            r["language"] = code
            if flag is not None:
                r["flag"] = bool(flag)
            _persist_rule(channel.id)
            await interaction.response.send_message(
                f"✅ Set language for linked-only rule: <#{channel.id}> → `{language.value}` (`{code}`), flag={r['flag']}",
                ephemeral=True
            )
            return
        await interaction.response.send_message("⚠️ Rule already exists. Use `/update` instead.", ephemeral=True)
        return
    set_rule(channel.id, code, bool(flag))
    await interaction.response.send_message(
        f"✅ Added rule: <#{channel.id}> → `{language.value}` (`{code}`), flag={bool(flag)}",
        ephemeral=True
    )

@bot.tree.command(name="update", description="Update a channel rule.")
@app_commands.describe(channel="Select a text channel", language="New target language (optional)", flag="Enable/disable flag emoji (optional)")
@app_commands.choices(language=[app_commands.Choice(name=ui, value=ui) for ui in LANG_UI])
async def slash_update(interaction: discord.Interaction, channel: discord.TextChannel,
                       language: Optional[app_commands.Choice[str]] = None, flag: Optional[bool] = None):
    if not await ensure_admin(interaction): return
    r = get_rule(channel.id)
    if not r:
        await interaction.response.send_message("❌ No existing rule for this channel. Use `/add` first.", ephemeral=True)
        return
    code = UI2CODE[language.value] if language else None
    update_rule(channel.id, language=code, flag=flag)
    disp_lang = code if code is not None else (r.get("language") or "unset")
    disp_flag = flag if flag is not None else r.get("flag")
    await interaction.response.send_message(
        f"✅ Updated: <#{channel.id}> language={disp_lang}, flag={disp_flag}",
        ephemeral=True
    )

@bot.tree.command(name="link", description="Link two channels bidirectionally.")
@app_commands.describe(channel1="First channel", channel2="Second channel")
async def slash_link(interaction: discord.Interaction, channel1: discord.TextChannel, channel2: discord.TextChannel):
    if not await ensure_admin(interaction): return
    link_channels(channel1.id, channel2.id)
    await interaction.response.send_message(
        f"✅ Linked <#{channel1.id}> ↔ <#{channel2.id}>",
        ephemeral=True
    )

@bot.tree.command(name="del", description="Delete a channel rule.")
@app_commands.describe(channel="Channel to remove its rule")
async def slash_del(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await ensure_admin(interaction): return
    if not get_rule(channel.id):
        await interaction.response.send_message("ℹ️ No rule for this channel.", ephemeral=True)
        return
    del_rule(channel.id)
    await interaction.response.send_message(
        f"🗑️ Deleted rule for <#{channel.id}>",
        ephemeral=True
    )

@bot.tree.command(name="add_flag", description="Enable flag-emoji translation for a channel (language unchanged).")
@app_commands.describe(channel="Select a text channel")
async def slash_add_flag(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await ensure_admin(interaction): return
    r = get_rule(channel.id)
    if not r:
        set_rule(channel.id, language=None, flag=True)
        await interaction.response.send_message(
            f"✅ Enabled flag mode for <#{channel.id}> (language remains unset).",
            ephemeral=True
        )
        return
    update_rule(channel.id, flag=True)
    await interaction.response.send_message(
        f"✅ Enabled flag mode for <#{channel.id}>",
        ephemeral=True
    )

# ========== Slash: backfill / usage / help ==========
@bot.tree.command(name="backfill_csv", description="Backfill bot messages into translation_msg.csv")
@app_commands.describe(channel="(Optional) Only backfill this channel. If omitted, backfill all rule channels.",
                       days="(Optional) Only messages within the last N days (default: all).",
                       max_per_channel="(Optional) Max messages per channel to scan (default: ALL).")
async def slash_backfill_csv(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                             days: Optional[int] = None, max_per_channel: Optional[int] = None):
    if not await ensure_admin(interaction): return
    await interaction.response.send_message("⏳ Backfill started…", ephemeral=True)

    targets: List[discord.TextChannel] = []
    if channel:
        targets = [channel]
    else:
        if not interaction.guild:
            await interaction.followup.send("No guild context.", ephemeral=True); return
        for cid, _r in (rules or {}).items():
            ch = interaction.guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel): targets.append(ch)
        if not targets and isinstance(interaction.channel, discord.TextChannel):
            targets = [interaction.channel]

    total_scanned = total_added = 0; per_channel_stats: List[str] = []
    for ch in targets:
        r = get_rule(ch.id) or {}
        default_lang = r.get("language")
        scanned, added = await _backfill_channel(ch, days=days, max_msgs=max_per_channel, default_lang=default_lang)
        total_scanned += scanned; total_added += added
        per_channel_stats.append(f"<#{ch.id}> scanned={scanned}, added={added}")

    msg = ("✅ **Backfill finished**\n"
           f"- Channels: {len(targets)}\n"
           f"- Scanned: {total_scanned}\n"
           f"- New rows added: {total_added}\n"
           "Details:\n" + "\n".join(per_channel_stats[:20]) + ("\n... (truncated)" if len(per_channel_stats) > 20 else ""))
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="usage", description="Show today's OpenAI usage; resets 19:00 CST")
async def slash_usage(interaction: discord.Interaction):
    state = _reset_if_needed(_load_usage_state())
    remain = max(0.0, (BUDGET_DOLLARS_PER_DAY - state.get("total_cost", 0.0))) if BUDGET_DOLLARS_PER_DAY > 0 else float("inf")
    used_tokens = state.get("total_tokens", 0)
    prompt_toks = state.get("prompt_tokens", 0)
    comp_toks   = state.get("completion_tokens", 0)
    msg = (
        f"**{PROVIDER} Usage (window resets 19:00 CST)**\n"
        f"- Date: `{_window_label(_now_cst())}`\n"
        f"- Model: `{ACTIVE_MODEL}`\n"
        f"- Tokens: `{used_tokens}` (prompt `{prompt_toks}`, completion `{comp_toks}`)\n"
        f"- Est. Cost: `${state.get('total_cost', 0.0):.4f}` "
        f"(input `${state.get('input_cost', 0.0):.4f}`, output `${state.get('output_cost', 0.0):.4f}`)\n"
        f"- Daily budget: `${BUDGET_DOLLARS_PER_DAY:.2f}` → remaining `${remain:.4f}`\n"
        if BUDGET_DOLLARS_PER_DAY > 0 else
        (f"- Daily budget disabled (OPENAI_BUDGET_DOLLARS_PER_DAY <= 0)\n")
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="help", description="Show bot commands and notes")
async def slash_help(interaction: discord.Interaction):
    text = (
        "**Pie’s_translator — Commands & Usage**\n"
        "• `/add channel:<#channel> language:<EN|JP|KR|CN|SP> [flag:<True|False>]` — set target language for a channel\n"
        "• `/update channel:<#channel> [language:<...>] [flag:<True|False>]` — update a rule\n"
        "• `/link channel1:<#a> channel2:<#b>` — cross-channel relay\n"
        "• `/del channel:<#channel>` — delete a rule\n"
        "• `/add_flag channel:<#channel>` — enable flag-emoji translation only (language unchanged)\n"
        "• `/usage` — show today's OpenAI usage\n"
        "• `/backfill_csv [channel] [days] [max_per_channel]` — backfill historical bot messages into the translation log\n"
        "• `/syn_his channel:<#dest> [max_count] [days] [skip_existing]` — sync history from linked channel\n"
        "• `/correct [size:<50|100|200>]` — scan last N log rows; fix texts not in target language\n"
        "\n**Live edit:** update `translation_msg.text` in Postgres → the bot edits the matching Discord message.\n"
        "Notes:\n"
        "• Translation history lives in the `translation_msg` table.\n"
        "• `created_at` is the REAL message creation time in UTC.\n"
    )
    await interaction.response.send_message(text, ephemeral=True)

# ========== 安全交互回复（避免 10062） ==========
async def safe_reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    except Exception:
        pass
    try:
        await interaction.followup.send(content, ephemeral=ephemeral)
    except discord.NotFound:
        log.warning("safe_reply: interaction expired (404).")
    except Exception as e:
        log.warning(f"safe_reply failed: {e}")

# ========== Slash: syn_his ==========
@bot.tree.command(
    name="syn_his",
    description="Sync recent messages from linked channel, translating to target language and preserving replies."
)
@app_commands.describe(
    channel="Destination channel (must have a linked partner)",
    max_count="How many recent messages to sync (default: 50, max: 500)",
    days="Only sync messages within the last N days (optional)",
    skip_existing="Skip messages already synced before (default: True)"
)
async def slash_syn_his(interaction: discord.Interaction,
    channel: discord.TextChannel,
    max_count: Optional[int] = 50,
    days: Optional[int] = None,
    skip_existing: bool = True
):
    if not await ensure_admin(interaction):
        return

    rule_dest = get_rule(channel.id) or {}
    target_lang = rule_dest.get("language")
    linked_id = rule_dest.get("link_channel_id")

    if not linked_id:
        await interaction.response.send_message(f"❌ <#{channel.id}> 没有关联的频道，请先用 `/link`。", ephemeral=True)
        return

    guild = interaction.guild
    src_ch = guild.get_channel(int(linked_id)) if guild else None
    if not isinstance(src_ch, discord.TextChannel):
        await interaction.response.send_message("❌ 找不到对应频道或类型错误。", ephemeral=True)
        return

    if max_count is not None:
        max_count = max(1, min(int(max_count), 500))
    await interaction.response.send_message(
        f"⏳ 正在从 <#{src_ch.id}> 同步到 <#{channel.id}>（最多 {max_count or 'ALL'} 条，{'仅最近 '+str(days)+' 天' if days else '不限时间'}）…",
        ephemeral=True
    )

    after_dt = None
    if days and days > 0:
        after_dt = datetime.now(timezone.utc) - timedelta(days=days)

    scanned = sent_ok = skipped = 0
    failed: List[int] = []
    msgs: List[discord.Message] = []
    history_kwargs = {}
    if max_count:
        history_kwargs["limit"] = max_count
    if after_dt:
        history_kwargs["after"] = after_dt

    try:
        async for m in src_ch.history(**history_kwargs):
            msgs.append(m)
    except discord.Forbidden:
        await interaction.followup.send(f"❌ 无法读取 <#{src_ch.id}> 历史消息：缺少 **Read Message History** 权限。", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(f"❌ 拉取历史失败：{e}", ephemeral=True)
        return

    # msgs.reverse()  # old → new
    msgs.sort(key=lambda x: x.created_at)

    for m in msgs:
        scanned += 1
        if m.author and m.author.bot:
            continue
        if skip_existing and map_get(m.id, channel.id):
            skipped += 1
            continue

        content = (m.content or "").strip()
        media_present = has_media(m)
        if not content and not media_present:
            continue

        try:
            translated = content
            if target_lang and content:
                translated, _ = await safe_translate(content, target_lang)
        except RuntimeError:
            translated = content or ""
        except Exception:
            translated = content or ""

        files = await build_files_from_attachments(m)
        try:
            reply_ref = None
            if m.reference and isinstance(m.reference, discord.MessageReference):
                try:
                    ref_src_id = None
                    if m.reference.resolved and isinstance(m.reference.resolved, discord.Message):
                        ref_src_id = m.reference.resolved.id
                    elif m.reference.message_id:
                        ref_src_id = m.reference.message_id

                    target_mid = None
                    if ref_src_id:
                        target_mid = map_get(ref_src_id, channel.id)
                        if not target_mid:
                            rev = reverse_get(ref_src_id)
                            if rev:
                                real_src_msg_id, _real_src_ch_id = rev
                                target_mid = map_get(real_src_msg_id, channel.id)
                    if target_mid:
                        reply_ref = discord.MessageReference(message_id=target_mid, channel_id=channel.id)
                except Exception as e:
                    if DEBUG_MODE: log.debug(f"linked reply ref build failed: {e}")

            async with CHANNEL_LOCKS[channel.id]:
                if len(translated) <= EMBED_DESC_LIMIT:
                    emb = make_embed_card(m.author, translated)
                    try:
                        emb.timestamp = m.created_at
                    except Exception:
                        pass
                    emb.set_footer(text=f"From #{src_ch.name}")
                    sent = await channel.send(embed=emb, files=files if files else None,
                                              allowed_mentions=NO_MENTIONS, reference=reply_ref)
                    our_message_ids.add(sent.id)
                    map_set(m.id, channel.id, sent.id)
                    origin_set(sent.id, src_ch.id)
                    reverse_set(sent.id, m.id, src_ch.id)
                    append_translation_row(sent, src_msg_id=m.id, origin_channel_id=src_ch.id,
                                           author=m.author, target_lang=target_lang,
                                           is_embed=True, seq=0, text=translated)
                    translation_cache[sent.id] = {"text": translated, "is_embed": "1"}
                    sent_ok += 1
                else:
                    chunks = split_long_text(translated, MSG_LIMIT)
                    emb = make_embed_card(m.author, chunks[0])
                    emb.timestamp = m.created_at
                    emb.set_footer(text=f"From #{src_ch.name}")
                    m0 = await channel.send(embed=emb, files=files if files else None,
                                            allowed_mentions=NO_MENTIONS, reference=reply_ref)
                    our_message_ids.add(m0.id)
                    map_set(m.id, channel.id, m0.id)
                    origin_set(m0.id, src_ch.id)
                    reverse_set(m0.id, m.id, src_ch.id)
                    append_translation_row(m0, src_msg_id=m.id, origin_channel_id=src_ch.id,
                                           author=m.author, target_lang=target_lang,
                                           is_embed=True, seq=0, text=chunks[0])
                    translation_cache[m0.id] = {"text": chunks[0], "is_embed": "1"}
                    for i, c in enumerate(chunks[1:], start=1):
                        tmsg = await channel.send(c, allowed_mentions=NO_MENTIONS)
                        our_message_ids.add(tmsg.id)
                        append_translation_row(tmsg, src_msg_id=m.id, origin_channel_id=src_ch.id,
                                               author=m.author, target_lang=target_lang,
                                               is_embed=False, seq=i, text=c)
                        translation_cache[tmsg.id] = {"text": c, "is_embed": "0"}
                    sent_ok += 1
        except Exception as e:
            failed.append(m.id)
            log.warning(f"/syn_his send failed for msg {m.id}: {e}")
        await asyncio.sleep(0)

    summary = (
        f"✅ 同步完成：从 <#{src_ch.id}> → 到 <#{channel.id}>\n"
        f"- 扫描: {scanned}\n"
        f"- 发送: {sent_ok}\n"
        f"- 跳过已存在: {skipped}\n"
        f"- 失败: {len(failed)}"
        + (f"\n失败示例: {failed[:5]}" if failed else "")
    )
    await interaction.followup.send(summary, ephemeral=True)

# 其余基础管理（权限校验）
async def get_member_from_interaction(inter: discord.Interaction) -> Optional[discord.Member]:
    if inter.guild is None: return None
    if isinstance(inter.user, discord.Member): return inter.user
    m = inter.guild.get_member(inter.user.id)
    if m: return m
    try: return await inter.guild.fetch_member(inter.user.id)
    except Exception: return None

def has_admin_perms(m: discord.Member) -> bool:
    gp = m.guild_permissions
    return any([gp.administrator, gp.manage_guild, gp.manage_channels, gp.manage_roles, gp.manage_messages,
                (m.guild and m.guild.owner_id == m.id)])

async def ensure_admin(inter: discord.Interaction) -> bool:
    m = await get_member_from_interaction(inter)
    if not m or not has_admin_perms(m):
        await inter.response.send_message(
            "❌ Admin only.\n(Require one of: Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages, or be the Server Owner.)",
            ephemeral=True
        ); return False
    return True

# ========== Emoji → Language ==========
EMOJI2LANG: Dict[str, str] = {
    "🇺🇸":"en","🇬🇧":"en","🇨🇦":"en","🇦🇺":"en","🇳🇿":"en","🇮🇪":"en",
    "🇯🇵":"ja","🇰🇷":"ko","🇨🇳":"zh","🇸🇬":"zh",
    "🇹🇼":"zh-hant","🇭🇰":"zh-hant","🇲🇴":"zh-hant",
    "🇪🇸":"es","🇲🇽":"es","🇦🇷":"es","🇨🇴":"es","🇨🇱":"es","🇵🇪":"es","🇻🇪":"es",
    "🇺🇾":"es","🇵🇾":"es","🇧🇴":"es","🇩🇴":"es","🇪🇨":"es","🇵🇷":"es","🇬🇹":"es",
    "🇭🇳":"es","🇨🇷":"es","🇵🇦":"es","🇳🇮":"es","🇸🇻":"es","🇨🇺":"es",
    "🇫🇷":"fr","🇧🇪":"fr","🇨🇭":"fr","🇨🇦":"fr","🇱🇺":"fr","🇲🇨":"fr","🇨🇩":"fr","🇨🇲":"fr","🇸🇳":"fr","🇨🇮":"fr",
    "🇸🇦":"ar","🇦🇪":"ar","🇶🇦":"ar","🇰🇼":"ar","🇧🇭":"ar","🇴🇲":"ar",
    "🇪🇬":"ar","🇲🇦":"ar","🇩🇿":"ar","🇹🇳":"ar","🇱🇧":"ar","🇮🇶":"ar","🇵🇸":"ar","🇾🇪":"ar","🇸🇩":"ar","🇸🇴":"ar",
}

# ========== helpers for media ==========
async def build_files_from_attachments(msg: discord.Message) -> List[discord.File]:
    files: List[discord.File] = []
    for a in msg.attachments:
        try:
            f = await a.to_file(spoiler=a.is_spoiler()); files.append(f)
        except Exception as e:
            log.warning(f"attachment to_file failed: {e}")
    return files

def has_media(msg: discord.Message) -> bool: return bool(msg.attachments or msg.stickers)

# ========== Slash: correct ==========
@bot.tree.command(
    name="correct",
    description="Admin: scan last N rows in CSV; fix texts not in target language."
)
@app_commands.describe(size="How many recent rows to check (default 50)")
@app_commands.choices(
    size=[
        app_commands.Choice(name="50",  value=50),
        app_commands.Choice(name="100", value=100),
        app_commands.Choice(name="200", value=200),
    ]
)
async def slash_correct(interaction: discord.Interaction, size: app_commands.Choice[int] = None):
    if not await ensure_admin(interaction):
        return

    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass

    window_n = int(size.value) if size is not None else 50
    try:
        rows = await db.fetch_recent_translations(window_n)
    except Exception as e:
        await safe_reply(interaction, f"❌ Failed to read DB: {e}")
        return

    if not rows:
        await safe_reply(interaction, "ℹ️ No translations recorded yet.")
        return

    updated = 0
    checked = len(rows)
    errors: List[str] = []

    for row in rows:
        try:
            text = (row.get("text") or "").strip()
            tgt  = _norm_lang(row.get("target_lang") or "")
            if not text or not tgt:
                continue

            if fast_lang_agree(text, tgt) or is_already_target_heuristic(text, tgt):
                continue

            if not need_llm_detect(text, tgt):
                new_text = await translate_to(text, tgt)
            else:
                src_code = await detect_lang(text)
                if lang_similarity_pct(src_code, tgt) >= 90:
                    continue
                new_text = await translate_to(text, tgt)

            if not new_text or new_text.strip() == text:
                continue

            try:
                await db.update_translation_text(int(row["msg_id"]), new_text)
                updated += 1
            except Exception as e:
                errors.append(str(e))
        except Exception as e:
            errors.append(str(e))
            continue
        await asyncio.sleep(0)

    msg = f"✅ /correct done. Checked: {checked}, Fixed: {updated}"
    if errors:
        msg += f"\n⚠️ Errors on {len(errors)} rows (first): {errors[:3]}"
    await safe_reply(interaction, msg)

# ========== Core ==========
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        if message.id not in our_message_ids:
            return
    if not isinstance(message.channel, discord.TextChannel): return
    if message.id in processed_ids: return
    processed_ids.add(message.id)

    rule = get_rule(message.channel.id)
    if not rule: return

    if not message.author.bot:
        bump(message.author)

    lang_here: Optional[str] = rule.get("language")
    link_id = rule.get("link_channel_id")

    content = (message.content or "").strip()
    media_present = has_media(message)
    if not content and not media_present: return

    tasks: List[asyncio.Task] = []

    # 同频道自动翻译（回复同频道）
    if (not message.author.bot) and lang_here and content and text_is_meaningful(content):
        async def reply_here():
            try:
                out, changed = await safe_translate(content, lang_here)
            except RuntimeError:
                await message.channel.send("⚠️ Daily translation budget reached. Try again after 19:00 CST.", delete_after=15)
                return
            if not changed: return
            if len(out) <= EMBED_DESC_LIMIT:
                emb = make_embed_card(message.author, out)
                async with CHANNEL_LOCKS[message.channel.id]:
                    m = await (message.reply(embed=emb, allowed_mentions=NO_MENTIONS)
                               if message.reference and isinstance(message.reference, discord.MessageReference)
                               else message.channel.send(embed=emb, allowed_mentions=NO_MENTIONS))
                    our_message_ids.add(m.id); map_set(message.id, message.channel.id, m.id)
                    append_translation_row(m, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                           author=message.author, target_lang=lang_here,
                                           is_embed=True, seq=0, text=out)
                    translation_cache[m.id] = {"text": out, "is_embed": "1"}
            else:
                head = f"**{message.author.display_name if isinstance(message.author, discord.Member) else message.author.name}:**"
                chunks = split_long_text(out, MSG_LIMIT)
                async with CHANNEL_LOCKS[message.channel.id]:
                    first = await message.channel.send(f"{head}\n{chunks[0]}", allowed_mentions=NO_MENTIONS)
                    our_message_ids.add(first.id); map_set(message.id, message.channel.id, first.id)
                    append_translation_row(first, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                           author=message.author, target_lang=lang_here,
                                           is_embed=False, seq=0, text=f"{head}\n{chunks[0]}")
                    translation_cache[first.id] = {"text": f"{head}\n{chunks[0]}", "is_embed": "0"}
                    for i, c in enumerate(chunks[1:], start=1):
                        m = await message.channel.send(c, allowed_mentions=NO_MENTIONS)
                        our_message_ids.add(m.id)
                        append_translation_row(m, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                               author=message.author, target_lang=lang_here,
                                               is_embed=False, seq=i, text=c)
                        translation_cache[m.id] = {"text": c, "is_embed": "0"}
        tasks.append(asyncio.create_task(reply_here()))

    # 跨频道转发
    if link_id:
        target_ch = message.guild.get_channel(int(link_id))
        if isinstance(target_ch, discord.TextChannel):
            if message.author.bot:
                origin_ch = origin_get(message.id)
                if origin_ch is not None and int(origin_ch) == int(link_id): return
            other_rule = get_rule(int(link_id)) or {}; other_lang: Optional[str] = other_rule.get("language")
            if other_lang or media_present or content:
                async def send_linked():
                    try:
                        translated, _ = await safe_translate(content, other_lang) if (other_lang and content) else (content or "", False)
                    except RuntimeError:
                        translated = content or ""
                    files = await build_files_from_attachments(message)

                    reply_ref = None
                    if message.reference and isinstance(message.reference, discord.MessageReference):
                        try:
                            ref_src_id = None
                            if message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                                ref_src_id = message.reference.resolved.id
                            elif message.reference.message_id:
                                ref_src_id = message.reference.message_id

                            target_mid = None
                            if ref_src_id:
                                target_mid = map_get(ref_src_id, target_ch.id)
                                if not target_mid:
                                    rev = reverse_get(ref_src_id)
                                    if rev:
                                        real_src_msg_id, _real_src_ch_id = rev
                                        target_mid = map_get(real_src_msg_id, target_ch.id)

                            if target_mid:
                                reply_ref = discord.MessageReference(message_id=target_mid, channel_id=target_ch.id)
                        except Exception as e:
                            if DEBUG_MODE: log.debug(f"linked reply ref build failed: {e}")

                    async with CHANNEL_LOCKS[target_ch.id]:
                        try:
                            if len(translated) <= EMBED_DESC_LIMIT:
                                emb = make_embed_card(message.author, translated)
                                m = await target_ch.send(embed=emb, files=files if files else None,
                                                         allowed_mentions=NO_MENTIONS, reference=reply_ref)
                                our_message_ids.add(m.id)
                                map_set(message.id, target_ch.id, m.id)
                                origin_set(m.id, message.channel.id)
                                reverse_set(m.id, message.id, message.channel.id)
                                append_translation_row(m, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                                       author=message.author, target_lang=other_lang,
                                                       is_embed=True, seq=0, text=translated)
                                translation_cache[m.id] = {"text": translated, "is_embed": "1"}
                            else:
                                chunks = split_long_text(translated, MSG_LIMIT)
                                emb = make_embed_card(message.author, chunks[0])
                                m0 = await target_ch.send(embed=emb, files=files if files else None,
                                                          allowed_mentions=NO_MENTIONS, reference=reply_ref)
                                our_message_ids.add(m0.id)
                                map_set(message.id, target_ch.id, m0.id)
                                origin_set(m0.id, message.channel.id)
                                reverse_set(m0.id, message.id, message.channel.id)
                                append_translation_row(m0, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                                       author=message.author, target_lang=other_lang,
                                                       is_embed=True, seq=0, text=chunks[0])
                                translation_cache[m0.id] = {"text": chunks[0], "is_embed": "1"}
                                for i, c in enumerate(chunks[1:], start=1):
                                    m = await target_ch.send(c, allowed_mentions=NO_MENTIONS)
                                    our_message_ids.add(m.id)
                                    append_translation_row(m, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                                           author=message.author, target_lang=other_lang,
                                                           is_embed=False, seq=i, text=c)
                                    translation_cache[m.id] = {"text": c, "is_embed": "0"}
                        except Exception as e:
                            log.warning(f"relay send failed (embed/files). Retrying plain text: {e}")
                            for i, c in enumerate(split_long_text(translated, MSG_LIMIT)):
                                prefix = f"**{message.author.display_name if isinstance(message.author, discord.Member) else message.author.name}:**\n" if i == 0 else ""
                                m = await target_ch.send(prefix + c, allowed_mentions=NO_MENTIONS, reference=reply_ref if i == 0 else None)
                                our_message_ids.add(m.id)
                                if i == 0:
                                    map_set(message.id, target_ch.id, m.id)
                                    origin_set(m.id, message.channel.id)
                                    reverse_set(m.id, message.id, message.channel.id)
                                append_translation_row(m, src_msg_id=message.id, origin_channel_id=message.channel.id,
                                                       author=message.author, target_lang=other_lang,
                                                       is_embed=False, seq=i, text=(prefix + c if i == 0 else c))
                                translation_cache[m.id] = {"text": (prefix + c if i == 0 else c), "is_embed": "0"}
                tasks.append(asyncio.create_task(send_linked()))
    if tasks: await asyncio.gather(*tasks)

# ========== Flag Emoji ==========
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id: return
    ch_id = payload.channel_id; r = get_rule(ch_id)
    if not r or not r.get("flag"): return
    if payload.emoji.id is not None: return
    emoji = payload.emoji.name; target_code = EMOJI2LANG.get(emoji)
    if not target_code: return
    try:
        channel = await bot.fetch_channel(ch_id)
        if not isinstance(channel, discord.TextChannel): return
        msg = await channel.fetch_message(payload.message_id)
    except discord.Forbidden:
        try:
            await channel.send(f"<@{payload.user_id}> ⚠️ I need **Read Message History** permission in this channel to translate reactions.",
                               allowed_mentions=MENTION_USER, delete_after=10)
        except Exception: pass
        return
    except Exception:
        return

    if bot.user and msg.author.id == bot.user.id:
        if any((f"auto-delete in {FLAG_EPHEMERAL_SECONDS}" in (msg.content or ""),)): return

    text = (msg.content or "").strip()
    if not text and not has_media(msg): return
    try:
        user = await bot.fetch_user(payload.user_id); bump(user)
    except Exception:
        pass

    try:
        translated, _ = await safe_translate(text, target_code) if text else ("", False)
    except RuntimeError:
        await channel.send("⚠️ Daily translation budget reached. Try again after 19:00 CST.", delete_after=10); return

    files = await build_files_from_attachments(msg)
    try:
        if translated and len(translated) > EMBED_DESC_LIMIT:
            chunks = split_long_text(translated, MSG_LIMIT)
            emb = make_embed_card(msg.author, chunks[0], footer_autodelete_seconds=FLAG_EPHEMERAL_SECONDS)
            async with CHANNEL_LOCKS[msg.channel.id]:
                sent0 = await msg.reply(embed=emb, files=files if files else None,
                                        mention_author=False, allowed_mentions=NO_MENTIONS)
                our_message_ids.add(sent0.id); origin_set(sent0.id, msg.channel.id)
                append_translation_row(sent0, src_msg_id=msg.id, origin_channel_id=msg.channel.id,
                                       author=msg.author, target_lang=target_code,
                                       is_embed=True, seq=0, text=chunks[0])
                translation_cache[sent0.id] = {"text": chunks[0], "is_embed": "1"}
                for i, c in enumerate(chunks[1:], start=1):
                    s = await msg.channel.send(c, allowed_mentions=NO_MENTIONS)
                    our_message_ids.add(s.id)
                    append_translation_row(s, src_msg_id=msg.id, origin_channel_id=msg.channel.id,
                                           author=msg.author, target_lang=target_code,
                                           is_embed=False, seq=i, text=c)
                    translation_cache[s.id] = {"text": c, "is_embed": "0"}
            async def _auto_delete(m: discord.Message):
                try: await asyncio.sleep(FLAG_EPHEMERAL_SECONDS); await m.delete()
                except (discord.Forbidden, Exception): pass
            asyncio.create_task(_auto_delete(sent0))
        else:
            emb = make_embed_card(msg.author, translated, footer_autodelete_seconds=FLAG_EPHEMERAL_SECONDS)
            async with CHANNEL_LOCKS[msg.channel.id]:
                sent = await msg.reply(embed=emb, files=files if files else None,
                                       mention_author=False, allowed_mentions=NO_MENTIONS)
            our_message_ids.add(sent.id); origin_set(sent.id, msg.channel.id)
            append_translation_row(sent, src_msg_id=msg.id, origin_channel_id=msg.channel.id,
                                   author=msg.author, target_lang=target_code,
                                   is_embed=True, seq=0, text=translated)
            translation_cache[sent.id] = {"text": translated, "is_embed": "1"}
            async def _auto_delete(m: discord.Message):
                try: await asyncio.sleep(FLAG_EPHEMERAL_SECONDS); await m.delete()
                except (discord.Forbidden, Exception): pass
            asyncio.create_task(_auto_delete(sent))
    except discord.Forbidden:
        log.warning("Forbidden to send flag-translation message in this channel.")
    except Exception as e:
        log.exception(f"flag translation send failed: {e}")

# ========== /sync ==========
@bot.tree.command(name="sync", description="Admin: copy globals and sync commands for this server")
async def slash_sync(interaction: discord.Interaction):
    if not await ensure_admin(interaction):
        return
    try:
        if interaction.guild:
            bot.tree.copy_global_to(guild=interaction.guild)
            cmds = await bot.tree.sync(guild=interaction.guild)
            await interaction.response.send_message(
                f"✅ Copied & synced {len(cmds)} commands to this server.", ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Not in a guild.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sync failed: {e}", ephemeral=True)

# ========== Ready ==========
@bot.event
async def on_ready():
    await load_rules()
    await load_usage()
    await load_relay_map()
    await load_relay_origin()
    await load_reverse_relay()
    await load_usage_state_from_db()
    asyncio.create_task(watch_translation_db_and_apply_edits())
    _reset_if_needed(_load_usage_state())

    try:
        await bot.tree.sync()
        for g in bot.guilds:
            bot.tree.copy_global_to(guild=g)
            cmds = await bot.tree.sync(guild=g)
            log.info(f"Synced {len(cmds)} commands to guild {g.id}")
        log.info(f"Slash commands synced on_ready to {len(bot.guilds)} guild(s).")
    except Exception as e:
        log.warning(f"on_ready sync failed: {e}")

    BOT_VERSION = "OPENAI-syn_his-verify-2025-12-30"
    try:
        from pathlib import Path as _P
        _running_file = _P(__file__).resolve()
    except Exception:
        _running_file = __file__
    log.info(f"=== PieTrans BOT_VERSION = {BOT_VERSION} ===")
    log.info(f"=== Running file: {_running_file} ===")
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")
    log.info("Translation log: Postgres table `translation_msg`")
    log.info(f"Translation provider: {PROVIDER} | model: {ACTIVE_MODEL}")
    if DEBUG_MODE: log.debug("DEBUG MODE is ON")
    activity = discord.Game(DESC.get("activity") or DESC.get("short_name") or "Translator")
    await bot.change_presence(status=discord.Status.online, activity=activity)

# ========== Main ==========
async def _async_main():
    await db.init_pool(DATABASE_URL)
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        try:
            await db.close_pool()
        except Exception as e:
            log.warning(f"close_pool failed: {e}")


def main():
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
