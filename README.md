# Pie's Translator

[日本語](README.ja.md) · [中文](README.zh.md)

A Discord bot for cross-language chat. Set up right now for *Last War: Survival Game*. Swap two files in `profile/` to point it at a different game. Everything else stays the same.

## What it does

If a channel has a language rule, the bot translates new messages there and replies with an embed.

Link two channels in different languages and the bot mirrors messages both ways. Reply threading stays consistent across the pair.

React to any message with a country flag (🇯🇵, 🇺🇸, 🇰🇷, ...) and the bot posts a one-off translation. It auto-deletes after a minute so flag spam doesn't pile up.

Game shorthand from `profile/ABBR_MAP.json` (VS, DS, mud, ...) gets expanded inline before the model sees the message, so it translates the meaning instead of the acronym.

Every translation lands in `data/translation_msg.csv`. Edit the `text` column there and the bot edits the matching Discord message. That's the bulk-proofreading path: fix it in the spreadsheet, channels update.

There is a daily OpenAI budget cap (default $5, configurable). The window resets at 19:00 CST.

## Setup

Install dependencies:

```
pip install "discord.py>=2.0" openai python-dotenv pyyaml certifi
```

Tested on Python 3.10 with discord.py 2.6.

Put secrets in `config.yaml` (gitignored):

```yaml
DISCORD_TOKEN: your-discord-bot-token
OPENAI_API_KEY: sk-...
```

Non-secret runtime knobs go in `.env`:

```
OPENAI_MODEL=gpt-4o-mini
PIES_DEBUG=0
FLAG_EPHEMERAL_SECONDS=60
```

The two files in `profile/` describe the game. If `desc.json` is missing the bot still runs, just as a generic translator with no game context.

Then:

```
python pies_translator_OPENAI.py
```

## Slash commands

| Command | What it does |
|---------|--------------|
| `/add channel language [flag]` | Give a channel a target language. |
| `/update channel [language] [flag]` | Change an existing rule. |
| `/del channel` | Drop a rule. |
| `/add_flag channel` | Turn on flag-emoji mode without changing the language. |
| `/link channel1 channel2` | Mirror two channels both ways. |
| `/syn_his channel [max_count] [days]` | Pull recent messages from the linked channel and translate them across. |
| `/backfill_csv [channel] [days]` | Backfill historical bot messages into the CSV log. |
| `/correct [size]` | Scan the last N rows of the CSV and re-translate any that aren't in their target language. |
| `/usage` | Today's OpenAI tokens and cost. |
| `/help` | List the commands. |
| `/sync` | Re-register slash commands on this server. |

All of these need Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages, or to be the server owner.

## Pointing it at a different game

Two files carry everything game-specific:

- `profile/desc.json`: the game's name, genre, what tone the translations should use, what to preserve verbatim, per-language style notes (the current setup keeps Japanese polite, for example), and the bot's Discord status string.
- `profile/ABBR_MAP.json`: in-chat shorthand for the game.

Change those two and the rest of the code stays put.

## Layout

```
.
├── pies_translator_OPENAI.py     main program
├── config.yaml                   secrets, gitignored
├── .env                          non-secret runtime knobs
├── README.md / README.ja.md
├── profile/                      game profile, edit to retarget
│   ├── desc.json
│   └── ABBR_MAP.json
├── data/                         runtime state, bot manages this
│   ├── built_rules.json          per-channel rules
│   ├── relay_map.json            cross-channel relay state
│   ├── relay_reverse.json
│   ├── relay_origin.json
│   ├── translation_msg.csv       translation log, editable
│   ├── user_query_hist.csv       per-user query counts
│   ├── usage_state.json          daily OpenAI budget snapshot
│   └── usage.log
└── archives/                     older versions kept around
```
