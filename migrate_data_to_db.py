# migrate_data_to_db.py
# -*- coding: utf-8 -*-
"""
One-time migration: load existing data/ files into Postgres.

Reads:
  - data/built_rules.json
  - data/relay_map.json
  - data/relay_origin.json
  - data/relay_reverse.json
  - data/usage_state.json
  - data/usage.log
  - data/user_query_hist.csv
  - data/translation_msg.csv  (if present; gitignored, only on machines that have it)

Writes to Postgres tables defined in db.py. Idempotent — re-runs are safe.

Usage:
  # Local (use DATABASE_URL from env or config.yaml):
  python migrate_data_to_db.py

  # Heroku (one-off dyno):
  heroku run python migrate_data_to_db.py
"""

import os
import re
import csv
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

import yaml

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("migrate")

DATA_DIR = Path("data")
CSV_ENCODING = "utf-8-sig"


def _read_secret(key: str, cfg: Dict[str, Any]) -> str:
    return (os.getenv(key) or str(cfg.get(key, "") or "")).strip()


def _load_cfg() -> Dict[str, Any]:
    p = Path("config.yaml")
    if p.exists():
        try:
            return yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as e:
            log.warning(f"config.yaml unreadable: {e}")
    return {}


async def migrate_rules() -> int:
    p = DATA_DIR / "built_rules.json"
    if not p.exists():
        log.info("skip rules: %s missing", p)
        return 0
    raw = json.loads(p.read_text())
    n = 0
    for cid_str, rec in raw.items():
        try:
            cid = int(cid_str)
        except ValueError:
            continue
        await db.upsert_rule(
            cid,
            rec.get("language"),
            bool(rec.get("flag", False)),
            int(rec["link_channel_id"]) if rec.get("link_channel_id") is not None else None,
        )
        n += 1
    log.info("rules: %d rows", n)
    return n


async def migrate_relay_map() -> int:
    p = DATA_DIR / "relay_map.json"
    if not p.exists():
        log.info("skip relay_map: %s missing", p)
        return 0
    raw = json.loads(p.read_text())
    n = 0
    for src_msg_id_str, target_dict in raw.items():
        try:
            src_msg_id = int(src_msg_id_str)
        except ValueError:
            continue
        for ch_id_str, target_msg_id in (target_dict or {}).items():
            try:
                ch_id = int(ch_id_str)
                tgt_id = int(target_msg_id)
            except (ValueError, TypeError):
                continue
            await db.upsert_relay_map(src_msg_id, ch_id, tgt_id)
            n += 1
    log.info("relay_map: %d rows", n)
    return n


async def migrate_relay_origin() -> int:
    p = DATA_DIR / "relay_origin.json"
    if not p.exists():
        log.info("skip relay_origin: %s missing", p)
        return 0
    raw = json.loads(p.read_text())
    n = 0
    for relayed_id_str, origin_id in raw.items():
        try:
            relayed_id = int(relayed_id_str)
            origin = int(origin_id)
        except (ValueError, TypeError):
            continue
        await db.upsert_relay_origin(relayed_id, origin)
        n += 1
    log.info("relay_origin: %d rows", n)
    return n


async def migrate_relay_reverse() -> int:
    p = DATA_DIR / "relay_reverse.json"
    if not p.exists():
        log.info("skip relay_reverse: %s missing", p)
        return 0
    raw = json.loads(p.read_text())
    n = 0
    for relayed_id_str, rec in raw.items():
        try:
            relayed_id = int(relayed_id_str)
            src_msg = int(rec.get("src_msg_id", 0))
            src_ch = int(rec.get("src_channel_id", 0))
        except (ValueError, TypeError):
            continue
        if not (src_msg and src_ch):
            continue
        await db.upsert_relay_reverse(relayed_id, src_msg, src_ch)
        n += 1
    log.info("relay_reverse: %d rows", n)
    return n


