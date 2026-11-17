"""시스템 자동화 page: schedule wording, crontab parsing, app-item filter, log whitelist. Run: python3 -m unittest discover tests"""
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_pilot import sysjobs as S


class ScheduleTest(unittest.TestCase):
    def test_launchd(self):
        self.assertEqual(S.launchd_schedule({"StartInterval": 3600}), "1시간마다")
        self.assertEqual(S.launchd_schedule({"StartInterval": 60, "RunAtLoad": True}), "1분마다 · 로그인 시")
        self.assertEqual(S.launchd_schedule({"StartCalendarInterval": {"Hour": 18, "Minute": 0}}), "매일 18:00")
        self.assertEqual(S.launchd_schedule({"StartCalendarInterval": [{"Weekday": 1, "Hour": 9, "Minute": 30}]}), "매주 월 09:30")
        self.assertEqual(S.launchd_schedule({"KeepAlive": {"SuccessfulExit": False}}), "종료 시 재시작")
        self.assertEqual(S.launchd_schedule({}), "요청 시")

    def test_cron(self):
        self.assertEqual(S.cron_schedule("*/5 * * * *"), "5분마다")
        self.assertEqual(S.cron_schedule("0 18 * * *"), "매일 18:00")
        self.assertEqual(S.cron_schedule("30 9 * * 1"), "매주 월 09:30")
        self.assertEqual(S.cron_schedule("@reboot"), "부팅 시")
        self.assertEqual(S.cron_schedule("0 9 1-7 * 1"), "0 9 1-7 * 1")

    def test_rrule(self):
        self.assertEqual(S.rrule_schedule("FREQ=WEEKLY;BYDAY=MO;BYHOUR=10;BYMINUTE=0"), "매주 월 10:00")
        self.assertEqual(S.rrule_schedule("FREQ=HOURLY;INTERVAL=2"), "2시간마다")
        self.assertEqual(S.rrule_schedule("FREQ=DAILY;BYHOUR=7;BYMINUTE=5"), "매일 07:05")


class CrontabTest(unittest.TestCase):
    def test_parse(self):
        jobs = S.parse_crontab(
            "# comment\nMAILTO=\"\"\nPATH=/usr/bin\n"
            "*/10 * * * * /usr/local/bin/backup.sh >> /tmp/backup.log 2>&1\n"
            "0 3 * * * python3 x.py > /dev/null 2>/tmp/x.err\n"
            "@reboot /opt/run.sh\nbroken line\n")
        self.assertEqual([j["name"] for j in jobs], ["backup.sh", "python3", "run.sh"])
        self.assertEqual(jobs[0]["schedule"], "10분마다")
        self.assertEqual([l["path"] for l in jobs[0]["logs"]], ["/tmp/backup.log"])
        self.assertEqual([(l["path"], l["label"]) for l in jobs[1]["logs"]], [("/tmp/x.err", "stderr")])
        self.assertEqual(jobs[2]["logs"], [])


class LaunchdTest(unittest.TestCase):
    def test_app_item(self):
        self.assertTrue(S.is_app_item(""))
        self.assertTrue(S.is_app_item("/tmp/home/Library/Application Support/G/Updater.app/Contents/MacOS/Updater"))
        self.assertFalse(S.is_app_item("/bin/zsh"))
        self.assertFalse(S.is_app_item("/opt/homebrew/bin/cloudflared"))

    def test_linux_lists_no_launchd(self):
        with mock.patch.object(S, "collect_launchd") as ld, mock.patch.object(S, "collect_cron", return_value=[]), \
                mock.patch.object(S, "collect_codex", return_value=[]):
            out = S.collect(mac=False)
        ld.assert_not_called()
        self.assertEqual(out["jobs"], [])
        self.assertIn("Linux에서는 지원하지 않아요", out["errors"][0])

    @unittest.skipUnless(sys.platform == "darwin", "launchd is macOS only")
    def test_collect_and_log_whitelist(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "job.log")
            log.write_text("".join(f"line {i}\n" for i in range(5000)))
            with open(Path(d, "com.me.job.plist"), "wb") as fh:
                plistlib.dump({"Label": "com.me.job", "ProgramArguments": ["/bin/zsh", "run.sh"],
                               "StartInterval": 3600, "StandardOutPath": str(log), "StandardErrorPath": str(log)}, fh)
            lc = {("launchctl", "list"): "PID\tStatus\tLabel\n-\t1\tcom.me.job\n",
                  ("launchctl", "print-disabled", f"gui/{os.getuid()}"): ""}
            with mock.patch.object(S, "LAUNCH_AGENTS", Path(d)), mock.patch.object(S, "CODEX_AUTOMATIONS", Path(d, "none")), \
                    mock.patch.object(S, "_run", lambda a: lc.get(tuple(a), "")):
                [job] = S.collect()["jobs"]
                self.assertEqual((job["status"], job["last_exit"], job["schedule"]), ("failed", 1, "1시간마다"))
                self.assertEqual([(l["label"], l["path"]) for l in job["logs"]], [("stdout+stderr", str(log))])
                got = S.read_log("launchd:com.me.job", 0, tail=100)
                self.assertTrue(got["truncated"])
                self.assertTrue(got["log"].endswith("line 4999\n"))
                self.assertTrue(got["log"].startswith("line "))
                self.assertIsNone(S.read_log("launchd:com.me.job", 1))
                self.assertIsNone(S.read_log("launchd:/etc/passwd", 0))


if __name__ == "__main__":
    unittest.main()
