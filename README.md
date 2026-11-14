# FreeModal 数据脑 · （GitHub Actions 静态 JSON）

> 用途：给 `free-modal-radar` 技能提供 `FREEMODAL_DATA_URL` 指向的结构化 JSON。
> 本仓库**不采集、不抓取、不编造**任何额度，只把「已核验清单」确定性编译成公开 JSON。
> 零服务器、零数据库、零第三方依赖、公开仓库零费用。

## 一、先看懂它做什么

> **当前刷新触发方式（实测结论）**：由 **push** 和 **手动 dispatch** 触发刷新，**定时触发未生效**。工作流里的 `schedule` 配置保留在文件中，但它当前不工作——不要把「配了 cron」理解为「配了就会跑」。核验到 13 个调度窗口 0 次触发，`offers.json` 的 `generated_at` 只在实际 push 或手动 dispatch 后更新。

```
你（人工/AI 辅助）核验官方页面
        ↓ 写入 sources/offers.json（可存私有核验字段）
GitHub Actions 由 push 或手动 dispatch 触发（定时触发当前未生效）
        ↓ 校验 → 剥离私有字段 → 剔除过期核验 → 写 offers.json → commit
GitHub Pages 静态托管 offers.json
        ↓ 匿名只读 URL
free-modal-radar 技能拉取 → 双轨折算 → 卡册看板
```

**关键设计**：`sources/offers.json` 初始是**空数组**，产出的是「诚实空表」。这样整条链路可以先跑通、技能不再报 `DATA_UNREACHABLE`，但**不宣称任何不存在的额度**。等你有真实已核验数据了，再逐条往清单里加。

## 二、为什么是这几条技术选择

| 选择 | 原因 |
| :--- | :--- |
| `cron: '*/20 * * * *'` | 配置保留，但**当前不工作**：实测 13 个调度窗口 0 次触发，故刷新实际只靠 push 与手动 dispatch。配置本身仍在，便于平台侧恢复后直接生效 |
| 时间按 **UTC** | `on.schedule` 默认 UTC，且只在默认分支最新提交上生效；当前定时未触发，UTC 口径仅作配置说明 |
| `workflow_dispatch` | **当前主力触发方式之一**：手动点一次立即出数据 |
| `push`（限定路径） | **当前主力触发方式之一**：改动 `sources/**`、`schema/**`、`scripts/**` 时自动构建 |
| 每次都改写 `generated_at` | 技能端 2 小时过期阈值；但刷新依赖 push / dispatch，**没有新提交就会持续变旧并最终 stale** |
| 每次 commit | 公开仓库 **60 天无任何提交会自动停用定时任务**；定时触发既然未生效，保活提交目前只随 push 构建产生 |
| GitHub Pages 而非 raw URL | `raw.githubusercontent.com` 有约 5 分钟 CDN 缓存且不适合当 API 端点；Pages 是正经静态托管 |
| `.nojekyll` | 禁止 Jekyll 构建吞掉非 HTML 文件，保证 `offers.json` 原样可访问 |
| 纯 Python 标准库 | 与技能端一致，零依赖，Actions 无需 `pip install` |

## 三、文件清单

| 路径 | 作用 | 你要不要改 |
| :--- | :--- | :--- |
| `.github/workflows/refresh.yml` | 定时任务 + 自检 + 构建 + 提交 | 一般不用 |
| `scripts/build_feed.py` | 校验 / 编译 / 预检 / 自检 | 不用 |
| `schema/contract.json` | 与技能端对齐的枚举与字段契约 | 技能端 schema 升级时同步 |
| `sources/offers.json` | **已核验信源清单（初始为空）** | **要，这是唯一日常要填的文件** |
| `sources/inbox.json` | **线索区：只记「听说」，不参与编译** | 有线索时追加，核实后转正 |
| `sources/TEMPLATE.md` | 字段说明、三步核验法与线索转正流程 | 读 |
| `offers.json` | 自动生成的公开产物 | 不要手改 |
| `.nojekyll` | Pages 配置 | 不要删 |

## 四、本地自检

```bash
python scripts/build_feed.py --selftest
python scripts/build_feed.py --fixtures
python scripts/build_feed.py --json
python scripts/build_feed.py --verify --json
```

`--selftest` 覆盖：合法条目、10 类非法条目被拒、私有字段剥离、空表合法、幂等、过期核验剔除、外链拦截、stale 判定、未知主版本拒绝、生命周期状态（缺省 `verified`、`pending_official` 保留、`deadline` 自动过期）、折算口径字段与六类拒绝规则。
`--fixtures` 覆盖 10 个 good + 17 个 bad 回归样例（含生命周期、`money_basis`、六类拒绝规则各一条）。
`--now` 可注入 UTC 时钟保证可复现，例如 `--now 2026-11-13T05:17:00+00:00`。

## 五、与技能端契约的一致性

`schema/contract.json` 的 `enums` / `required_offer_fields` / `schema_major_supported` / `stale_threshold_hours` 必须始终与技能端 `references/valuation-constants.json` 及 `references/data-schema.md` 一致。**任一侧改了枚举，另一侧必须同步并重跑自检**，否则技能端会报 `SCHEMA_INVALID`。

