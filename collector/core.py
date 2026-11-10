"""虚拟化设备台账的行覆盖采集核心。

只读取页面 DOM（元素属性、文本与布局）：不访问页面脚本变量，
不调用 /api、/__ 等内部或运维接口。查询签名、数据修订与总行数
全部来自表格元素的 data-query、data-revision 与 aria-rowcount，
逐行证据来自滚动容器内实际渲染的 [role=row] 行。

采集计划由（查询签名, 修订, 总行数）唯一确定；采集中任一变化都会
作废旧尝试并重建计划，重试次数有限，持续变化不会无限重跑。状态目录
保存游标（查询、修订、逐行覆盖与视口证据），支持中断后核对恢复：
恢复时先确认查询与修订，再抽样核对已有覆盖，无法确认就重采该查询。
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

GRID = "#grid"
ROW = '#grid [role="row"][aria-rowindex][data-id]'

# 原子读取当前网格状态与可见行：查询签名、修订、总行数、滚动位置、行数据。
SNAPSHOT_JS = (
    """() => {
  const g = document.querySelector('#grid');
  const total = +(g.getAttribute('aria-rowcount') || 0);
  const spacer = document.querySelector('#spacer');
  const spacerHeight = spacer ? parseFloat(spacer.style.height || '0') : 0;
  const summary = document.querySelector('#summary');
  return {
    query: g.dataset.query || '',
    revision: +(g.dataset.revision || 0),
    total,
    scrollTop: g.scrollTop,
    maxScroll: g.scrollHeight - g.clientHeight,
    stride: total > 0 && spacerHeight > 0 ? spacerHeight / total : 0,
    summary: summary ? summary.textContent.trim() : '',
    rows: [...document.querySelectorAll('"""
    + ROW
    + """')].map(e => ({
      row_index: +e.getAttribute('aria-rowindex'),
      id: e.dataset.id,
      spans: [...e.querySelectorAll('span')].map(s => s.textContent.trim())
    }))
  };
}"""
)

# 渲染完成条件：查询、修订或可见行集合任一变化即视为已重渲染。
RENDER_WAIT_JS = (
    """(sig) => {
  const g = document.querySelector('#grid');
  const rows = [...document.querySelectorAll('"""
    + ROW
    + """')]
    .map(e => e.getAttribute('aria-rowindex') + ':' + e.dataset.id).join(',');
  return (g.dataset.query + '|' + g.dataset.revision + '|' + rows) !== sig;
}"""
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class CollectError(Exception):
    """采集流程的可预期失败。"""


class RevisionChanged(CollectError):
    """采集中查询签名、修订或总行数发生变化（含同位置编号变化）。"""

    def __init__(self, plan: dict, seen: dict, detail: str = ""):
        self.plan = plan
        self.seen = seen
        message = (
            f"查询状态变化：计划(query={plan['query']}, 修订={plan['revision']}, 总行数={plan['total']})，"
            f"当前(query={seen['query']}, 修订={seen['revision']}, 总行数={seen['total']})"
        )
        if detail:
            message += f"；{detail}"
        super().__init__(message)


class UnreachableRows(CollectError):
    """滚动到边界仍无法让某些逻辑行进入渲染窗口。"""


class InterruptSimulated(CollectError):
    """按配置在指定视口数后模拟用户中断。"""


@dataclass
class Config:
    url: str
    category: str
    sort: str
    state_dir: Path
    max_retries: int = 3
    resume: bool = False
    verify_samples: int = 5
    stop_after_viewports: int | None = None
    scroll_mode: str = "auto"  # auto=直接设置 scrollTop；wheel=鼠标滚轮
    headless: bool = True
    executable_path: str | None = None
    viewport: dict = field(default_factory=lambda: {"width": 1280, "height": 900})
    logger: object | None = None  # 可选回调 fn(str)

    def __post_init__(self):
        self.state_dir = Path(self.state_dir)


def collect(config: Config, browser=None) -> dict:
    """单次入口：执行一轮采集（或恢复），返回运行结果字典。"""
    return Collector(config, browser=browser).run()


class Collector:
    def __init__(self, config: Config, browser=None):
        self.config = config
        self._browser = browser
        self._log = config.logger or (lambda _msg: None)

    # ---------- 运行与计划 ----------

    def run(self) -> dict:
        cfg = self.config
        run = self._load_or_init_run()
        if run["status"] == "completed":
            raise CollectError(f"状态目录 {cfg.state_dir} 已是完成状态，请换用新目录")
        driver = None
        browser = self._browser
        if browser is None:
            driver = sync_playwright().start()
            kwargs = {"headless": cfg.headless, "args": ["--no-sandbox"]}
            if cfg.executable_path:
                kwargs["executable_path"] = cfg.executable_path
            browser = driver.chromium.launch(**kwargs)
        context = browser.new_context(viewport=cfg.viewport)
        try:
            page = context.new_page()
            page.goto(cfg.url)
            page.wait_for_selector(ROW, state="visible", timeout=15000)
            self._apply_query(page)
            while True:
                snap = self._snapshot(page)
                attempt = self._new_attempt(run, snap)
                try:
                    coverage = self._adopt_saved_coverage(page, run, attempt, snap)
                    self._scroll_collect(page, attempt, snap, coverage)
                except RevisionChanged as exc:
                    self._close_attempt(run, attempt, "aborted", str(exc))
                    run["retries_used"] += 1
                    if run["retries_used"] > run["max_retries"]:
                        self._fail_run(run, f"查询状态持续变化，{run['max_retries']} 次重试后仍不稳定：{exc}")
                        return self._outcome(run)
                    continue
                except InterruptSimulated as exc:
                    self._close_attempt(run, attempt, "interrupted", str(exc))
                    run["status"] = "interrupted"
                    self._save_run(run)
                    self._log(f"已中断：{exc}")
                    return self._outcome(run)
                except UnreachableRows as exc:
                    self._close_attempt(run, attempt, "aborted", str(exc))
                    self._fail_run(run, str(exc))
                    return self._outcome(run)
                self._close_attempt(run, attempt, "completed", "")
                result = self._write_result(run, attempt)
                run["status"] = "completed"
                run["result"] = "result.json"
                self._save_run(run)
                out = self._outcome(run)
                out["result"] = result
                return out
        finally:
            context.close()
            if driver is not None:
                browser.close()
                driver.stop()

    def _outcome(self, run: dict) -> dict:
        return {
            "status": run["status"],
            "run_id": run["run_id"],
            "state_dir": str(self.config.state_dir),
            "attempts": len(run["attempts"]),
            "retries_used": run["retries_used"],
            "failure_reason": run.get("failure_reason"),
        }

    def _load_or_init_run(self) -> dict:
        cfg = self.config
        path = cfg.state_dir / "run.json"
        if path.exists():
            run = _read_json(path)
            if not cfg.resume:
                raise CollectError(
                    f"状态目录 {cfg.state_dir} 已存在未完成的运行（状态 {run['status']}），"
                    "请使用 --resume 接着做，或换用新目录"
                )
            if (run["category"], run["sort"]) != (cfg.category, cfg.sort):
                raise CollectError("恢复的运行与当前类别/排序不一致，请核对参数")
            self._log(f"恢复运行 {run['run_id']}（状态 {run['status']}，已有 {len(run['attempts'])} 次尝试）")
            return run
        if cfg.resume:
            raise CollectError(f"状态目录 {cfg.state_dir} 没有可恢复的运行")
        run = {
            "run_id": f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{cfg.category}-{cfg.sort}",
            "url": cfg.url,
            "category": cfg.category,
            "sort": cfg.sort,
            "status": "in_progress",
            "max_retries": cfg.max_retries,
            "retries_used": 0,
            "attempts": [],
            "result": None,
            "failure_reason": None,
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
        }
        self._save_run(run)
        return run

    def _save_run(self, run: dict) -> None:
        run["updated_at"] = _utcnow()
        _write_json(self.config.state_dir / "run.json", run)

    def _fail_run(self, run: dict, reason: str) -> None:
        run["status"] = "failed"
        run["failure_reason"] = reason
        self._save_run(run)
        self._log(f"运行失败：{reason}")

    def _new_attempt(self, run: dict, snap: dict) -> dict:
        n = len(run["attempts"]) + 1
        attempt_dir = self.config.state_dir / "attempts" / f"{n:03d}"
        (attempt_dir / "viewports").mkdir(parents=True, exist_ok=True)
        plan = {
            "query": snap["query"],
            "revision": snap["revision"],
            "total": snap["total"],
            "created_at": _utcnow(),
        }
        _write_json(attempt_dir / "plan.json", plan)
        attempt = {
            "n": n,
            "dir": attempt_dir,
            "plan": plan,
            "viewports": 0,
            "notes": [],
        }
        run["attempts"].append(
            {
                "n": n,
                "dir": f"attempts/{n:03d}",
                "status": "collecting",
                "reason": "",
                "notes": [],
                "plan": {"query": plan["query"], "revision": plan["revision"], "total": plan["total"]},
                "rows_observed": 0,
                "started_at": _utcnow(),
                "finished_at": None,
            }
        )
        self._save_run(run)
        self._log(
            f"尝试 {n}：计划 query={plan['query']} 修订={plan['revision']} 总行数={plan['total']}"
        )
        return attempt

    def _close_attempt(self, run: dict, attempt: dict, status: str, reason: str) -> None:
        outcome = {
            "status": status,
            "reason": reason,
            "notes": attempt["notes"],
            "rows_observed": self._coverage_size(attempt),
            "finished_at": _utcnow(),
        }
        _write_json(attempt["dir"] / "outcome.json", outcome)
        entry = run["attempts"][-1]
        entry.update(
            {
                "status": status,
                "reason": reason,
                "notes": attempt["notes"],
                "rows_observed": outcome["rows_observed"],
                "finished_at": outcome["finished_at"],
            }
        )
        self._save_run(run)
        if reason:
            self._log(f"尝试 {attempt['n']} {status}：{reason}")

    def _coverage_size(self, attempt: dict) -> int:
        path = attempt["dir"] / "coverage.json"
        if not path.exists():
            return 0
        return len(_read_json(path)["rows"])

    # ---------- 页面交互 ----------

    def _apply_query(self, page) -> None:
        cfg = self.config
        current = page.evaluate(
            "() => ({category: document.querySelector('#category').value,"
            " sort: document.querySelector('#sort').value})"
        )
        for key, selector in (("category", "#category"), ("sort", "#sort")):
            if current[key] != getattr(cfg, key):
                page.select_option(selector, getattr(cfg, key))
        expected = f"category={cfg.category};sort={cfg.sort}"
        page.wait_for_function(
            "(q) => document.querySelector('#grid').dataset.query === q",
            arg=expected,
            timeout=10000,
        )
        self._log(f"查询已就绪：{expected}")

    def _snapshot(self, page) -> dict:
        return page.evaluate(SNAPSHOT_JS)

    def _checked_snapshot(self, page, plan: dict) -> dict:
        snap = self._snapshot(page)
        if (snap["query"], snap["revision"], snap["total"]) != (
            plan["query"],
            plan["revision"],
            plan["total"],
        ):
            raise RevisionChanged(plan, snap)
        return snap

    def _scroll(self, page, target_top: float) -> float:
        if self.config.scroll_mode == "wheel":
            current = page.evaluate("() => document.querySelector('#grid').scrollTop")
            delta = target_top - current
            if delta:
                box = page.locator(GRID).bounding_box()
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.wheel(0, delta)
            return page.evaluate("() => document.querySelector('#grid').scrollTop")
        return page.evaluate(
            "(top) => { const g = document.querySelector('#grid');"
            " g.scrollTop = top; return g.scrollTop; }",
            target_top,
        )

    def _wait_render(self, page, prev_snap: dict) -> None:
        sig = "|".join(
            [
                prev_snap["query"],
                str(prev_snap["revision"]),
                ",".join(f"{r['row_index']}:{r['id']}" for r in prev_snap["rows"]),
            ]
        )
        try:
            page.wait_for_function(RENDER_WAIT_JS, arg=sig, timeout=8000)
        except PlaywrightTimeout:
            # 渲染未变化（例如滚动被边界钳制）；后续快照会反映真实状态，
            # 无进展由停滞/不可达检测处理，不用固定秒数等待。
            pass

    def _scroll_to_row(self, page, target: int, snap: dict, plan: dict) -> dict:
        """滚动直到目标逻辑行进入渲染窗口，返回最新快照。"""
        stride = snap["stride"] or 40
        top = min((target - 1) * stride, snap["maxScroll"])
        for _ in range(4):
            applied = self._scroll(page, top)
            if applied != snap["scrollTop"]:
                self._wait_render(page, snap)
            cur = self._checked_snapshot(page, plan)
            indices = [r["row_index"] for r in cur["rows"]]
            if target in indices:
                return cur
            if not indices:
                raise UnreachableRows(f"目标行 {target} 不可达：当前没有渲染任何数据行")
            start = min(indices) - 1  # 渲染窗口的 0 基起始行
            step = (start - (target - 1)) * (cur["stride"] or stride)
            top = min(max(applied - step, 0), cur["maxScroll"])
            if top == applied:
                break
            snap = cur
        raise UnreachableRows(
            f"逻辑行 {target} 无法进入渲染窗口（scrollTop 已到边界 {top}）"
        )

    # ---------- 覆盖与证据 ----------

    def _scroll_collect(self, page, attempt: dict, snap: dict, coverage: dict) -> None:
        plan = attempt["plan"]
        total = plan["total"]
        snap = self._checked_snapshot(page, plan)
        while len(coverage) < total:
            self._record_viewport(page, attempt, snap, coverage)
            limit = self.config.stop_after_viewports
            if limit and attempt["viewports"] >= limit:
                raise InterruptSimulated(
                    f"已保存 {attempt['viewports']} 个视口后按要求中断，覆盖 {len(coverage)}/{total} 行"
                )
            if len(coverage) >= total:
                break
            target = min(i for i in range(1, total + 1) if i not in coverage)
            snap = self._scroll_to_row(page, target, snap, plan)

    def _record_viewport(self, page, attempt: dict, snap: dict, coverage: dict,
                         purpose: str = "collect", confirmed=(), persist_coverage: bool = True) -> None:
        plan = attempt["plan"]
        attempt["viewports"] += 1
        name = f"v{attempt['viewports']:04d}"
        rows_out = []
        new_rows = []
        for row in snap["rows"]:
            idx = row["row_index"]
            spans = row["spans"]
            if len(spans) < 4 or spans[0] != row["id"]:
                raise CollectError(f"行 {idx} 结构异常，无法核对编号与列：{spans}")
            record = {
                "row_index": idx,
                "id": row["id"],
                "name": spans[1],
                "site": spans[2],
                "state": spans[3],
            }
            rows_out.append(record)
            if idx in coverage:
                if coverage[idx]["id"] != row["id"]:
                    raise RevisionChanged(
                        plan,
                        snap,
                        detail=(
                            f"逻辑行 {idx} 编号变化：已记录 {coverage[idx]['id']}，"
                            f"当前渲染 {row['id']}"
                        ),
                    )
                continue
            coverage[idx] = {
                **record,
                "attempt": attempt["n"],
                "viewport": name,
                "viewport_file": f"attempts/{attempt['n']:03d}/viewports/{name}.json",
                "screenshot": f"attempts/{attempt['n']:03d}/viewports/{name}.png",
                "scroll_top": snap["scrollTop"],
                "revision": snap["revision"],
                "query": snap["query"],
                "observed_at": _utcnow(),
            }
            new_rows.append(idx)
        evidence = {
            "viewport": name,
            "attempt": attempt["n"],
            "purpose": purpose,
            "scroll_top": snap["scrollTop"],
            "query": snap["query"],
            "revision": snap["revision"],
            "total_rows": snap["total"],
            "summary_text": snap["summary"],
            "rows": rows_out,
            "new_rows": new_rows,
            "confirmed_rows": sorted(confirmed),
            "screenshot": f"{name}.png",
            "captured_at": _utcnow(),
        }
        _write_json(attempt["dir"] / "viewports" / f"{name}.json", evidence)
        page.screenshot(path=str(attempt["dir"] / "viewports" / f"{name}.png"), full_page=True)
        if persist_coverage:
            _write_json(
                attempt["dir"] / "coverage.json",
                {"plan": plan, "rows": {str(k): coverage[k] for k in sorted(coverage)}, "updated_at": _utcnow()},
            )
        if purpose == "verify":
            self._log(
                f"尝试 {attempt['n']} 视口 {name}（核对）：scrollTop={snap['scrollTop']} "
                f"渲染行 {[r['row_index'] for r in rows_out]} 核对一致 {len(confirmed)} 行 "
                f"修订 {snap['revision']}"
            )
        else:
            self._log(
                f"尝试 {attempt['n']} 视口 {name}：scrollTop={snap['scrollTop']} "
                f"渲染行 {[r['row_index'] for r in rows_out]} 新增 {len(new_rows)} "
                f"覆盖 {len(coverage)}/{plan['total']} 修订 {snap['revision']}"
            )

    # ---------- 恢复 ----------

    def _adopt_saved_coverage(self, page, run: dict, attempt: dict, snap: dict) -> dict:
        """恢复时核对上一尝试的覆盖；无法确认则重采该查询。"""
        if not self.config.resume or len(run["attempts"]) < 2:
            return {}
        prev_dir = self.config.state_dir / run["attempts"][-2]["dir"]
        prev_coverage_path = prev_dir / "coverage.json"
        if not prev_coverage_path.exists():
            attempt["notes"].append("上一尝试没有保存覆盖，直接重采")
            return {}
        saved = _read_json(prev_coverage_path)
        saved_plan = saved["plan"]
        saved_rows = {int(k): v for k, v in saved["rows"].items()}
        plan = attempt["plan"]
        if (saved_plan["query"], saved_plan["total"]) != (plan["query"], plan["total"]):
            attempt["notes"].append(
                f"查询签名或总行数变化（{saved_plan['query']}/{saved_plan['total']} → "
                f"{plan['query']}/{plan['total']}），旧覆盖不采用，重采该查询"
            )
            return {}
        if saved_plan["revision"] != plan["revision"]:
            attempt["notes"].append(
                f"修订变化（{saved_plan['revision']} → {plan['revision']}），"
                "旧修订的行不能拼进新修订，重采该查询"
            )
            return {}
        if not saved_rows:
            return {}
        mismatches, confirmed = self._verify_saved(page, attempt, plan, saved_rows)
        if mismatches:
            detail = "；".join(
                f"行 {m['row_index']} 编号 {m['saved_id']}→{m['seen_id']}" for m in mismatches
            )
            attempt["notes"].append(f"核对发现位置编号变化（{detail}），旧覆盖不采用，重采该查询")
            return {}
        attempt["notes"].append(
            f"核对通过：{len(confirmed)} 个已存行在新会话中一致，采用上一尝试的 "
            f"{len(saved_rows)} 行覆盖（来自尝试 {run['attempts'][-2]['n']}）"
        )
        self._log(attempt["notes"][-1])
        return saved_rows

    def _verify_saved(self, page, attempt: dict, plan: dict, saved_rows: dict):
        """抽样重访已存行：同一逻辑行号必须仍是同一编号与内容。"""
        indices = sorted(saved_rows)
        samples = _pick_samples(indices, self.config.verify_samples)
        mismatches = []
        confirmed = set()
        snap = self._checked_snapshot(page, plan)
        for target in samples:
            snap = self._scroll_to_row(page, target, snap, plan)
            viewport_confirmed = []
            for row in snap["rows"]:
                idx = row["row_index"]
                if idx not in saved_rows:
                    continue
                saved = saved_rows[idx]
                spans = row["spans"]
                seen = {
                    "id": row["id"],
                    "name": spans[1] if len(spans) > 1 else "",
                    "site": spans[2] if len(spans) > 2 else "",
                    "state": spans[3] if len(spans) > 3 else "",
                }
                if any(saved[key] != seen[key] for key in ("id", "name", "site", "state")):
                    mismatches.append(
                        {"row_index": idx, "saved_id": saved["id"], "seen_id": seen["id"]}
                    )
                else:
                    confirmed.add(idx)
                    viewport_confirmed.append(idx)
            self._record_viewport(
                page, attempt, snap, {}, purpose="verify",
                confirmed=viewport_confirmed, persist_coverage=False,
            )
        return mismatches, confirmed

    # ---------- 结果 ----------

    def _write_result(self, run: dict, attempt: dict) -> dict:
        cfg = self.config
        coverage_path = attempt["dir"] / "coverage.json"
        coverage = {int(k): v for k, v in _read_json(coverage_path)["rows"].items()}
        plan = attempt["plan"]
        rows = [coverage[i] for i in sorted(coverage)]
        result = {
            "run_id": run["run_id"],
            "status": "completed",
            "query": {
                "category": cfg.category,
                "sort": cfg.sort,
                "signature": plan["query"],
            },
            "revision": plan["revision"],
            "total_rows": plan["total"],
            "collected_rows": len(rows),
            "rows": [
                {
                    "row_index": r["row_index"],
                    "id": r["id"],
                    "name": r["name"],
                    "site": r["site"],
                    "state": r["state"],
                    "evidence": {
                        "attempt": r["attempt"],
                        "viewport": r["viewport"],
                        "viewport_file": r["viewport_file"],
                        "screenshot": r["screenshot"],
                        "scroll_top": r["scroll_top"],
                        "revision": r["revision"],
                        "query": r["query"],
                        "observed_at": r["observed_at"],
                    },
                }
                for r in rows
            ],
            "coverage_proof": [
                {
                    "row_index": r["row_index"],
                    "id": r["id"],
                    "attempt": r["attempt"],
                    "viewport": r["viewport"],
                    "viewport_file": r["viewport_file"],
                    "scroll_top": r["scroll_top"],
                }
                for r in rows
            ],
            "attempts": run["attempts"],
            "retries_used": run["retries_used"],
            "max_retries": run["max_retries"],
            "started_at": run["created_at"],
            "finished_at": _utcnow(),
        }
        _write_json(cfg.state_dir / "result.json", result)
        with open(cfg.state_dir / "rows.csv", "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["行号", "设备编号", "名称", "所属站点", "状态", "观察视口", "scrollTop", "修订", "查询签名"])
            for r in rows:
                writer.writerow(
                    [r["row_index"], r["id"], r["name"], r["site"], r["state"],
                     r["viewport_file"], r["scroll_top"], r["revision"], r["query"]]
                )
        self._log(
            f"采集完成：{len(rows)}/{plan['total']} 行，修订 {plan['revision']}，"
            f"结果见 {cfg.state_dir / 'result.json'}"
        )
        return result


def _pick_samples(indices: list, k: int) -> list:
    """均匀抽样，始终包含首行与末行。"""
    if len(indices) <= k:
        return list(indices)
    last = len(indices) - 1
    picked = {indices[round(i * last / (k - 1))] for i in range(k)}
    return sorted(picked)
