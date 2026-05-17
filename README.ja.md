# Pie's Translator

[English](README.md)

Discord 上で異なる言語のチャットをつなぐ翻訳 bot です。`desc.json`（ゲームプロファイル）と `ABBR_MAP.json`（ゲーム内略語）の 2 ファイルを差し替えるだけで別のゲームに転用でき、その他のコードは汎用エンジンとして動作します。

## できること

- **自動翻訳** — ルールが設定されたチャンネルに投稿されたメッセージを、そのチャンネルの目標言語に翻訳します。
- **チャンネル連携** — 異なる言語の 2 チャンネルを双方向にミラーリング。返信関係も保持されます。
- **国旗 emoji 翻訳** — 任意のメッセージに国旗（🇯🇵 / 🇺🇸 / 🇰🇷 …）でリアクションすると、その言語に翻訳されたメッセージが返信されます。60 秒後に自動削除。
- **略語展開** — ゲーム内の略語（例：`VS` → `Duel`）を翻訳前に展開し、モデルが略語のままではなく意味として翻訳できるようにします。
- **CSV で編集** — すべての翻訳は `translation_msg.csv` に記録されます。`text` 列を編集すると Discord 上のメッセージも自動で更新されます。
- **日次予算管理** — OpenAI の 1 日あたりの支出に上限を設けます。集計ウィンドウは 19:00 CST にリセット。

## セットアップ

1. **依存パッケージのインストール**

   ```
   pip install "discord.py>=2.0" openai python-dotenv pyyaml certifi
   ```

   Python 3.10 + discord.py 2.6 で動作確認済み。

2. **`config.yaml` を作成**（gitignore 済み。シークレットを格納）：

   ```yaml
   DISCORD_TOKEN: your-discord-bot-token
   OPENAI_API_KEY: sk-...
   ```

3. **`.env`**（任意。実行時の調整用）：

   ```
   OPENAI_MODEL=gpt-4o-mini
   PIES_DEBUG=0
   FLAG_EPHEMERAL_SECONDS=60
   ```

4. **`desc.json`** — ゲームの説明。翻訳用 system prompt の構築に使用されます。ファイルがない場合は汎用翻訳モードで動作します。

5. **`ABBR_MAP.json`** — 翻訳前に展開する略語マップ。任意。

6. **起動**

   ```
   python pies_translator_OPENAI.py
   ```

## スラッシュコマンド

| コマンド | 内容 |
|---------|------|
| `/add channel language [flag]` | チャンネルに目標言語を設定。 |
| `/update channel [language] [flag]` | 既存ルールの更新。 |
| `/del channel` | ルールの削除。 |
| `/add_flag channel` | 国旗 emoji 翻訳のみを有効化（言語は変更しない）。 |
| `/link channel1 channel2` | 2 つのチャンネルを双方向にミラーリング。 |
| `/syn_his channel [max_count] [days]` | 連携先チャンネルの直近メッセージを取り込み。 |
| `/backfill_csv [channel] [days]` | 過去の bot メッセージを CSV に書き戻し。 |
| `/correct [size]` | CSV 内で目標言語になっていない行を再翻訳。 |
| `/usage` | 当日の OpenAI トークン / コスト使用状況。 |
| `/help` | コマンド一覧。 |
| `/sync` | スラッシュコマンドを現在のサーバーに再同期。 |

すべてのコマンドは Administrator / Manage Server / Manage Channels / Manage Roles / Manage Messages のいずれか、もしくはサーバーオーナー権限が必要です。

## 別ゲームへの転用

*Last War: Survival Game* に依存しているのは `profile/` 配下の 2 ファイルだけです：

- `profile/desc.json` — ゲーム名、ジャンル、トーン、保持すべき情報、言語別のスタイル指示、Discord ステータスの文言。
- `profile/ABBR_MAP.json` — そのゲームの略語。

それ以外のコードは汎用なので変更不要です。

## ディレクトリ構成

```
.
├── pies_translator_OPENAI.py     本体プログラム
├── config.yaml                   シークレット（gitignore 済み）
├── .env                          非機密な実行時設定
├── README.md / README.ja.md
├── profile/                      ゲームプロファイル — 別ゲームに転用する場合はここを編集
│   ├── desc.json
│   └── ABBR_MAP.json
├── data/                         実行時の状態（bot が自動管理）
│   ├── built_rules.json          チャンネル別ルール
│   ├── relay_map.json            チャンネル間中継の状態
│   ├── relay_reverse.json
│   ├── relay_origin.json
│   ├── translation_msg.csv       翻訳ログ（編集で Discord 側も更新）
│   ├── user_query_hist.csv       ユーザー別クエリ回数
│   ├── usage_state.json          OpenAI 日次予算のスナップショット
│   └── usage.log
└── archives/                     旧バージョン（参考用）
```
