# MornBuildNotify

**使い方: https://tsukumistudio.github.io/MornBuildNotify/**

GitHub Actions の進捗と結果を、**1つの Discord メッセージを書き換えながら**通知する Action。

- 開始時にメッセージを1つ送り、途中経過も**完成形と同じ見た目**（コミット・工程・ログ）で出す。工程は ✅ 済 / ▶️ 実行中 / ⬜ 待機で並び、実行中の工程にはプログレスバーが付く
- バーは前回成功した同じ workflow の所要時間から見積もり、setup した job の間は数秒ごとに書き換えて進む（見積もりが無ければ経過に合わせて光が流れる）
- 各 step から `morn-build-notify update "文言"` で見出しの下の進捗文言を変える（送信は裏の定期更新にまとめる）
- Discord の Webhook は1分に30回まで。定期更新は6秒ごと（最大10回/分）で、内容が変わらなければ送らず、429 を受けたら指示された時間だけ待つ
- 終了時に、成功・失敗・中止・スキップ、コミット、工程ごとの結果、ログへのリンク、任意の欄に置き換える
- 失敗時は失敗した job のログから `::error` の内容（無ければエラーらしい行）を抜き出して載せ、コミット作者を Discord でメンションする（進捗メッセージを編集した場合、編集ではメンション通知が届かないので、メンションは別投稿にする。失敗した job を再実行したときは、前の試行の失敗メンションを消してから結果を出す。メンションのIDは同じ run の Actions cache で次の試行へ運ぶ）
- 通知に失敗してもビルドは止めない。Webhook URL はログに出さない
- 依存は `python3` と `gh`（GitHub-hosted runner には入っている）だけ

## 導入（テンプレート）

対象リポジトリのルートで実行する。設定ファイルと workflow の雛形を書き出し、最後に Webhook URL だけ聞いて Secret へ登録する（Enter で飛ばせる）。既存のファイルには触らない。書き出した後は自由に書き換える。

```bash
python3 <(curl -fsSL https://raw.githubusercontent.com/TsukumiStudio/MornBuildNotify/v1/init.py)
```

## 既存の workflow へ足す

```yaml
permissions:
  contents: read
  actions: read          # 工程結果と失敗ログの取得に使う

jobs:
  notify-start:
    runs-on: ubuntu-latest
    outputs:
      message-id: ${{ steps.notify.outputs.message-id }}
    steps:
      - uses: actions/checkout@v4   # 設定ファイルを読むため
      - id: notify
        uses: TsukumiStudio/MornBuildNotify@v1
        with:
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
          mode: start

  build:
    needs: notify-start
    steps:
      - uses: actions/checkout@v4
      - uses: TsukumiStudio/MornBuildNotify@v1       # mode 省略 = setup
        with:
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
          message-id: ${{ needs.notify-start.outputs.message-id }}
      - run: |
          morn-build-notify update "テスト中"
          ./test.sh

  notify:
    needs: [notify-start, build]
    if: ${{ always() }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: TsukumiStudio/MornBuildNotify@v1
        with:
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
          mode: finish
          message-id: ${{ needs.notify-start.outputs.message-id }}
          needs: ${{ toJSON(needs) }}
          fields: |
            遊ぶ: ${{ needs.build.outputs.url && format('[このビルドを遊ぶ]({0})', needs.build.outputs.url) || '' }}
```

`morn-build-notify` は setup した job の中なら、シェルからでもスクリプトからでも呼べる（`MORN_BUILD_NOTIFY_MESSAGE_ID` が無ければ何もしない）。

## 入力

| 入力 | 説明 |
|---|---|
| `webhook` | Discord Webhook URL。空なら何もしない |
| `mode` | `setup`（既定）/ `start` / `finish` |
| `message-id` | `start` の出力。setup・finish に渡すと同じメッセージを更新する。無ければ finish は新しく送る |
| `status` | finish の結果 `success` / `failure` / `cancelled` / `skipped`。空なら `needs` から決める（中止 > 失敗 > 全成功 > それ以外はスキップ） |
| `needs` | `${{ toJSON(needs) }}`。status の既定値と、API 障害時の表示に使う |
| `fields` | 足す欄。1行1項目の `名前: 値`。値の `\n` は改行。**値が空の行は出さない**ので、式で空にすれば条件付きの欄になる |
| `sha` | 通知に載せるコミット（既定 `github.sha`） |
| `config` | 設定ファイル（既定 `.github/morn-build-notify.json`） |
| `github-token` | 既定 `github.token` |

## 設定ファイル

すべて省略できる。無ければ既定の見た目で通知する。

```json
{
  "name": "MyGame",
  "username": "ビルド通知bot",
  "titles": {"success": "✅ 公開完了", "failure": "❌ 公開失敗", "cancelled": "⏹️ 中止", "skipped": "⏭️ スキップ"},
  "results": [
    {"name": "準備", "items": [
      {"label": "Checkout", "job": "Build", "step": "Checkout"},
      {"label": "公開", "job": "Publish"}
    ]}
  ],
  "errors": true,
  "mentions": {"matsufriends": "703992652391120911", "*": "703992652391120911"},
  "mention-message": "ビルドが失敗しました。確認をお願いします。"
}
```

| キー | 説明 |
|---|---|
| `name` | 進捗メッセージの見出し（既定はリポジトリ名） |
| `username` | 送信者名 |
| `titles` | 結果ごとの見出し。後ろに `: owner/repo` が付く。途中経過の見出しは `running`（既定 `🔄 ビルド中`、後ろに `（済/全）` が付く） |
| `results` | 工程結果の欄。`job` は job の表示名、`step` は step 名（省略すると job の結果）。途中経過でも同じ並びで状態とバーを出す。省略時は job を1欄に並べる |
| `errors` | 失敗時にログ抜粋を載せるか（既定 true） |
| `mentions` | 失敗時に呼ぶ人。GitHub ログイン名 → Discord ユーザーID。`*` は未登録・取得失敗時の宛先。無ければ誰も呼ばない |
| `mention-message` | メンションに続く文 |

## 送らずに確かめる

```bash
MORN_BUILD_NOTIFY_WEBHOOK=https://discord.com/api/webhooks/1/x GITHUB_REPOSITORY=o/r GITHUB_SHA=... \
GITHUB_RUN_ID=1 GITHUB_RUN_ATTEMPT=1 MORN_BUILD_NOTIFY_STATUS=failure python3 morn_build_notify.py payload
python3 -m unittest
```

## 版

`v1` は互換を保つ限り最新の `v1.x.y` を指す。
