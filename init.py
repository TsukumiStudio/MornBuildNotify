#!/usr/bin/env python3
"""MornBuildNotify のテンプレートを書き出す。対象リポジトリのルートで実行する。

  python3 <(curl -fsSL https://raw.githubusercontent.com/TsukumiStudio/MornBuildNotify/v1/init.py)

設定ファイルと workflow の雛形を書き出し、最後に Webhook URL だけ聞いて Secret へ登録する。
既存のファイルには触らない。書き出した後は自由に書き換えてよい。
"""
import getpass
import json
import re
import subprocess
from pathlib import Path

SECRET = "DISCORD_WEBHOOK"
CONFIG = Path(".github/morn-build-notify.json")
WORKFLOW = Path(".github/workflows/build.yml")

WORKFLOW_TEXT = """name: Build

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  actions: read   # 工程結果と失敗ログの取得に使う

jobs:
  notify-start:
    runs-on: ubuntu-latest
    outputs:
      message-id: ${{ steps.notify.outputs.message-id }}
    steps:
      - uses: actions/checkout@v4
      - id: notify
        uses: TsukumiStudio/MornBuildNotify@v1
        with:
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
          mode: start

  build:
    needs: notify-start
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # これ以降の step で `morn-build-notify update "文言"` が使える。
      - uses: TsukumiStudio/MornBuildNotify@v1
        with:
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
          message-id: ${{ needs.notify-start.outputs.message-id }}
      - name: Build
        run: |
          morn-build-notify update "ビルド中"
          echo "ここをビルドのコマンドに書き換える"

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
          # 成果物のリンクなどを足すときは 1行1項目の `名前: 値`。値が空の行は出ない。
          # fields: |
          #   遊ぶ: ${{ needs.build.outputs.url && format('[このビルドを遊ぶ]({0})', needs.build.outputs.url) || '' }}
"""


def write(path, text):
    if path.exists():
        print(f"  {path} は既にあるので触りません")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  {path} を書き出しました")


def main():
    if not Path(".git").exists():
        raise SystemExit("リポジトリのルートで実行してください")
    write(CONFIG, json.dumps({"name": Path.cwd().name, "mentions": {}}, ensure_ascii=False, indent=2) + "\n")
    write(WORKFLOW, WORKFLOW_TEXT)

    try:
        url = getpass.getpass(f"\nDiscord の Webhook URL（Secret {SECRET} へ登録。Enter で飛ばす）: ").strip()
    except EOFError:
        url = ""
    if not url:
        print(f"  飛ばしました。あとで `gh secret set {SECRET}` で登録してください")
    elif not re.fullmatch(r"https://(discord|discordapp)\.com/api/webhooks/[0-9]+/\S+", url):
        print("  Discord の Webhook URL ではないため登録しませんでした")
    elif subprocess.run(["gh", "secret", "set", SECRET], input=url, text=True).returncode == 0:
        print(f"  Secret {SECRET} を登録しました")
    else:
        print(f"  登録に失敗しました。あとで `gh secret set {SECRET}` を実行してください")

    print(f"""
次にやること:
  1. {WORKFLOW} の `echo "ここを…"` をビルドのコマンドに書き換える
  2. 失敗時にメンションしたい人がいれば {CONFIG} の mentions に
     "GitHubログイン名": "DiscordユーザーID" を足す（"*" は未登録の作者のときの宛先）
  3. コミットして push する""")


if __name__ == "__main__":
    main()
