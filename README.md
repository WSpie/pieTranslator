# Pie's Translator

[日本語](README.ja.md)

A Discord bot that bridges chat across languages, tuned for one mobile game at a time. Pair a `desc.json` (game profile) with an `ABBR_MAP.json` (in-game shorthand) and the rest of the code is a generic engine.

## What it does

- **Auto-translate** messages in any channel that has a rule, into that channel's target language.
- **Link two channels** in different languages — messages are mirrored both ways, reply chains preserved.
- **Flag-emoji translation** — react with a country flag (🇯🇵 / 🇺🇸 / 🇰🇷 / …) on any message and the bot replies with that translation. Auto-deletes after 60s.
- **Abbreviation expansion** — expand in-game shorthand (e.g. `VS` → `Duel`) before translating, so the model translates the meaning instead of the acronym.
- **Edit by CSV** — every translation is logged to `translation_msg.csv`. Edit the `text` column and the bot re-edits the Discord message.
- **Daily budget** — caps OpenAI spend per day; window resets at 19:00 CST.

## Setup

1. **Install dependencies**

   ```
   pip install "discord.py>=2.0" openai python-dotenv pyyaml certifi
   ```

   Tested on Python 3.10 with discord.py 2.6.

2. **Create `config.yaml`** (gitignored, holds secrets):

   ```yaml
   DISCORD_TOKEN: your-discord-bot-token
   OPENAI_API_KEY: sk-...
   ```

3. **`.env`** (optional runtime knobs):

   ```
   OPENAI_MODEL=gpt-4o-mini
   PIES_DEBUG=0
   FLAG_EPHEMERAL_SECONDS=60
   ```

4. **`desc.json`** — describe the game. Used to build the translator's system prompt. If missing, the bot runs as a generic translator.

5. **`ABBR_MAP.json`** — game shorthand to expand before translation. Optional.

6. **Run**

   ```
   python pies_translator_OPENAI.py
   ```

## Slash commands

| Command | What it does |
|---------|--------------|
| `/add channel language [flag]` | Set a channel's target language. |
| `/update channel [language] [flag]` | Update an existing rule. |
| `/del channel` | Remove a rule. |
| `/add_flag channel` | Enable flag-emoji translation only (language unchanged). |
| `/link channel1 channel2` | Bidirectionally mirror two channels. |
| `/syn_his channel [max_count] [days]` | Backfill recent messages from the linked channel. |
| `/backfill_csv [channel] [days]` | Backfill historical bot messages into the CSV log. |
| `/correct [size]` | Re-translate rows in the CSV that aren't in their target language. |
| `/usage` | Show today's OpenAI token / cost usage. |
| `/help` | List commands. |
| `/sync` | Re-sync slash commands to the current server. |

All commands require Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages, or being the server owner.

## Targeting a different game

The two files in `profile/` are the only things tied to *Last War: Survival Game*. To retarget:

- Edit `profile/desc.json` — name, genre, tone, what to preserve, per-language style overrides, Discord activity text.
- Edit `profile/ABBR_MAP.json` — the game's in-chat shorthand.

Nothing else in the codebase needs to change.

## Layout

```
.
├── pies_translator_OPENAI.py     main program
├── config.yaml                   secrets (gitignored)
├── .env                          non-secret runtime knobs
├── README.md / README.ja.md
├── profile/                      game profile — edit to retarget
│   ├── desc.json
│   └── ABBR_MAP.json
├── data/                         runtime state, bot-managed
│   ├── built_rules.json          per-channel rules
│   ├── relay_map.json            cross-channel relay state
│   ├── relay_reverse.json
│   ├── relay_origin.json
│   ├── translation_msg.csv       translation log — edit to re-edit Discord
│   ├── user_query_hist.csv       per-user query counts
│   ├── usage_state.json          daily OpenAI budget snapshot
│   └── usage.log
└── archives/                     older versions, kept for reference
```