发布前 `--verify` 会用与技能端 `validate_payload` 等价的核心判定预检一次产物，把问题拦在 commit 之前。

## 六、合规红线

- 公开仓库：**不得放入任何密钥、Token、私人联系方式、私人二维码**。
- 不使用原开源项目名称与 Logo，品牌统一 **FreeModal**。
- 不承诺「永久免费 / 无限额度 / 保证薅到 / 稳定收益」；所有额度以官方最新公告为准。
- 不内置爬虫；抓取第三方页面前自行确认该站服务条款与 robots 约束。
- 不作投资、财务或收益建议；本数据仅为信息整理。

## 七、已知限制与网络现实（国内用户必读）

- Actions 定时任务（`schedule`）**当前未生效**：实测 13 个调度窗口 0 次触发。刷新实际由 push 与手动 dispatch 驱动，因此两次触发之间的间隔不受「每 20 分钟」约束，`generated_at` 可能长时间不更新。
- 即使定时恢复，Actions 也可能延迟 5–30 分钟，且只在默认分支的最新提交上运行；它不是精确闹钟。
- 若仓库连续 60 天无任何提交，GitHub 会自动停用 schedule；当前保活提交只随 push 构建产生。
- 公开仓库的 Actions 日志与产物全部公开，注意信息边界。
- 数据源 URL 由技能端环境变量或配置注入，本仓库不硬编码任何 URL。

### 7.1 三个 URL 选项，按「新鲜度」而非「速度」排序

| 选项 | URL 形式 | 缓存 / 新鲜度 | 国内可访问性 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **A. GitHub Pages** | `https://<用户名>.github.io/<仓库名>/offers.json` | 每次 commit 重新部署，约 30–60 秒生效；新鲜度取决于是否有 push / dispatch | 时好时坏，可能打不开 | **默认推荐**，先在浏览器实测 |
| **B. raw 直链** | `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/offers.json` | 约有 5 分钟 CDN 缓存，跟随 commit 更新 | 同样不稳定，且官方不建议当 API 端点 | 备选 / 排障用 |
| **C. jsDelivr** | `https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/offers.json` | 未固定版本的分支 URL **缓存可达 7 天**（另有资料称 12 小时） | 国内相对更稳 | **不适合要求新鲜的场景**；除非接受数据滞后一周 |

> **决策**：技能端有 2 小时过期阈值，所以需要尽量新鲜的产物。**当前定时触发未生效**，新鲜度只能靠 push / 手动 dispatch 维持；A 与 B 之间选 A；若两者在国内都访问不稳，不要靠 CDN 绕，应改用**方案 A 的国内静态托管**（如已连接的环境、对象存储静态站），并保留同一份 `offers.json` 产物——数据脑的构建部分完全不用改，只需换 URL。

### 7.2 两个容易踩的坑

- **技能端超时 3 秒**：`references/valuation-constants.json` 的 `fetch_timeout_seconds` 默认 `"3"`。跨境访问 GitHub 时 3 秒常不够，会返回 `DATA_UNREACHABLE`。若实测超时，把该值调到 `"10"`（**改技能包前先按快照铁律做快照，并重跑 QA 自检**）。
- **空表与「今日 0 元」的区别**：数据脑初始产出 `offers: []` 是「尚无已核验数据」。技能端已修正：空表时看板渲染「本次没有可核验记录，不输出折算结论」，不再出现「今日可用 ¥0.00」这类会被误读成「今天没有免费额度」的写法；只有一次性额度时也会单独成栏、标注「不计入今日」。结果 JSON 中空表与「今日桶为空」的数值键为 `null` 并附提示字段，不会输出 0。看板与 `radar-result.json` 均只反映数据脑的 `offers` 与 `generated_at`，请以 `generated_at` 判断新鲜度。

## 八、同类参照

线上产品 `aihot.news/free-tokens` 与本项目面向同一类需求（汇总可用的免费 AI 额度），可作横向参照：它按「每天 / 每周 / 每月 / 其他」分组展示卡片，逐条标注核实日期与剩余天数，并把赠金面值、积分单价、官方模型价、不可折钱四类折算依据分开处理，这些做法值得学习。

两边定位不同，差异主要在工程形态而不是内容多少：

- **数据形态**：本项目产出可机读的 `offers.json`（固定字段、固定枚举、带 `schema_version`），技能可直接消费；`aihot.news/free-tokens` 是网页展示，未提供同等的公开只读数据结构。
- **额度描述**：本项目对每条额度记录四维口径（`watermark` / `commercial_rights` / `queue_speed` / `resolution`）与额度周期（`once` / `daily` / `per_style_daily` / `monthly` / `unknown`），用于判断「今天真能用多少」，而不只是「有没有活动」。
- **失效与降级**：本项目有明确的降级契约（`DATA_UNREACHABLE` / `SCHEMA_INVALID` / `DATA_STALE` 等）与缓存快照策略，取不到数据时说明原因并标 `stale`，不拿旧数据冒充实时；同时用 `status` + `deadline` 标出待核实与已过期条目。

本项目不借用对方的名称与 Logo，仅在此记录公开 URL 作为同类产品的客观对照；两边数据各自维护，互不抓取。

