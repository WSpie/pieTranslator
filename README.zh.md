# Pie's Translator

[English](README.md) · [日本語](README.ja.md)

跨语言 Discord 聊天翻译 bot。目前是为 *Last War: Survival Game* 配置的。换 `profile/` 下两个文件就能切到别的游戏，其他代码不用动。

## 功能

频道有语言规则的话，新消息进来 bot 会翻译并以 embed 回复。

把两个不同语言的频道 link 起来，消息会双向镜像，回复关系也跟着对齐。

给任意消息加国旗 emoji（🇯🇵 / 🇺🇸 / 🇰🇷 等）反应，bot 会单独发一条对应语言的翻译。1 分钟后自动删除，免得国旗刷屏。

`profile/ABBR_MAP.json` 里写的游戏术语缩写（VS、DS、mud 等）在模型看到消息之前先展开，所以翻译出来是意思，不是字面缩写。

所有翻译都会写到 `data/translation_msg.csv`。改这个文件的 `text` 列，bot 会反向编辑对应的 Discord 消息。批量校对就是这么用的：在表里改完，频道里跟着更新。

OpenAI 有日预算上限（默认 $5，可改）。窗口每天 19:00 CST 重置。

## 安装

装依赖：

```
pip install -r requirements.txt
```

在 Python 3.10 + discord.py 2.6 上验证过。

密钥从环境变量读取（Heroku Config Vars）。本地开发也可以放进 `config.yaml`
（已 gitignore）；两者都设时环境变量优先：

```yaml
DISCORD_TOKEN: your-discord-bot-token
OPENAI_API_KEY: sk-...        # API_OPTION=1 时需要
TAMU_API_KEY: sk-...          # API_OPTION=2 时需要
DATABASE_URL: postgres://...  # Heroku Postgres 会自动注入
```

非敏感的运行参数放 `.env`：

```
API_OPTION=1                  # 1 = OpenAI（默认），2 = TAMU AI Chat
OPENAI_MODEL=gpt-5.4-mini
PIES_DEBUG=0
FLAG_EPHEMERAL_SECONDS=60
```

### 用 TAMU AI Chat 代替 OpenAI

TAMU AI Chat 兼容 OpenAI 接口，翻译逻辑完全不用改。把 `API_OPTION` 设成 `2`，
并提供 `TAMU_API_KEY`（在 https://tamus.ai 登录 → Settings → Account → API Key
创建）。可选项：

```
API_OPTION=2
TAMU_MODEL=protected.gemini-2.5-flash-lite   # 注意必须带 "protected." 前缀
TAMU_API_ENDPOINT=https://chat-api.tamu.ai   # 你所在院校的端点，默认是 Texas A&M University
```

TAMU 的模型名都带 `protected.` 前缀（如 `protected.gpt-4.1-mini`、
`protected.gpt-5.4-mini`）。列出你可用的确切模型 id：

```bash
curl -H "Authorization: Bearer $TAMU_API_KEY" https://chat-api.tamu.ai/api/models
```

**在 Heroku 上（此分支的部署目标）**：在 Config Vars 里加 `API_OPTION=2` 和
`TAMU_API_KEY`，保存后 dyno 会自动重启切到 TAMU。不在 Texas A&M University 的话，
去 https://docs.tamus.ai/docs/prod/api-tool/api-endpoints/ 查你院校的端点。

`profile/` 下两个文件描述游戏。`desc.json` 缺失的话 bot 照样能跑，只是变成无游戏上下文的通用翻译。

跑起来：

```
python pies_translator_OPENAI.py
```

## Slash 命令

| 命令 | 作用 |
|------|------|
| `/add channel language [flag]` | 给频道设一个目标语言。 |
| `/update channel [language] [flag]` | 改已有规则。 |
| `/del channel` | 删规则。 |
| `/add_flag channel` | 只开国旗 emoji 翻译，不改频道语言。 |
| `/link channel1 channel2` | 把两个频道双向镜像。 |
| `/syn_his channel [max_count] [days]` | 从 link 的对端频道拉最近的消息翻过来。 |
| `/backfill_csv [channel] [days]` | 把 bot 的历史消息回填进 CSV。 |
| `/correct [size]` | 扫 CSV 最近 N 行，把不在目标语言的重新翻一遍。 |
| `/usage` | 今天 OpenAI 用了多少 token / 多少钱。 |
| `/help` | 命令列表。 |
| `/sync` | 重新注册 slash 命令到当前服务器。 |

这些命令需要 Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages 任一权限，或是服务器 owner。

## 换到别的游戏

游戏相关的东西只在这两个文件里：

- `profile/desc.json`：游戏名、类型、翻译的语气、要原样保留的内容、按语言的风格指示（比如目前的设置里日文翻译固定用丁寧語）、Discord 状态栏文字。
- `profile/ABBR_MAP.json`：这款游戏的聊天缩写。

改这俩就够了，代码不用碰。

## 目录结构

```
.
├── pies_translator_OPENAI.py     主程序
├── config.yaml                   密钥（gitignore 不入库）
├── .env                          非敏感运行参数
├── README.md / README.ja.md / README.zh.md
├── profile/                      游戏 profile（换游戏改这里）
│   ├── desc.json
│   └── ABBR_MAP.json
├── data/                         运行时状态，bot 自己管
│   ├── built_rules.json          各频道规则
│   ├── relay_map.json            跨频道转发状态
│   ├── relay_reverse.json
│   ├── relay_origin.json
│   ├── translation_msg.csv       翻译日志（可手编）
│   ├── user_query_hist.csv       每个用户查询次数
│   ├── usage_state.json          OpenAI 日预算快照
│   └── usage.log
└── archives/                     旧版本存档
```