async def migrate_usage_state() -> bool:
    p = DATA_DIR / "usage_state.json"
    if not p.exists():
        log.info("skip usage_state: %s missing", p)
        return False
    state = json.loads(p.read_text())
    await db.save_usage_state(state)
    log.info("usage_state: 1 row")
    return True


_USAGE_LOG_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+(\d+)/")


async def migrate_usage_log() -> int:
    p = DATA_DIR / "usage.log"
    if not p.exists():
        log.info("skip usage_log: %s missing", p)
        return 0
    # usage.log is append-only; clear the table first so re-running migration
    # doesn't double-count.
    await db.truncate_usage_log()
    n = 0
    for line in p.read_text().splitlines():
        m = _USAGE_LOG_RE.match(line.strip())
        if not m:
            continue
        await db.append_usage_log(m.group(1), int(m.group(2)))
        n += 1
    log.info("usage_log: %d rows", n)
    return n


async def migrate_user_usage() -> int:
    p = DATA_DIR / "user_query_hist.csv"
    if not p.exists():
        log.info("skip user_usage: %s missing", p)
        return 0
    n = 0
    with p.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("username") or "").strip()
            if not name:
                continue
            try:
                cnt = int(row.get("query_counts") or 0)
            except ValueError:
                cnt = 0
            # set_user_usage overwrites instead of incrementing, so re-running
            # migration leaves counts unchanged.
            await db.set_user_usage(name, cnt)
            n += 1
    log.info("user_usage: %d rows", n)
    return n


async def migrate_translation_csv() -> int:
    p = DATA_DIR / "translation_msg.csv"
    if not p.exists():
        log.info("skip translation_msg: %s missing (gitignored)", p)
        return 0
    n = 0
    with p.open("r", newline="", encoding=CSV_ENCODING) as f:
        for row in csv.DictReader(f):
            try:
                msg_id = int((row.get("msg_id") or "0").strip() or 0)
                if msg_id <= 0:
                    continue
                channel_id = int(row["channel_id"]) if row.get("channel_id") else None
                guild_id = int(row["guild_id"]) if row.get("guild_id") else None
                src_msg_id = int(row["src_msg_id"]) if row.get("src_msg_id") else 0
                origin_channel_id = int(row["origin_channel_id"]) if row.get("origin_channel_id") else None
                author_id = int(row["author_id"]) if row.get("author_id") else None
                created_raw = row.get("created_at") or ""
                created_at = datetime.fromisoformat(created_raw) if created_raw else datetime.utcnow()
                is_embed = (row.get("is_embed", "1").strip() or "1") == "1"
                seq = int(row.get("seq") or 0)
            except Exception as e:
                log.warning("skip bad row: %s (%s)", row, e)
                continue
            await db.insert_translation_row(
                msg_id=msg_id,
                channel_id=channel_id,
                guild_id=guild_id,
                src_msg_id=src_msg_id,
                origin_channel_id=origin_channel_id,
                author_id=author_id,
                author_name=row.get("author_name") or "",
                target_lang=row.get("target_lang") or None,
                is_embed=is_embed,
                seq=seq,
                text=row.get("text") or "",
                created_at=created_at,
            )
            n += 1
    log.info("translation_msg: %d rows", n)
    return n


async def main_async():
    cfg = _load_cfg()
    database_url = _read_secret("DATABASE_URL", cfg)
    if not database_url:
        raise SystemExit(
            "DATABASE_URL is required.\n"
            "  Local: export DATABASE_URL=postgresql://user:pass@host:5432/dbname\n"
            "  Heroku: heroku run python migrate_data_to_db.py (DATABASE_URL is auto-set)\n"
        )

    log.info("Initializing DB pool...")
    await db.init_pool(database_url)
    try:
        log.info("Schema created/verified.")
        await migrate_rules()
        await migrate_relay_map()
        await migrate_relay_origin()
        await migrate_relay_reverse()
        await migrate_usage_state()
        await migrate_usage_log()
        await migrate_user_usage()
        await migrate_translation_csv()
        log.info("Migration complete.")
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main_async())
