"""采集入口：python -m collector --url ... --category all --sort id --state-dir ...

退出码：0 采集完成；2 已中断（可用 --resume 接着做）；1 失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import CollectError, Collector, Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m collector",
        description="虚拟化设备台账的行覆盖采集：按类别与排序收齐逻辑行，输出结构化结果与页面证据",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8090", help="门户地址")
    parser.add_argument("--category", default="all", help="类别，如 all、lift")
    parser.add_argument("--sort", default="id", help="排序，如 id、name")
    parser.add_argument("--state-dir", type=Path, required=True, help="状态目录（游标、尝试记录与证据）")
    parser.add_argument("--resume", action="store_true", help="接着状态目录中未完成的运行继续做")
    parser.add_argument("--max-retries", type=int, default=3, help="查询状态变化后的最大重建次数")
    parser.add_argument("--verify-samples", type=int, default=5, help="恢复时抽样核对的行数")
    parser.add_argument("--scroll-mode", choices=["auto", "wheel"], default="auto",
                        help="滚动方式：auto 直接设置 scrollTop，wheel 用鼠标滚轮")
    parser.add_argument("--stop-after-viewports", type=int, default=None,
                        help="调试用途：保存指定数量视口后模拟中断")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--executable-path", default=None, help="自定义 Chromium 路径")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(
        url=args.url,
        category=args.category,
        sort=args.sort,
        state_dir=args.state_dir,
        max_retries=args.max_retries,
        resume=args.resume,
        verify_samples=args.verify_samples,
        scroll_mode=args.scroll_mode,
        stop_after_viewports=args.stop_after_viewports,
        headless=not args.headed,
        executable_path=args.executable_path,
        logger=print,
    )
    try:
        outcome = Collector(config).run()
    except CollectError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("收到中断信号，状态已保存，可用 --resume 接着做", file=sys.stderr)
        return 2
    status = outcome["status"]
    if status == "completed":
        print(f"完成：结果与证据见 {outcome['state_dir']}")
        return 0
    if status == "interrupted":
        print(f"已中断：可用 --resume --state-dir {outcome['state_dir']} 接着做")
        return 2
    print(f"未完成：{outcome.get('failure_reason')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
