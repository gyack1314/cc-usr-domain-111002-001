"""采集模块的浏览器行为检查：真实驱动 Chromium 滚动并读取 DOM。

覆盖正常收齐、同名设备区分、中断恢复、滚动中换序拒绝拼接、
重试有界以及恢复时位置编号变化的识别。运维入口 /__control 只用于
来源场景调度，采集模块本身不接触内部或运维接口。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from collector import Collector, Config
from portal.__main__ import running

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "web" / "data.json").read_text(encoding="utf-8"))


def expected_ids(category, sort, rows=None):
    """按门户同样的过滤与排序规则计算期望编号序列。"""
    values = [dict(r) for r in (rows or DATA["rows"]) if category == "all" or r["category"] == category]
    if sort == "id":
        values.sort(key=lambda r: r["id"])
    else:
        values.sort(key=lambda r: (r["name"], r["id"]))
    return [r["id"] for r in values]


class CollectorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.portal_state = self.server.__enter__()
        self.tmp = Path(tempfile.mkdtemp(prefix="collector-check-"))

    def tearDown(self):
        self.server.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, name, **kwargs):
        options = dict(url=self.base, category="all", sort="id", state_dir=self.tmp / name)
        options.update(kwargs)
        return Config(**options)

    def collect(self, config):
        return Collector(config, browser=self.browser).run()

    def control(self, fields):
        request = Request(
            self.base + "/__control",
            data=json.dumps(fields).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            return json.load(response)

    def result_of(self, config):
        return json.loads((config.state_dir / "result.json").read_text(encoding="utf-8"))

    def run_json_of(self, config):
        return json.loads((config.state_dir / "run.json").read_text(encoding="utf-8"))

    def assert_evidence_sound(self, config, result):
        """覆盖证明逐行可核对：每个逻辑行号对应编号与观察视口，证据文件真实存在。"""
        proof = {p["row_index"]: p for p in result["coverage_proof"]}
        self.assertEqual(set(range(1, result["total_rows"] + 1)), set(proof))
        for entry in proof.values():
            viewport = json.loads(
                (config.state_dir / entry["viewport_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(result["query"]["signature"], viewport["query"])
            self.assertEqual(result["revision"], viewport["revision"])
            hits = [r for r in viewport["rows"] if r["row_index"] == entry["row_index"]]
            self.assertEqual(1, len(hits))
            self.assertEqual(entry["id"], hits[0]["id"])
            screenshot = config.state_dir / entry["viewport_file"].rsplit("/", 1)[0] / viewport["screenshot"]
            self.assertTrue(screenshot.stat().st_size > 0)

    def test_collect_all_by_id_completes(self):
        outcome = self.collect(self.config("run"))
        self.assertEqual("completed", outcome["status"])
        config = self.config("run")
        result = self.result_of(config)
        self.assertEqual(23, result["total_rows"])
        self.assertEqual(23, result["collected_rows"])
        self.assertEqual(list(range(1, 24)), [r["row_index"] for r in result["rows"]])
        self.assertEqual(expected_ids("all", "id"), [r["id"] for r in result["rows"]])
        # 同名设备不合并：16 台“巡检设备”编号各不相同
        self.assertEqual(16, [r["name"] for r in result["rows"]].count("巡检设备"))
        self.assertEqual(23, len({r["id"] for r in result["rows"]}))
        self.assertEqual(1, result["revision"])
        for row in result["rows"]:
            self.assertEqual(1, row["evidence"]["revision"])
            self.assertEqual("category=all;sort=id", row["evidence"]["query"])
        self.assert_evidence_sound(config, result)

    def test_collect_all_by_name_completes(self):
        outcome = self.collect(self.config("run", sort="name"))
        self.assertEqual("completed", outcome["status"])
        result = self.result_of(self.config("run", sort="name"))
        self.assertEqual(expected_ids("all", "name"), [r["id"] for r in result["rows"]])
        self.assertEqual(list(range(1, 24)), [r["row_index"] for r in result["rows"]])

    def test_collect_lift_by_name_with_wheel(self):
        # 用鼠标滚轮真实滚动网格
        outcome = self.collect(self.config("run", category="lift", sort="name", scroll_mode="wheel"))
        self.assertEqual("completed", outcome["status"])
        result = self.result_of(self.config("run", category="lift", sort="name"))
        self.assertEqual(7, result["collected_rows"])
        self.assertEqual(expected_ids("lift", "name"), [r["id"] for r in result["rows"]])
        self.assertEqual(["举升设备"] * 7, [r["name"] for r in result["rows"]])

    def test_interrupt_then_resume_completes(self):
        first = self.collect(self.config("run", stop_after_viewports=2))
        self.assertEqual("interrupted", first["status"])
        run_json = self.run_json_of(self.config("run"))
        self.assertEqual("interrupted", run_json["status"])
        saved = json.loads(
            (self.tmp / "run" / "attempts" / "001" / "coverage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(10, len(saved["rows"]))

        second = self.collect(self.config("run", resume=True))
        self.assertEqual("completed", second["status"])
        config = self.config("run")
        result = self.result_of(config)
        self.assertEqual(23, result["collected_rows"])
        self.assertEqual(expected_ids("all", "id"), [r["id"] for r in result["rows"]])
        # 两次尝试的行证据拼接：前 10 行来自尝试 1，后续行来自尝试 2
        by_attempt = {r["evidence"]["attempt"] for r in result["rows"]}
        self.assertEqual({1, 2}, by_attempt)
        for row in result["rows"][:10]:
            self.assertEqual(1, row["evidence"]["attempt"])
        # 恢复时先核对了已有覆盖：尝试 2 存在 verify 视口
        verify_viewports = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (self.tmp / "run" / "attempts" / "002" / "viewports").glob("*.json")
            if json.loads(p.read_text(encoding="utf-8"))["purpose"] == "verify"
        ]
        self.assertTrue(verify_viewports)
        self.assertTrue(any(v["confirmed_rows"] for v in verify_viewports))
        self.assert_evidence_sound(config, result)

    def test_reorder_aborts_and_recollects_without_splicing(self):
        self.control({"phase": "reorder"})
        outcome = self.collect(self.config("run"))
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(1, outcome["retries_used"])
        config = self.config("run")
        run_json = self.run_json_of(config)
        self.assertEqual(2, len(run_json["attempts"]))
        first, second = run_json["attempts"]
        self.assertEqual("aborted", first["status"])
        self.assertIn("修订", first["reason"])
        self.assertEqual(1, first["plan"]["revision"])
        self.assertEqual("completed", second["status"])
        self.assertEqual(2, second["plan"]["revision"])
        # 旧修订的覆盖被隔离保留，不并入结果
        saved = json.loads(
            (self.tmp / "run" / "attempts" / "001" / "coverage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, saved["plan"]["revision"])
        self.assertTrue(saved["rows"])
        result = self.result_of(config)
        self.assertEqual(2, result["revision"])
        ids = [r["id"] for r in result["rows"]]
        self.assertEqual(expected_ids("all", "id")[:-1] + ["DV-NEW"], ids)
        self.assertIn("DV-NEW", ids)
        self.assertNotIn("DV-023", ids)
        # 结果中每一行都在修订 2 下重新观察，没有拼入旧修订的行
        for row in result["rows"]:
            self.assertEqual(2, row["evidence"]["revision"])
            self.assertEqual(2, row["evidence"]["attempt"])
        self.assert_evidence_sound(config, result)

    def test_persistent_change_exhausts_retries(self):
        self.control({"phase": "reorder"})
        outcome = self.collect(self.config("run", max_retries=0))
        self.assertEqual("failed", outcome["status"])
        run_json = self.run_json_of(self.config("run"))
        self.assertEqual("failed", run_json["status"])
        self.assertIn("持续变化", run_json["failure_reason"])
        self.assertEqual(1, len(run_json["attempts"]))

    def test_resume_detects_position_id_change(self):
        first = self.collect(self.config("run", stop_after_viewports=2))
        self.assertEqual("interrupted", first["status"])
        self.server.__exit__(None, None, None)
        # 换一份来源数据：DV-001 改为 DV-099（同一行号位置编号变化）
        import portal.__main__ as portal_main

        web_copy = self.tmp / "web"
        shutil.copytree(ROOT / "web", web_copy)
        changed = [dict(r, id="DV-099") if r["id"] == "DV-001" else dict(r) for r in DATA["rows"]]
        (web_copy / "data.json").write_text(
            json.dumps({"rows": changed}, ensure_ascii=False), encoding="utf-8"
        )
        original_web = portal_main.WEB
        portal_main.WEB = web_copy
        try:
            self.server = running()
            self.base, self.portal_state = self.server.__enter__()
            second = self.collect(self.config("run", resume=True, url=self.base))
        finally:
            portal_main.WEB = original_web
        self.assertEqual("completed", second["status"])
        config = self.config("run")
        result = self.result_of(config)
        ids = [r["id"] for r in result["rows"]]
        self.assertEqual(expected_ids("all", "id", rows=changed), ids)
        self.assertIn("DV-099", ids)
        self.assertNotIn("DV-001", ids)
        # 核对发现位置编号变化，旧覆盖未拼入：前 10 行全部重新观察
        run_json = self.run_json_of(config)
        notes = json.dumps(run_json["attempts"], ensure_ascii=False)
        self.assertIn("编号变化", notes)
        for row in result["rows"][:10]:
            self.assertEqual(2, row["evidence"]["attempt"])


if __name__ == "__main__":
    unittest.main()
