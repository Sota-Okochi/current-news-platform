# Current News Research Platform

GitHub Actions・Notion・OpenAI APIを使って、毎日2回、重要な時事ニュースをNotionデータベースへ自動追加する仕組みです。

以前作成した「NEC daily research」と同じく、次の流れで動きます。

1. GitHub Actionsが定期実行される
2. Notionから直近の記事を取得する
3. OpenAI APIのWeb検索でニュース候補を収集する
4. 直近記事との重複を除外する
5. 条件に合う3記事をNotionへ追加する

---

## ファイル構成

```text
.
├── .github/
│   └── workflows/
│       └── current-news-research.yml
├── scripts/
│   └── current_news_research.py
├── .env.example
├── requirements.txt
└── README.md
```

---

## 実行スケジュール

日本時間で毎日2回実行します。

| JST | UTC | cron |
|---|---:|---|
| 09:00 | 00:00 | `0 0 * * *` |
| 19:00 | 10:00 | `0 10 * * *` |

GitHub ActionsのcronはUTC基準のため、JSTから9時間引いた時刻を設定しています。

手動実行もできるように、workflowには `workflow_dispatch` を入れています。

---

## Notionデータベースの準備

Notion側で、以下のプロパティを作成してください。

| プロパティ名 | 型 | 内容 |
|---|---|---|
| チェックポイント | チェックボックス | 読んだかどうか。追加時点では未チェック |
| タイトル | タイトル | 記事内容を簡潔に表す題名 |
| 日付 | 日付 | 追加・整理日 |
| 重要ポイント | テキスト | 3点程度の箇条書き |
| カテゴリ | セレクト | 外交 / 世界情勢 / 国内政治 / 政策 / マクロ経済 / テクノロジー |
| URL | URL | 記事または公式発表のURL |
| 信頼度 | セレクト | 公式発表 / 報道 / 要確認 |

カテゴリの選択肢:

- 外交
- 世界情勢
- 国内政治
- 政策
- マクロ経済
- テクノロジー

信頼度の選択肢:

- 公式発表
- 報道
- 要確認

### Notion連携の注意

Notionのインテグレーションを作成したら、対象データベースの右上メニューから連携を追加してください。
連携されていない場合、APIキーやDB IDが正しくてもNotion APIからは404になることがあります。

---

## GitHub Secretsに登録する値

GitHubリポジトリの以下から登録します。

`Settings` → `Secrets and variables` → `Actions` → `Secrets`

| Secret名 | 内容 |
|---|---|
| `OPENAI_API_KEY` | OpenAI APIキー |
| `NOTION_API_KEY` | Notion Integration Secret |
| `NOTION_DATABASE_ID` | NotionデータベースID |

必要に応じて、GitHub Variablesに以下を登録できます。

| Variable名 | デフォルト | 内容 |
|---|---:|---|
| `OPENAI_MODEL` | `gpt-4.1-mini` | 使用するOpenAIモデル |

---

## ローカルでの動作確認

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` に実際の値を入れてから実行します。

```bash
python scripts/current_news_research.py
```

---

## 重複防止の仕組み

実行時に、Notionデータベースから直近12件を取得します。

重複判定では、以下を確認します。

- URLが同じか
- URLのクエリパラメータだけが違う同一記事か
- タイトル、カテゴリ、重要ポイント、topic_keyが近い同一テーマか
- 同じニュースを扱う別メディア記事と思われるか

OpenAIへの指示でも重複除外を行い、さらにPython側でもURL・テキスト類似度で二重チェックします。

`LOOKBACK_LIMIT` を増やすと、より長い期間の重複防止ができます。ただしNotionから読む件数が増えます。

---

## API使用量を抑える工夫

通常はOpenAI APIを1回だけ呼び出します。

1回で9件程度の候補を出し、その中からPython側で3件を選びます。
重複が多く3件に満たない場合だけ、追加で1回リサーチします。

Web検索の検索コンテキストは `low` にしています。
より詳しく調べたい場合は、`.env` またはGitHub Actionsの環境変数で変更できます。

```env
OPENAI_SEARCH_CONTEXT_SIZE=medium
```

---

## 停止方法

一時停止したい場合は、コードを削除せずに以下のどれかを行います。

### 方法1: GitHub Actions画面から停止

`Actions` → `Current News Research` → `...` → `Disable workflow`

一番安全です。再開したいときは `Enable workflow` に戻します。

### 方法2: workflowのcronだけコメントアウト

`.github/workflows/current-news-research.yml` の `schedule` 部分をコメントアウトします。

```yml
on:
  workflow_dispatch:
  # schedule:
  #   - cron: "0 0 * * *"
  #   - cron: "0 10 * * *"
```

この場合、手動実行は残ります。

### 方法3: Secretsを外す

`OPENAI_API_KEY` などのSecretを削除すれば実行は失敗します。
ただしエラーログが残るため、停止目的なら方法1か2がおすすめです。

---

## エラー時の見方

GitHub Actionsのログに以下が出ます。

- Notionから直近何件を取得したか
- OpenAI APIを何回呼んだか
- 候補記事が何件返ったか
- 重複除外された理由
- Notion追加に成功した記事数

よくある原因:

| エラー | 原因 |
|---|---|
| Notion API 401 | `NOTION_API_KEY` が間違っている |
| Notion API 404 | DB IDが違う、またはDBにIntegrationを追加していない |
| Notion API 400 | Notionプロパティ名や型が違う |
| OpenAI API 401 | `OPENAI_API_KEY` が間違っている |
| OpenAI API 429 | 利用上限、レート制限、または残高不足 |

---

## カスタマイズ

Notionのプロパティ名が違う場合は、環境変数で変更できます。

```env
NOTION_PROP_CHECKPOINT=チェックポイント
NOTION_PROP_TITLE=タイトル
NOTION_PROP_DATE=日付
NOTION_PROP_IMPORTANT_POINTS=重要ポイント
NOTION_PROP_CATEGORY=カテゴリ
NOTION_PROP_URL=URL
NOTION_PROP_RELIABILITY=信頼度
```

1回あたりの記事数を増やす場合:

```env
ARTICLES_PER_RUN=5
CANDIDATE_COUNT=12
```

ただし記事数や候補数を増やすと、API使用量も増えます。
