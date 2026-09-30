#!/usr/bin/env python3
"""GitHub Actions の進捗と結果を、1つの Discord メッセージへ書き換えながら通知する。

使い方（action.yml が PATH と環境変数を整える）:
  morn-build-notify start           開始メッセージを送り、message_id を GITHUB_OUTPUT へ書く
  morn-build-notify update <文言>   同じメッセージを進捗文言へ書き換える
  morn-build-notify finish          結果・工程・失敗ログ・メンション付きの埋め込みへ置き換える

通知の失敗はビルドを止めない。警告だけ出して終了コード 0 で抜ける。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

MESSAGE_ID = re.compile(r"[0-9]{17,20}\Z")
USER_AGENT = "MornBuildNotify/1.0"
COLORS = {"success": 5763719, "failure": 15548997, "cancelled": 9807270, "skipped": 9807270}
TITLES = {"success": "✅ ビルド成功", "failure": "❌ ビルド失敗",
          "cancelled": "⏹️ ビルド中止", "skipped": "⏭️ ビルドスキップ"}
STATUS_LABELS = {
    "success": "✅ 成功", "failure": "❌ 失敗", "skipped": "⏭️ スキップ",
    "cancelled": "🚫 キャンセル", "timed_out": "⏰ 時間切れ",
    "in_progress": "🔄 実行中", "queued": "⏳ 待機中",
    "neutral": "➖ 中立", "action_required": "⚠️ 要対応",
}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[^ ]+Z\s*")
ERROR = re.compile(r"fatal|error|script error|exception|traceback|missing|not found|failed", re.I)
ACTIONABLE = re.compile(r"##\[error\]|fatal:|script error|exception|traceback", re.I)
GENERIC_EXIT = re.compile(r"exit(?:ed|ing)?\s+(?:with\s+)?(?:code\s+)?[1-9]\d*\b", re.I)
ANNOTATION = re.compile(r"##\[error\](.*)|::error(?:\s[^:]*)?::(.*)")


def warn(message):
    print(f"::warning::{message}", file=sys.stderr)


def load_config():
    path = Path(os.environ.get("MORN_BUILD_NOTIFY_CONFIG") or ".github/morn-build-notify.json")
    if not path.is_file():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("設定ファイルはJSONオブジェクトにしてください")
    return config


# --- Discord 送信 ---------------------------------------------------------

def webhook_url():
    parts = urlsplit(os.environ["MORN_BUILD_NOTIFY_WEBHOOK"])
    if (parts.scheme != "https" or parts.hostname not in {"discord.com", "discordapp.com"}
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[^/]+", parts.path)):
        raise ValueError
    return parts


def request(method, payload, *, wait=False, message_id=None):
    parts = webhook_url()
    path = parts.path + (f"/messages/{message_id}" if message_id else "")
    query = "&".join(q for q in (parts.query, "wait=true" if wait else "") if q)
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = Request(urlunsplit((parts.scheme, parts.netloc, path, query, "")), data=body, method=method,
                  headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urlopen(req, timeout=10) as response:
        return json.loads(response.read()) if response.status != 204 else {}


def message_id():
    value = os.environ.get("MORN_BUILD_NOTIFY_MESSAGE_ID", "")
    return value if MESSAGE_ID.fullmatch(value) else None


def post(payload):
    sent = request("POST", payload, wait=True)["id"]
    if not isinstance(sent, str) or not MESSAGE_ID.fullmatch(sent):
        raise ValueError
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"message_id={sent}\n")


def progress_content(config, phase):
    repo = os.environ["GITHUB_REPOSITORY"]
    name = config.get("name") or repo.split("/")[-1]
    url = (f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
           f"/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}")
    sha = (os.environ.get("MORN_BUILD_NOTIFY_SHA") or os.environ["GITHUB_SHA"])[:7]
    return f"{name} ビルド進捗\n{repo}@{sha}\n{phase[:1500]}\n{url}"


# --- GitHub API -----------------------------------------------------------

def gh(*args):
    return subprocess.run(["gh", "api", "--allow-escape-sequences", *args], capture_output=True,
                          text=True, timeout=20, check=True).stdout


def jobs():
    endpoint = (f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                f"/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}/jobs")
    return [job for page in json.loads(gh("--paginate", "--slurp", endpoint)) for job in page["jobs"]]


def sha():
    return os.environ.get("MORN_BUILD_NOTIFY_SHA") or os.environ["GITHUB_SHA"]


def commit_info():
    """コミット件名と作者の GitHub ログイン名。取れない部分は空。"""
    # push イベントの件名は github.sha のもの。別のコミットを載せるときは API から取る。
    title = os.environ.get("MORN_BUILD_NOTIFY_COMMIT_TITLE", "") if sha() == os.environ["GITHUB_SHA"] else ""
    author = ""
    try:
        commit = json.loads(gh(f"repos/{os.environ['GITHUB_REPOSITORY']}/commits/{sha()}"))
        title = title or commit["commit"]["message"]
        author = (commit.get("author") or {}).get("login") or ""
    except (KeyError, TypeError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        warn("コミット情報を取得できませんでした")
    return title or "コミット名を取得できませんでした", author


# --- 埋め込みの組み立て ---------------------------------------------------

def derive_status(needs):
    results = [job.get("result") for job in needs.values() if isinstance(job, dict)]
    if "cancelled" in results:
        return "cancelled"
    if "failure" in results:
        return "failure"
    return "success" if results and all(r == "success" for r in results) else "skipped"


def status_line(label, result):
    icon, text = STATUS_LABELS.get(result, "❔ 不明").split(" ", 1)
    return f"{icon} {label}：{text}"


def result_fields(config, run_jobs):
    """config の results（段ごとの job/step 対応）か、既定では job ごとの結論を並べる。"""
    def conclusion(job_name, step_name=None):
        job = next((j for j in run_jobs if j.get("name") == job_name), {})
        if step_name is None:
            return job.get("conclusion") or job.get("status") or "skipped"
        return next((s.get("conclusion") or s.get("status") or "unknown" for s in job.get("steps", [])
                     if s.get("name") == step_name), "skipped")

    groups = config.get("results")
    if not groups:
        lines = [status_line(j.get("name", "?"), j.get("conclusion")) for j in run_jobs
                 if j.get("status") == "completed"]
        return [{"name": "処理結果", "value": "\n".join(lines), "inline": False}] if lines else []
    return [{"name": group["name"],
             "value": "\n".join(status_line(item["label"], conclusion(item["job"], item.get("step")))
                                for item in group["items"]),
             "inline": False}
            for group in groups]


def room(embed, fields, name):
    used = sum(len(str(embed.get(key, ""))) for key in ("title", "description"))
    used += sum(len(str(field.get(key, ""))) for field in fields for key in ("name", "value"))
    return max(0, min(1024, 6000 - used - len(name)))


def extra_fields(text):
    """`名前: 値` を1行1項目で受け取る。値の `\\n` は改行にし、値が空の行は出さない。"""
    fields = []
    for line in text.splitlines():
        name, sep, value = line.partition(": ")
        value = value.replace("\\n", "\n").strip()
        if sep and name.strip() and value:
            fields.append({"name": name.strip()[:256], "value": value[:1024]})
    return fields


def clean(line):
    line = ANSI.sub("", line).replace("\r", "")
    line = TIMESTAMP.sub("", line).strip()
    return re.sub(r"^##\[[^]]+\]\s*", "", line)


def excerpt(log, limit=860):
    raw = log.splitlines()
    lines = [line for line in map(clean, raw) if line]
    actionable = []
    for index, line in enumerate(raw):
        if ACTIONABLE.search(line):
            detail = clean(line)
            if index + 1 < len(raw) and clean(raw[index + 1]).startswith("at:"):
                detail += "\n" + clean(raw[index + 1])
            actionable.append(detail)
    useful = actionable or [line for line in lines if ERROR.search(line)]
    useful = [line for line in useful if not GENERIC_EXIT.search(line)] or useful
    useful.sort(key=len, reverse=True)
    selected = []
    for line in useful or list(reversed(lines)):
        if line not in selected:
            selected.append(line)
        if len(selected) == 3:
            break
    if not selected:
        return "ログにエラー詳細がありません。"
    text = "\n".join(selected)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def annotations(log):
    """`::error` で出したエラーを、ログの順に重複なく返す。終了コードだけの行は除く。"""
    found = []
    for raw in log.splitlines():
        match = ANNOTATION.search(ANSI.sub("", raw))
        if not match:
            continue
        message = (match[1] if match[1] is not None else match[2]).strip()
        message = message.replace("%0D", "").replace("%0A", " ").replace("%25", "%")[:300]
        if message and not GENERIC_EXIT.search(message) and message not in found:
            found.append(message)
    return found


def bounded(details, limit):
    if details and limit and len(details[0]) > limit:
        return details[0][:limit - 1] + "…"
    lines = []
    for detail in details:
        omitted = len(details) - len(lines) - 1
        suffix = f"\nほか{omitted}件はActionsログ参照" if omitted else ""
        if len("\n".join([*lines, detail]) + suffix) > limit:
            break
        lines.append(detail)
    omitted = len(details) - len(lines)
    if omitted:
        suffix = f"ほか{omitted}件はActionsログ参照"
        while lines and len("\n".join([*lines, suffix])) > limit:
            lines.pop()
            omitted += 1
            suffix = f"ほか{omitted}件はActionsログ参照"
        if len("\n".join([*lines, suffix])) <= limit:
            lines.append(suffix)
    return "\n".join(lines)


def error_details(run_jobs):
    details = []
    for job in run_jobs:
        if job.get("conclusion") not in {"failure", "timed_out"}:
            continue
        log = gh(f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/jobs/{job['id']}/logs")
        found = annotations(log)
        if found:
            details.extend(found)
        else:
            step = next((s.get("name") for s in job.get("steps", [])
                         if s.get("conclusion") in {"failure", "timed_out"}), "失敗ステップ")
            details.append(f"{job.get('name', '失敗ジョブ')} / {step}:\n{excerpt(log)}")
    return details


def mention(config, author):
    users = {login.lower(): user for login, user in (config.get("mentions") or {}).items()}
    user_id = str(users.get(author.lower(), users.get("*", "")))
    if not MESSAGE_ID.fullmatch(user_id):
        return {}
    text = config.get("mention-message", "ビルドが失敗しました。確認をお願いします。")
    return {"content": f"<@{user_id}> {text}", "allowed_mentions": {"parse": [], "users": [user_id]}}


def build_payload(config):
    needs = json.loads(os.environ.get("MORN_BUILD_NOTIFY_NEEDS") or "{}")
    status = os.environ.get("MORN_BUILD_NOTIFY_STATUS") or derive_status(needs)
    if status not in COLORS:
        warn(f"status は {'/'.join(COLORS)} のいずれかにしてください: {status}")
        status = "failure"
    repo = os.environ["GITHUB_REPOSITORY"]
    title, author = commit_info()
    subject = title.split("\n")[0][:350].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    embed = {"title": f"{({**TITLES, **config.get('titles', {})})[status]}: {repo}", "color": COLORS[status]}
    fields = [{"name": "コミット", "value": f"[{subject}](https://github.com/{repo}/commit/{sha()})"}]
    try:
        run_jobs = jobs()
    except (KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        warn(f"工程結果を取得できません: {type(error).__name__}")
        run_jobs = None
    if run_jobs is None:
        summary = " / ".join(f"{name}: {job.get('result')}" for name, job in needs.items())
        fields.append({"name": "処理結果", "value": ("GitHub APIから工程結果を取得できませんでした"
                                                   + (f"（{summary}）" if summary else ""))[:1024]})
    else:
        for field in result_fields(config, run_jobs):
            if len(fields) >= 20 or len(field["value"]) > room(embed, fields, field["name"]):
                break
            fields.append(field)
    log = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    fields.append({"name": "ログ", "value": f"[GitHub Actionsを開く]({log})"})
    for field in extra_fields(os.environ.get("MORN_BUILD_NOTIFY_FIELDS", "")):
        if len(fields) < 24 and len(field["value"]) <= room(embed, fields, field["name"]):
            fields.append(field)
    payload = {"embeds": [embed]}
    if config.get("username"):
        payload["username"] = config["username"]
    if status == "failure":
        if config.get("errors", True):
            details = []
            if run_jobs is not None:
                try:
                    details = error_details(run_jobs)
                except (KeyError, OSError, subprocess.SubprocessError) as error:
                    warn(f"ビルドエラー詳細を取得できません: {type(error).__name__}")
            details = details or ["GitHub API unavailable: ビルドエラー詳細を取得できませんでした。"]
            value = bounded(details, room(embed, fields, "ビルドエラー詳細"))
            if value:
                fields.append({"name": "ビルドエラー詳細", "value": value, "inline": False})
        payload.update(mention(config, author))
    embed["fields"] = fields
    return payload


# --- 入口 -----------------------------------------------------------------

def main(args):
    try:
        command = args[0] if args else ""
        config = load_config()
        if command == "start" and len(args) == 1:
            post({"content": progress_content(config, "ビルド開始"), "allowed_mentions": {"parse": []}})
        elif command == "update" and len(args) >= 2:
            if not message_id():
                warn("進捗通知のメッセージIDがありません")
                return
            request("PATCH", {"content": progress_content(config, " ".join(args[1:])), "embeds": [],
                              "allowed_mentions": {"parse": []}}, message_id=message_id())
        elif command == "finish" and len(args) == 1:
            payload = build_payload(config)
            payload.setdefault("content", "")
            payload.setdefault("allowed_mentions", {"parse": []})
            if message_id():
                request("PATCH", payload, message_id=message_id())
            else:
                post(payload)
        elif command == "payload" and len(args) == 1:
            # 送信せずに組み立て結果だけを見る（確認・テスト用）。
            print(json.dumps(build_payload(config), ensure_ascii=False, indent=2))
        else:
            warn("引数は start / update <文言> / finish / payload のいずれかです")
    except Exception:
        # urllib や JSON の例外には Webhook URL や応答本文が含まれうるので出さない。
        warn("Discord通知に失敗しました（処理は継続します）")


if __name__ == "__main__":
    main(sys.argv[1:])
