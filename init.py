#!/usr/bin/env python3
"""MornBuildNotify の導入ウィザード。対象リポジトリのルートで実行する。

  python3 <(curl -fsSL https://raw.githubusercontent.com/TsukumiStudio/MornBuildNotify/v1/init.py)

質問に答えると、設定ファイル・workflow を書き出し、Webhook を GitHub Secret へ登録する。
既存のファイルは上書きしない（上書きするか毎回聞く）。
"""
import getpass
import json
import re
import subprocess
from pathlib import Path

ACTION = "TsukumiStudio/MornBuildNotify@v1"
CONFIG = Path(".github/morn-build-notify.json")

WORKFLOW = """name: {name}

on:
  push:
    branches: [{branch}]
  workflow_dispatch:

permissions:
  contents: read
  actions: read

jobs:
  notify-start:
    runs-on: ubuntu-latest
    outputs:
      message-id: ${{{{ steps.notify.outputs.message-id }}}}
    steps:
      - uses: actions/checkout@v4
      - id: notify
        uses: {action}
        with:
          webhook: ${{{{ secrets.{secret} }}}}
          mode: start

  build:
    needs: notify-start
    runs-on: {runner}
    steps:
      - uses: actions/checkout@v4
      # これ以降のstepで `morn-build-notify update "文言"` が使える。
      - uses: {action}
        with:
          webhook: ${{{{ secrets.{secret} }}}}
          message-id: ${{{{ needs.notify-start.outputs.message-id }}}}
      - name: Build
        run: |
          morn-build-notify update "ビルド中"
{command}

  notify:
    needs: [notify-start, build]
    if: ${{{{ always() }}}}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: {action}
        with:
          webhook: ${{{{ secrets.{secret} }}}}
          mode: finish
          message-id: ${{{{ needs.notify-start.outputs.message-id }}}}
          needs: ${{{{ toJSON(needs) }}}}
          # 成果物のリンクなどを足すときは 1行1項目の `名前: 値`。値が空の行は出ない。
          # fields: |
          #   遊ぶ: ${{{{ needs.build.outputs.url && format('[このビルドを遊ぶ]({{0}})', needs.build.outputs.url) || '' }}}}
"""


def ask(question, default=""):
    answer = input(f"{question}" + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def write(path, text):
    if path.exists() and not yes(f"{path} は既にあります。上書きしますか", False):
        print(f"  {path} はそのままにしました")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  {path} を書き出しました")


def mentions():
    print("失敗時に呼ぶ人を登録します。`GitHubログイン名 DiscordユーザーID` を1行ずつ。空行で終わり。")
    print("GitHubログイン名を * にすると、未登録の作者のときの宛先になります。")
    table = {}
    while line := input("  > ").strip():
        login, _, user = line.partition(" ")
        if login and re.fullmatch(r"[0-9]{17,20}", user.strip()):
            table[login] = user.strip()
        else:
            print("  形式が違います（例: matsufriends 703992652391120911）")
    return table


def main():
    if not Path(".git").exists():
        raise SystemExit("リポジトリのルートで実行してください")
    print("MornBuildNotify を導入します。\n")
    secret = ask("Webhook を入れる Secret 名", "DISCORD_WEBHOOK")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", secret):
        raise SystemExit("Secret 名は英大文字・数字・_ にしてください")
    config = {"name": ask("通知に出すプロジェクト名", Path.cwd().name)}
    if username := ask("Discord に出す送信者名（空なら Webhook の既定）"):
        config["username"] = username
    config["errors"] = yes("失敗時に失敗ログの抜粋を載せますか")
    if table := mentions():
        config["mentions"] = table
    write(CONFIG, json.dumps(config, ensure_ascii=False, indent=2) + "\n")

    if yes("ビルド用の workflow も書き出しますか"):
        file = ask("workflow のファイル名", "build.yml")
        command = ask("ビルドで実行するコマンド", "echo build")
        write(Path(".github/workflows") / file, WORKFLOW.format(
            name=ask("workflow 名", "Build"), branch=ask("通知するブランチ", "main"),
            runner=ask("ビルドを動かす runner", "ubuntu-latest"), action=ACTION, secret=secret,
            command="\n".join("          " + line for line in command.splitlines())))
    else:
        print(f"\n既存の workflow には README の「既存の workflow へ足す」を参考に {ACTION} を足してください。")

    if yes(f"Discord の Webhook URL を Secret {secret} へ登録しますか（gh を使います）"):
        url = getpass.getpass("  Webhook URL（入力は表示されません）: ").strip()
        if not re.fullmatch(r"https://(discord|discordapp)\.com/api/webhooks/[0-9]+/\S+", url):
            print("  Discord の Webhook URL ではないため登録しませんでした")
        elif subprocess.run(["gh", "secret", "set", secret], input=url, text=True).returncode == 0:
            print(f"  Secret {secret} を登録しました")
        else:
            print(f"  登録に失敗しました。あとで `gh secret set {secret}` を実行してください")
    print("\n完了しました。書き出したファイルをコミットして push すると通知が届きます。")


if __name__ == "__main__":
    main()
