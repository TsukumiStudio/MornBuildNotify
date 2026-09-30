"""外部へ送信せずに MornBuildNotify を検査する。 python3 -m unittest"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import morn_build_notify as notify

WEBHOOK = "https://discord.com/api/webhooks/12345678901234567/secret"
USER = "433012031512248320"


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        env = patch.dict(os.environ, {
            "MORN_BUILD_NOTIFY_WEBHOOK": WEBHOOK, "MORN_BUILD_NOTIFY_CONFIG": str(self.config),
            "GITHUB_OUTPUT": str(Path(self.temp.name) / "output"),
            "GITHUB_REPOSITORY": "org/repo", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_SHA": "a" * 40,
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("MORN_BUILD_NOTIFY_MESSAGE_ID", "MORN_BUILD_NOTIFY_STATUS", "MORN_BUILD_NOTIFY_NEEDS",
                    "MORN_BUILD_NOTIFY_FIELDS", "MORN_BUILD_NOTIFY_SHA", "MORN_BUILD_NOTIFY_COMMIT_TITLE"):
            os.environ.pop(key, None)

    def send_all(self, *commands):
        calls = []

        def send(req, timeout):
            calls.append(req)
            return Response(b'{"id":"123456789012345678"}' if req.method == "POST" else b"{}")

        with patch.object(notify, "urlopen", side_effect=send):
            for command in commands:
                notify.main(command)
        return calls

    def test_start_update_finish_reuse_one_message(self):
        # Without the GitHub API the progress falls back to the plain text message.
        self.config.write_text(json.dumps({"name": "Game"}))
        os.environ["RUNNER_TEMP"] = self.temp.name
        unavailable = patch.object(notify, "gh", side_effect=OSError)
        unavailable.start()
        self.addCleanup(unavailable.stop)
        calls = self.send_all(["start"])
        os.environ["MORN_BUILD_NOTIFY_MESSAGE_ID"] = Path(os.environ["GITHUB_OUTPUT"]).read_text().strip().split("=")[1]
        with patch.object(notify, "build_payload", return_value={"embeds": [{"title": "完了"}]}):
            calls += self.send_all(["update", "Web", "書き出し中"], ["finish"])
        self.assertEqual([c.method for c in calls], ["POST", "PATCH", "PATCH"])
        self.assertIn("wait=true", calls[0].full_url)
        self.assertTrue(all("/messages/123456789012345678" in c.full_url for c in calls[1:]))
        self.assertIn("Game ビルド進捗", json.loads(calls[0].data)["content"])
        update = json.loads(calls[1].data)
        self.assertIn("Web 書き出し中", update["content"])
        self.assertEqual((update["embeds"], update["allowed_mentions"]), ([], {"parse": []}))
        self.assertEqual(json.loads(calls[2].data),
                         {"embeds": [{"title": "完了"}], "content": "", "allowed_mentions": {"parse": []}})

    def progress_jobs(self):
        return [{"id": 7, "name": "build", "status": "in_progress", "runner_name": "mac", "steps": [
            {"name": "Checkout", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-01T00:00:00Z", "completed_at": "2026-10-01T00:00:05Z"},
            {"name": "Export", "status": "in_progress", "started_at": "2026-10-01T00:00:05Z"},
            {"name": "Upload", "status": "queued"}]}]

    def test_progress_uses_final_layout_with_running_bar(self):
        from datetime import datetime, timezone
        config = {"results": [{"name": "工程", "items": [
            {"label": "準備", "job": "build", "step": "Checkout"},
            {"label": "書き出し", "job": "build", "step": "Export"},
            {"label": "配置", "job": "build", "step": "Upload"}]}]}
        now = datetime(2026, 10, 1, 0, 0, 20, tzinfo=timezone.utc)
        payload = notify.progress_payload(config, self.progress_jobs(), {("build", "Export"): 30.0},
                                          "書き出し中", "[修正] を入れる", now)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "🔄 ビルド中（1/3）: org/repo")
        self.assertEqual(embed["description"], "書き出し中")
        self.assertEqual([f["name"] for f in embed["fields"]], ["コミット", "工程", "ログ"])
        self.assertIn("\\[修正\\]", embed["fields"][0]["value"])
        lines = embed["fields"][1]["value"].split("\n")
        self.assertEqual(lines[0], "✅ 準備：成功")
        self.assertEqual(lines[1], "▶️ 書き出し：▰▰▰▰▰▰▱▱▱▱▱▱ 50%")
        self.assertEqual(lines[2], "⬜ 配置：待機中")

    def test_bar_caps_and_flows_without_estimate(self):
        self.assertTrue(notify.bar(100, 10).endswith("95%"))
        first, later = notify.bar(0, None), notify.bar(notify.TICK_SECONDS, None)
        self.assertNotEqual(first.split()[0], later.split()[0])
        self.assertEqual(first.count("▰"), 1)

    def test_update_sends_progress_embed_and_keeps_phase(self):
        os.environ["MORN_BUILD_NOTIFY_MESSAGE_ID"] = "123456789012345678"
        os.environ["RUNNER_TEMP"] = self.temp.name
        with patch.object(notify, "jobs", return_value=self.progress_jobs()), \
                patch.object(notify, "estimates", return_value={}), \
                patch.object(notify, "commit_info", return_value=("件名", "someone")):
            calls = self.send_all(["update", "Web", "書き出し中"])
        body = json.loads(calls[0].data)
        self.assertEqual((calls[0].method, body["content"]), ("PATCH", ""))
        self.assertEqual(body["embeds"][0]["description"], "Web 書き出し中")
        self.assertEqual(notify.read_phase(), "Web 書き出し中")

    def test_tick_stops_when_own_job_completes(self):
        os.environ.update(MORN_BUILD_NOTIFY_MESSAGE_ID="123456789012345678", RUNNER_NAME="mac",
                          RUNNER_TEMP=self.temp.name)
        running, finished = self.progress_jobs(), self.progress_jobs()
        finished[0]["status"] = "completed"
        with patch.object(notify, "jobs", side_effect=[running, finished]), \
                patch.object(notify, "estimates", return_value={}), \
                patch.object(notify, "commit_info", return_value=("件名", "someone")), \
                patch.object(notify.time, "sleep"):
            calls = self.send_all(["tick"])
        self.assertEqual([c.method for c in calls], ["PATCH"])

    def test_finish_without_message_posts_new_one(self):
        with patch.object(notify, "build_payload", return_value={"embeds": []}):
            calls = self.send_all(["finish"])
        self.assertEqual(calls[0].method, "POST")

    def test_failures_are_nonfatal_and_hide_webhook(self):
        with patch.object(notify, "urlopen", side_effect=OSError(WEBHOOK)), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            notify.main(["start"])
        self.assertIn("::warning::", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())
        os.environ["MORN_BUILD_NOTIFY_WEBHOOK"] = "https://example.com/api/webhooks/1/x"
        with patch.object(notify, "urlopen") as send, patch("sys.stderr", new_callable=io.StringIO):
            notify.main(["start"])
        send.assert_not_called()

    def payload(self, status, run_jobs, log="", fields=""):
        self.config.write_text(json.dumps({
            "username": "bot", "titles": {"failure": "❌ 公開失敗"},
            "mentions": {"TsuyoshiNakami": USER, "*": "703992652391120911"},
            "results": [{"name": "Phase 1 ─ 検査", "items": [
                {"label": "準備", "job": "build", "step": "Checkout"},
                {"label": "保存", "job": "build", "step": "検査 / 保存"},
                {"label": "公開", "job": "publish"}]}],
        }))
        os.environ.update(MORN_BUILD_NOTIFY_STATUS=status, MORN_BUILD_NOTIFY_FIELDS=fields)
        with patch.object(notify, "jobs", return_value=run_jobs), patch.object(notify, "gh", return_value=log), \
                patch.object(notify, "commit_info", return_value=("[通知]を直す\n本文", "tsuyoshinakami")):
            return notify.build_payload(notify.load_config())

    def test_failure_payload_has_results_errors_and_mention(self):
        run_jobs = [{"id": 1, "name": "build", "status": "completed", "conclusion": "failure", "steps": [
                        {"name": "Checkout", "conclusion": "success"},
                        {"name": "検査 / 保存", "conclusion": "failure"}]},
                    {"id": 2, "name": "publish", "status": "completed", "conclusion": "skipped"}]
        log = ("2026-09-17T13:36:59Z ##[error][save/a] 失敗(exit=1); 保存値が違います\n"
               "##[error][save/a] 失敗(exit=1); 保存値が違います\n"
               "##[error]Process completed with exit code 1.\n")
        payload = self.payload("failure", run_jobs, log,
                               "遊ぶ: [ここ](https://x)\\n7日で消える\n空: \n壊れた行")
        embed = payload["embeds"][0]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual((embed["title"], embed["color"]), ("❌ 公開失敗: org/repo", 15548997))
        self.assertEqual(fields["コミット"], "[\\[通知\\]を直す](https://github.com/org/repo/commit/" + "a" * 40 + ")")
        self.assertEqual(fields["Phase 1 ─ 検査"], "✅ 準備：成功\n❌ 保存：失敗\n⏭️ 公開：スキップ")
        self.assertEqual(fields["遊ぶ"], "[ここ](https://x)\n7日で消える")
        self.assertNotIn("空", fields)
        self.assertEqual(fields["ビルドエラー詳細"], "[save/a] 失敗(exit=1); 保存値が違います")
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "users": [USER]})
        self.assertEqual(payload["username"], "bot")
        self.assertLessEqual(sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]), 6000)

    def test_success_payload_has_no_errors_or_mention(self):
        payload = self.payload("success", [])
        self.assertNotIn("content", payload)
        self.assertNotIn("ビルドエラー詳細", [f["name"] for f in payload["embeds"][0]["fields"]])
        self.assertTrue(payload["embeds"][0]["title"].startswith("✅ ビルド成功"))

    def test_log_excerpt_used_when_no_annotation(self):
        run_jobs = [{"id": 1, "name": "build", "status": "completed", "conclusion": "failure",
                     "steps": [{"name": "Checkout", "conclusion": "failure"}]}]
        log = "\x1b[31mfatal: reference is not a tree: deadbeef\x1b[0m\n##[error]Process completed with exit code 128."
        value = {f["name"]: f["value"] for f in self.payload("failure", run_jobs, log)["embeds"][0]["fields"]}
        self.assertEqual(value["ビルドエラー詳細"], "build / Checkout:\nfatal: reference is not a tree: deadbeef")

    def test_api_failure_keeps_notification(self):
        os.environ["MORN_BUILD_NOTIFY_NEEDS"] = json.dumps({"build": {"result": "failure"}})
        self.config.write_text("{}")
        with patch.object(notify, "jobs", side_effect=OSError), \
                patch.object(notify, "commit_info", return_value=("t", "")), patch("sys.stderr", new_callable=io.StringIO):
            payload = notify.build_payload(notify.load_config())
        fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
        self.assertIn("build: failure", fields["処理結果"])
        self.assertIn("API unavailable", fields["ビルドエラー詳細"])
        self.assertNotIn("content", payload)  # mentions 未設定なら誰も呼ばない

    def test_default_results_list_completed_jobs(self):
        self.assertEqual(notify.result_fields({}, [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "notify", "status": "in_progress", "conclusion": None}]),
            [{"name": "処理結果", "value": "✅ build：成功", "inline": False}])

    def test_status_from_needs(self):
        self.assertEqual(notify.derive_status({"a": {"result": "success"}, "b": {"result": "cancelled"}}), "cancelled")
        self.assertEqual(notify.derive_status({"a": {"result": "failure"}, "b": {"result": "skipped"}}), "failure")
        self.assertEqual(notify.derive_status({"a": {"result": "success"}}), "success")
        self.assertEqual(notify.derive_status({"a": {"result": "success"}, "b": {"result": "skipped"}}), "skipped")

    def test_bounded_errors(self):
        self.assertEqual(notify.bounded(["a: " + "x" * 100], 10), "a: xxxxxx…")
        value = notify.bounded(["save/a: cause", "wonder/b: cause", "novel/c: cause"], 38)
        self.assertIn("save/a: cause", value)
        self.assertIn("ほか2件はActionsログ参照", value)
        self.assertLessEqual(len(value), 38)


if __name__ == "__main__":
    unittest.main()
