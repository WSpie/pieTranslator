# db.py
# -*- coding: utf-8 -*-
"""
Postgres-backed persistence for Pie's Translator.

Replaces the file-based state in data/* (built_rules.json, relay_map.json,
relay_origin.json, relay_reverse.json, usage_state.json, usage.log,
user_query_hist.csv, translation_msg.csv) so the bot can run on Heroku where
the dyno filesystem is ephemeral.

All functions are async. The main module keeps in-memory caches for hot reads
and writes through to the DB on each mutation.
"""

import os
import ssl
import logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any

import asyncpg

log = logging.getLogger("PieTransDB")

_pool: Optional[asyncpg.Pool] = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rules (
    channel_id BIGINT PRIMARY KEY,
    language TEXT,
    flag BOOLEAN NOT NULL DEFAULT FALSE,
    link_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS relay_map (
    src_msg_id BIGINT NOT NULL,
    target_channel_id BIGINT NOT NULL,
    target_msg_id BIGINT NOT NULL,
    PRIMARY KEY (src_msg_id, target_channel_id)
);
CREATE INDEX IF NOT EXISTS idx_relay_map_target_msg ON relay_map(target_msg_id);

CREATE TABLE IF NOT EXISTS relay_origin (
    relayed_msg_id BIGINT PRIMARY KEY,
    origin_channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_reverse (
    relayed_msg_id BIGINT PRIMARY KEY,
    src_msg_id BIGINT NOT NULL,
    src_channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    window_start TEXT,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    input_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    output_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
    CONSTRAINT usage_state_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id BIGSERIAL PRIMARY KEY,
    window_label TEXT NOT NULL,
    tokens_used BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_usage (
    username TEXT PRIMARY KEY,
    query_counts BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS translation_msg (
    msg_id BIGINT PRIMARY KEY,
    channel_id BIGINT,
    guild_id BIGINT,
    src_msg_id BIGINT,
    origin_channel_id BIGINT,
    author_id BIGINT,
    author_name TEXT,
    target_lang TEXT,
    is_embed BOOLEAN NOT NULL DEFAULT TRUE,
    seq INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_translation_msg_channel ON translation_msg(channel_id);
CREATE INDEX IF NOT EXISTS idx_translation_msg_updated ON translation_msg(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_translation_msg_created ON translation_msg(created_at DESC);

CREATE OR REPLACE FUNCTION pies_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_translation_msg_updated ON translation_msg;
CREATE TRIGGER trg_translation_msg_updated
BEFORE UPDATE ON translation_msg
FOR EACH ROW EXECUTE FUNCTION pies_set_updated_at();
"""


def _normalize_url(url: str) -> str:
    # Heroku historically gives postgres://; asyncpg accepts it but
    # normalize to postgresql:// to be explicit.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _needs_ssl(url: str) -> bool:
    if os.getenv("DYNO"):
        return True
    if "sslmode=disable" in url:
        return False
    host_markers = ("amazonaws.com", "render.com", "digitalocean.com", "supabase.co", "neon.tech")
    return any(m in url for m in host_markers)


async def init_pool(database_url: str, *, min_size: int = 1, max_size: int = 5) -> None:
    global _pool
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    url = _normalize_url(database_url)

    ssl_ctx: Any = None
    if _needs_ssl(url):
        ctx = ssl.create_default_context()
        # Heroku Postgres certs aren't always in the system trust store; soften
        # verification to match the documented Heroku connection style.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ssl_ctx = ctx

    _pool = await asyncpg.create_pool(
        dsn=url,
        min_size=min_size,
        max_size=max_size,
        ssl=ssl_ctx,
        command_timeout=30,
    )
    await create_schema()
    log.info("Postgres pool initialized (size %d-%d).", min_size, max_size)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def create_schema() -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first.")
    return _pool


# ============ rules ============

async def load_rules() -> Dict[str, Dict[str, Any]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_id, language, flag, link_channel_id FROM rules"
        )
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[str(r["channel_id"])] = {
            "language": r["language"],
            "flag": bool(r["flag"]),
            "link_channel_id": int(r["link_channel_id"]) if r["link_channel_id"] is not None else None,
        }
    return out


async def upsert_rule(channel_id: int, language: Optional[str], flag: bool,
                      link_channel_id: Optional[int]) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO rules (channel_id, language, flag, link_channel_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (channel_id) DO UPDATE
              SET language = EXCLUDED.language,
                  flag = EXCLUDED.flag,
                  link_channel_id = EXCLUDED.link_channel_id
            """,
            int(channel_id), language, bool(flag),
            int(link_channel_id) if link_channel_id is not None else None,
        )


async def delete_rule(channel_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM rules WHERE channel_id = $1", int(channel_id))


# ============ relay_map ============

async def load_relay_map() -> Dict[str, Dict[str, int]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT src_msg_id, target_channel_id, target_msg_id FROM relay_map"
        )
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        out.setdefault(str(r["src_msg_id"]), {})[str(r["target_channel_id"])] = int(r["target_msg_id"])
    return out


async def upsert_relay_map(src_msg_id: int, target_channel_id: int, target_msg_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO relay_map (src_msg_id, target_channel_id, target_msg_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (src_msg_id, target_channel_id) DO UPDATE
              SET target_msg_id = EXCLUDED.target_msg_id
            """,
            int(src_msg_id), int(target_channel_id), int(target_msg_id),
        )


# ============ relay_origin ============

async def load_relay_origin() -> Dict[str, int]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT relayed_msg_id, origin_channel_id FROM relay_origin")
    return {str(r["relayed_msg_id"]): int(r["origin_channel_id"]) for r in rows}


async def upsert_relay_origin(relayed_msg_id: int, origin_channel_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO relay_origin (relayed_msg_id, origin_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (relayed_msg_id) DO UPDATE
              SET origin_channel_id = EXCLUDED.origin_channel_id
            """,
            int(relayed_msg_id), int(origin_channel_id),
        )


# ============ relay_reverse ============

async def load_relay_reverse() -> Dict[str, Dict[str, int]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT relayed_msg_id, src_msg_id, src_channel_id FROM relay_reverse"
        )
    return {
        str(r["relayed_msg_id"]): {
            "src_msg_id": int(r["src_msg_id"]),
            "src_channel_id": int(r["src_channel_id"]),
        }
        for r in rows
    }


async def upsert_relay_reverse(relayed_msg_id: int, src_msg_id: int, src_channel_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO relay_reverse (relayed_msg_id, src_msg_id, src_channel_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (relayed_msg_id) DO UPDATE
              SET src_msg_id = EXCLUDED.src_msg_id,
                  src_channel_id = EXCLUDED.src_channel_id
            """,
            int(relayed_msg_id), int(src_msg_id), int(src_channel_id),
        )


# ============ usage_state ============

DEFAULT_USAGE_STATE: Dict[str, Any] = {
    "window_start": None,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "input_cost": 0.0,
    "output_cost": 0.0,
    "total_cost": 0.0,
}


async def load_usage_state() -> Dict[str, Any]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT window_start, prompt_tokens, completion_tokens, total_tokens,
                      input_cost, output_cost, total_cost
               FROM usage_state WHERE id = 1"""
        )
    if row is None:
        return dict(DEFAULT_USAGE_STATE)
    return {
        "window_start": row["window_start"],
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "input_cost": float(row["input_cost"] or 0.0),
        "output_cost": float(row["output_cost"] or 0.0),
        "total_cost": float(row["total_cost"] or 0.0),
    }


async def save_usage_state(state: Dict[str, Any]) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_state
                (id, window_start, prompt_tokens, completion_tokens, total_tokens,
                 input_cost, output_cost, total_cost)
            VALUES (1, $1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE
              SET window_start = EXCLUDED.window_start,
                  prompt_tokens = EXCLUDED.prompt_tokens,
                  completion_tokens = EXCLUDED.completion_tokens,
                  total_tokens = EXCLUDED.total_tokens,
                  input_cost = EXCLUDED.input_cost,
                  output_cost = EXCLUDED.output_cost,
                  total_cost = EXCLUDED.total_cost
            """,
            state.get("window_start"),
            int(state.get("prompt_tokens", 0)),
            int(state.get("completion_tokens", 0)),
            int(state.get("total_tokens", 0)),
            float(state.get("input_cost", 0.0)),
            float(state.get("output_cost", 0.0)),
            float(state.get("total_cost", 0.0)),
        )


async def append_usage_log(window_label: str, tokens_used: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO usage_log (window_label, tokens_used) VALUES ($1, $2)",
            window_label, int(tokens_used),
        )


# ============ user_usage ============

async def load_user_usage() -> Dict[str, int]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, query_counts FROM user_usage")
    return {r["username"]: int(r["query_counts"] or 0) for r in rows}


async def bump_user_usage(username: str, by: int = 1) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_usage (username, query_counts)
            VALUES ($1, $2)
            ON CONFLICT (username) DO UPDATE
              SET query_counts = user_usage.query_counts + EXCLUDED.query_counts
            """,
            username, int(by),
        )


async def set_user_usage(username: str, count: int) -> None:
    """Set the absolute count (used by migration to overwrite, not add)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_usage (username, query_counts)
            VALUES ($1, $2)
            ON CONFLICT (username) DO UPDATE
              SET query_counts = EXCLUDED.query_counts
            """,
            username, int(count),
        )


async def truncate_usage_log() -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE usage_log RESTART IDENTITY")


# ============ translation_msg ============

async def insert_translation_row(
    msg_id: int,
    channel_id: Optional[int],
    guild_id: Optional[int],
    src_msg_id: int,
    origin_channel_id: Optional[int],
    author_id: Optional[int],
    author_name: str,
    target_lang: Optional[str],
    is_embed: bool,
    seq: int,
    text: str,
    created_at: datetime,
) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO translation_msg
              (msg_id, channel_id, guild_id, src_msg_id, origin_channel_id,
               author_id, author_name, target_lang, is_embed, seq, text, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12)
            ON CONFLICT (msg_id) DO NOTHING
            """,
            int(msg_id),
            int(channel_id) if channel_id is not None else None,
            int(guild_id) if guild_id is not None else None,
            int(src_msg_id or 0),
            int(origin_channel_id) if origin_channel_id is not None else None,
            int(author_id) if author_id is not None else None,
            author_name or "",
            target_lang,
            bool(is_embed),
            int(seq),
            text or "",
            created_at,
        )


async def update_translation_text(msg_id: int, text: str) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE translation_msg SET text = $2 WHERE msg_id = $1",
            int(msg_id), text or "",
        )


async def fetch_translations_changed_since(since: datetime) -> List[Dict[str, Any]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT msg_id, channel_id, guild_id, text, is_embed, updated_at
               FROM translation_msg
               WHERE updated_at > $1
               ORDER BY updated_at ASC""",
            since,
        )
    return [dict(r) for r in rows]


async def fetch_all_translations() -> List[Dict[str, Any]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT msg_id, channel_id, text, is_embed FROM translation_msg"
        )
    return [dict(r) for r in rows]


async def fetch_recent_translations(limit: int) -> List[Dict[str, Any]]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT msg_id, channel_id, guild_id, src_msg_id, origin_channel_id,
                      author_id, author_name, target_lang, is_embed, seq, text, created_at
               FROM translation_msg
               ORDER BY created_at DESC
               LIMIT $1""",
            int(limit),
        )
    return [dict(r) for r in rows]


async def fetch_existing_translation_ids() -> set:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT msg_id FROM translation_msg")
    return {int(r["msg_id"]) for r in rows}
