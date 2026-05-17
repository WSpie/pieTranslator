# Pie's Translator

[English](README.md) · [中文](README.zh.md)

言語を跨ぐ Discord チャット翻訳 bot。今は *Last War: Survival Game* 用に設定してある。`profile/` の 2 ファイルを差し替えれば他のゲームにも転用できる。コード本体は汎用エンジン。

## できること

チャンネルに言語ルールが設定されていれば、そこに来た新しいメッセージを bot が翻訳して embed で返信する。

2 つのチャンネルを連携させると、両方向にメッセージがミラーリングされる。返信関係もペア内で揃う。

メッセージに国旗 emoji（🇯🇵 / 🇺🇸 / 🇰🇷 など）でリアクションすると、その言語への翻訳が単発で投稿される。1 分後に自動で消えるので、国旗が増えてもチャンネルが汚れない。

`profile/ABBR_MAP.json` に書いたゲーム内略語（VS、DS、mud など）は、モデルが見る前にあらかじめ展開される。略語のまま訳されるのではなく、意味として訳される。

翻訳結果はすべて `data/translation_msg.csv` に書き出される。CSV の `text` 列を編集すると、対応する Discord メッセージも書き換わる。スプレッドシートで一括校正する用途を想定している。

OpenAI には日次の予算上限がある（デフォルト $5、変更可）。ウィンドウは毎日 19:00 CST にリセット。

## セットアップ

依存パッケージのインストール：

```
pip install "discord.py>=2.0" openai python-dotenv pyyaml certifi
```

Python 3.10 + discord.py 2.6 で動作確認済み。

シークレットは `config.yaml` に入れる（gitignore 済み）：

```yaml
DISCORD_TOKEN: your-discord-bot-token
OPENAI_API_KEY: sk-...
```

機密でない実行時設定は `.env` に：

```
OPENAI_MODEL=gpt-4o-mini
PIES_DEBUG=0
FLAG_EPHEMERAL_SECONDS=60
```

`profile/` 配下の 2 ファイルがゲーム情報。`desc.json` が無い場合はゲーム情報なしの汎用翻訳 bot として起動する。

起動：

```
python pies_translator_OPENAI.py
```

## スラッシュコマンド

| コマンド | 内容 |
|---------|------|
| `/add channel language [flag]` | チャンネルに目標言語を設定。 |
| `/update channel [language] [flag]` | 既存ルールの変更。 |
| `/del channel` | ルール削除。 |
| `/add_flag channel` | 言語は変えずに国旗 emoji 翻訳だけ有効化。 |
| `/link channel1 channel2` | 2 つのチャンネルを双方向ミラー。 |
| `/syn_his channel [max_count] [days]` | 連携先チャンネルの直近メッセージを取り込んで翻訳。 |
| `/backfill_csv [channel] [days]` | 過去の bot メッセージを CSV に書き戻し。 |
| `/correct [size]` | CSV の直近 N 行を見て、目標言語になっていない行を再翻訳。 |
| `/usage` | 当日の OpenAI トークン・コストの使用状況。 |
| `/help` | コマンド一覧。 |
| `/sync` | 現在のサーバーにスラッシュコマンドを再同期。 |

すべて Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages のいずれか、もしくはサーバーオーナー権限が必要。

## 別ゲームに転用するには

ゲーム固有の情報は次の 2 ファイルだけに集約してある：

- `profile/desc.json`：ゲーム名、ジャンル、翻訳に使うトーン、保持すべき要素、言語別のスタイル指示（今の設定では日本語訳は丁寧語固定）、Discord のステータス文言。
- `profile/ABBR_MAP.json`：そのゲームの略語マップ。

この 2 つを編集すればよく、コード本体は触らなくていい。

## ファイル配置

```
.
├── pies_translator_OPENAI.py     本体プログラム
├── config.yaml                   シークレット（gitignore 済み）
├── .env                          機密でない実行時設定
├── README.md / README.ja.md
├── profile/                      ゲームプロファイル（転用時はここを編集）
│   ├── desc.json
│   └── ABBR_MAP.json
├── data/                         実行時の状態（bot が自動管理）
│   ├── built_rules.json          チャンネル別ルール
│   ├── relay_map.json            チャンネル間中継の状態
│   ├── relay_reverse.json
│   ├── relay_origin.json
│   ├── translation_msg.csv       翻訳ログ（編集可）
│   ├── user_query_hist.csv       ユーザー別クエリ回数
│   ├── usage_state.json          OpenAI 日次予算のスナップショット
│   └── usage.log
└── archives/                     旧バージョン置き場
```
