# 虚拟化设备台账的行覆盖采集本地门户

本项目是可在本地运行的合成业务门户，提供真实浏览器页面、交互状态及样本资料，不包含业务采集端。资料、用户与工作空间都是虚构内容。

## 环境

Python 3.12、Playwright 1.56.0 与 Chromium。`portal/` 是标准库 HTTP 服务，`web/` 是浏览器页面及 JavaScript 交互，`tests/` 检查门户自身行为。Python 用于服务与浏览器控制，JavaScript 只在浏览器内运行，不需要 Node.js 服务。

安装依赖及浏览器：

```sh
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m portal --port 8090
```

在浏览器打开本机8090端口即可操作。门户的正常交互在页面完成，`web/data.json` 保存合成来源材料。状态随页面或浏览器上下文生命周期保存；需要持久状态的功能使用当前 origin 的 localStorage。重新开始可创建新的浏览器上下文。

运维入口 GET `/__state` 查看来源动作记录，POST `/__control` 用 JSON 设置 phase、version、confirmed。它们只用于本地来源调度，接入程序的正常操作不能借用运维入口取得业务数据或完成授权。GET `/api/runtime` 和 POST `/api/action` 是门户页面内部协议。

## 验证与容器

```sh
python -m unittest discover -s tests -v
python -m compileall -q portal tests
docker build -t virtual-grid-portal .
docker run --rm --network none --shm-size=1g virtual-grid-portal
```

Dockerfile 固定浏览器基础镜像的完整摘要，依赖版本在 requirements.txt 固定。首次构建需要下载依赖；镜像完成后，上述检查禁用外网，浏览器与门户在同一容器内通信。默认运行来源检查，不运行资料采集。

如需在宿主浏览器操作容器内门户：

```sh
docker run --rm -p 127.0.0.1:8090:8090 virtual-grid-portal python -m portal --host 0.0.0.0 --port 8090
```

页面的 DOM 状态、公开身份属性与中文加载提示可用于等待完成，避免使用固定秒数等待。来源动作记录可核对页面操作；不要在日志记录会话秘密或不需要的私密字段。

来源场景、属性及控制方式详见 `source-contract.json`。

## 采集模块（collector/）

`collector/` 是面向该门户的业务采集端：按用户选择的类别与排序，在真实 Chromium 中滚动虚拟化表格，从滚动容器内实际渲染的 `[role=row]` 行读取数据，收齐全部逻辑行。模块只读取页面 DOM（`data-query`、`data-revision`、`aria-rowcount`、`aria-rowindex`、`data-id` 与行列文本），不访问页面脚本变量，不调用 `/api`、`/__` 接口。

```sh
python -m portal --port 8090 &
python -m collector --url http://127.0.0.1:8090 --category all --sort id --state-dir state/run-1
python -m collector --state-dir state/run-1 --resume   # 中断后接着做
```

退出码：0 完成；2 已中断（可 `--resume`）；1 失败。`--max-retries` 限制查询状态变化后的重建次数（默认 3），持续变化不会无限重跑；`--scroll-mode wheel` 改用鼠标滚轮滚动。

### 采集计划与游标

一次采集计划由（查询签名, 修订, 总行数）唯一确定。采集中任一变化（含同一 `aria-rowindex` 渲染出不同编号）会作废旧尝试：其覆盖被隔离保存在 `attempts/NNN/`，绝不拼入新修订的结果，随后按新页面状态重建计划。游标不是 `scrollTop`，而是计划 + 逐行覆盖 + 视口证据；恢复时先确认查询与修订，再抽样核对已存行（首末行必查），核对不一致或修订不同就放弃旧覆盖、重采该查询。

### 状态目录与结构化结果

```
state/run-1/
  run.json                 # 运行状态、各尝试的计划/结果/未完成原因、重试计数
  attempts/001/plan.json   # 该尝试的 查询签名/修订/总行数
  attempts/001/coverage.json
  attempts/001/outcome.json
  attempts/001/viewports/v0001.json  # 该视口的查询、修订、scrollTop、渲染行
  attempts/001/viewports/v0001.png   # 同视口页面截图证据
  result.json              # 行级结果 + coverage_proof（每个逻辑行号→编号→观察视口）
  rows.csv                 # 设备编号、名称、所属站点、状态及证据定位
```

`result.json` 的每行含设备编号、名称、所属站点、状态与证据（尝试号、视口文件、截图、scrollTop、修订、查询签名、观察时间）。同名不同编号的设备按各自逻辑行号分别记录，不会合并。

### 采集行为检查

`tests/test_collector.py` 真实驱动 Chromium 滚动并读取 DOM，不用固定秒数等待：正常收齐（编号/名称排序、类别过滤、滚轮滚动）、中断恢复、滚动中换序拒绝拼接（结果只含新修订的行，DV-023 不与 DV-NEW 混排）、重试有界、恢复时识别同位置编号变化并重采。

```sh
python -m unittest discover -s tests -v
```

门户修复说明：`web/app.js` 的占位高度增加一行过扫描量；原实现滚动到底时渲染窗口止步于倒数第二行，最后一个逻辑行永远无法进入渲染窗口，任何采集端都无法收齐。
