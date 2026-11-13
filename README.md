# FreeModal 数据脑 · （GitHub Actions 静态 JSON）

> 用途：给 `free-modal-radar` 技能提供 `FREEMODAL_DATA_URL` 指向的结构化 JSON。
> 本仓库**不采集、不抓取、不编造**任何额度，只把「已核验清单」确定性编译成公开 JSON。
> 零服务器、零数据库、零第三方依赖、公开仓库零费用。

## 一、先看懂它做什么

```
你（人工/AI 辅助）核验官方页面
        ↓ 写入 sources/offers.json（可存私有核验字段）
GitHub Actions 每小时第 17 分（UTC）自动运行
        ↓ 校验 → 剥离私有字段 → 剔除过期核验 → 写 offers.json → commit
GitHub Pages 静态托管 offers.json
        ↓ 匿名只读 URL
free-modal-radar 技能拉取 → 双轨折算 → 卡册看板
```

**关键设计**：`sources/offers.json` 初始是**空数组**，产出的是「诚实空表」。这样整条链路可以先跑通、技能不再报 `DATA_UNREACHABLE`，但**不宣称任何不存在的额度**。等你有真实已核验数据了，再逐条往清单里加。

## 二、为什么是这几条技术选择

| 选择 | 原因 |
| :--- | :--- |
| `cron: '17 * * * *'` | GitHub Actions 最短间隔 5 分钟，小时级足够；**避开整点高峰**（整点排队最严重，官方与社区均建议用 17 分这类奇数分钟） |
| 时间按 **UTC** | `on.schedule` 默认 UTC，且只在默认分支最新提交上生效。UTC 每小时 17 分 = **北京时间每小时 17 分**（时差 8 小时为整数），无需换算也无需改时区 |
| `workflow_dispatch` | 首次可手动点一次立即出数据，不用等整点 |
| 每次都改写 `generated_at` | 技能端 2 小时过期阈值；小时级刷新保证不会 stale |
| 每次 commit | 公开仓库 **60 天无任何提交会自动停用定时任务**；小时级提交同时解决「保活」 |
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
| `sources/TEMPLATE.md` | 字段说明与三步核验法 | 读 |
| `offers.json` | 自动生成的公开产物 | 不要手改 |
| `.nojekyll` | Pages 配置 | 不要删 |

## 四、本地自检

```bash
python scripts/build_feed.py --selftest
python scripts/build_feed.py --fixtures
python scripts/build_feed.py --json
python scripts/build_feed.py --verify --json
```

`--selftest` 覆盖：合法条目、10 类非法条目被拒、私有字段剥离、空表合法、幂等、过期核验剔除、外链拦截、stale 判定、未知主版本拒绝。
`--fixtures` 覆盖 5 个 good + 6 个 bad 回归样例。
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

- Actions 定时任务**可能延迟 5–30 分钟**，且只在默认分支的最新提交上运行；不是精确闹钟。
- 若仓库连续 60 天无任何提交，GitHub 会自动停用 schedule；本骨架的小时级 commit 已规避。工作流在内容确实没变时会用 `--allow-empty` 提交一次，保证「每小时都有提交」这条保活前提在任何情况下都成立。
- 公开仓库的 Actions 日志与产物全部公开，注意信息边界。
- 数据源 URL 由技能端环境变量或配置注入，本仓库不硬编码任何 URL。

### 7.1 三个 URL 选项，按「新鲜度」而非「速度」排序

| 选项 | URL 形式 | 缓存 / 新鲜度 | 国内可访问性 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **A. GitHub Pages** | `https://<用户名>.github.io/<仓库名>/offers.json` | 每次 commit 重新部署，约 30–60 秒生效，**小时级新鲜度可保证** | 时好时坏，可能打不开 | **默认推荐**，先在浏览器实测 |
| **B. raw 直链** | `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/offers.json` | 约有 5 分钟 CDN 缓存，小时级更新**够用** | 同样不稳定，且官方不建议当 API 端点 | 备选 / 排障用 |
| **C. jsDelivr** | `https://cdn.jsdelivr.net/gh/<用户名>/<仓库名>@main/offers.json` | 未固定版本的分支 URL **缓存可达 7 天**（另有资料称 12 小时） | 国内相对更稳 | **小时级场景禁用**；除非接受数据滞后一周 |

> **决策**：小时级更新是硬指标，所以 **C 不能用**。A 与 B 之间选 A；若两者在国内都访问不稳，不要靠 CDN 绕，应改用**方案 A 的国内静态托管**（如已连接的环境、对象存储静态站），并保留同一份 `offers.json` 产物——数据脑的构建部分完全不用改，只需换 URL。

### 7.2 两个容易踩的坑

- **技能端超时 3 秒**：`references/valuation-constants.json` 的 `fetch_timeout_seconds` 默认 `"3"`。跨境访问 GitHub 时 3 秒常不够，会返回 `DATA_UNREACHABLE`。若实测超时，把该值调到 `"10"`（**改技能包前先按快照铁律做快照，并重跑 QA 自检**）。
- **空表与「今日 0 元」的区别**：数据脑初始产出 `offers: []` 是「尚无已核验数据」。技能端已修正：空表时看板渲染「本次没有可核验记录，不输出折算结论」，不再出现「今日可用 ¥0.00」这类会被误读成「今天没有免费额度」的写法；只有一次性额度时也会单独成栏、标注「不计入今日」。结果 JSON 中空表与「今日桶为空」的数值键为 `null` 并附提示字段，不会输出 0。看板与 `radar-result.json` 均只反映数据脑的 `offers` 与 `generated_at`，请以 `generated_at` 判断新鲜度。

