# 用真浏览器检查 Web UI

前端的问题大多**不报错**：控制台一条 `ReferenceError`、hover 高亮比旁边矮一截、
点了没反应、滚动条压住内容——这些都不影响页面"能打开"，所以单测和模板断言
全部绿着，用户一上手就是坏的。

`tools/ui_shot.py` 用 Playwright 驱动真 chromium，把这些"绿着但坏"的问题变成
可断言的结论，并留下截图。

## 前置条件

| 依赖 | 说明 |
|---|---|
| Python 包 `playwright` | 本机已装（1.64.1） |
| chromium 内核 | 已下载（`~/AppData/Local/ms-playwright/chromium-*`） |

缺内核时执行一次：`python -m playwright install chromium`。

> 注意：本机 **Edge 的无头模式是坏的**（`--headless` 各种写法全部静默失败、
> 无输出、无文件）。这只说明不能用 Edge 做自动化，**不代表没有浏览器能力**——
> Playwright 自带的 chromium 完全可用。

## 用法

```bash
# 默认：起临时服务（自动造数据）、跑 10 个页面 × 浅色/深色、截图并检查
python tools/ui_shot.py

# 对着正在跑的服务检查（不造数据，看真实状态）
python tools/ui_shot.py --url http://127.0.0.1:8000

# 只看流水线页、1600 宽、只截深色
python tools/ui_shot.py --only /pipeline --width 1600 --theme dark
```

产物在 `data/ui-shots/<时间戳>/`：

- `<页面>-light.png` / `<页面>-dark.png`（整页截图，含折叠区之外的全部内容）
- `report.json`（每条问题的页面、检查名、明细）

退出码：`0` = 全部通过，`1` = 有 error 级问题（可直接进 CI）。

## 检查项与它们各自的来历

| 检查 | 抓什么 | 为什么会有这条 |
|---|---|---|
| `console` / `pageerror` | 控制台错误与未捕获异常 | 曾每行一条 `Fissue is not defined`：模板在 `app.js` 之前调用了未定义的命名空间 |
| `overflow` | 整页横向溢出 | 溢出时右侧多出一条空背景，用户以为页面没加载完 |
| `kanban-wrap` | 看板列被挤成多行 | `repeat(auto-fit, …)` 在列宽不够时把最后一列甩到第二行 |
| `kanban-scroll` | 看板需要横向滚动（warn） | 列宽下限算错，每屏都被迫横滚 |
| `cell-height` | `td.cell-acts` 的高度 ≠ 行高 | `display:flex` 挂在 `<td>` 上让它不再是 table-cell，行 hover 高亮下沿出现死区 |
| `cell-dead-zone` | 格子近角处点下去命中的不是自己 | 只有格内小字可点、而高亮是整格时，高亮区里有一截点不动 |
| `view-switch` | 点了看板/泳道/漏斗是否真的切过去 | `<html data-mode>` 与按钮的 `data-mode` 撞车，裸选择器把根节点也注册成按钮，切完立刻弹回看板 |
| `stuck-layout` | 「需要你处理的」标题是否在表格上方 | `.pipe-stuck` 与 `.pipe-acts` 共用了一条 flex 规则，标题被挤到表格左侧、表头数据错位 |
| `theme` | 请求的主题是否真的生效 | 主题偏好读取失败时静默落回浅色 |

## 反向对照（重要）

"全部通过"可能只是检查闭着眼。所以工具自带一组**故意做坏**的合成页面：

```bash
python tools/ui_shot.py --self-test
```

每个场景对应一种毛病（控制台报错、看板换行、flex 的 td、圆角死区、不生效的
切换按钮、并排的标题……），每条检查都必须在对应场景里报警；少一条就返回 1。

这一步不是形式主义：第一版自检就照出两条**哑检查**（`cell-height`、
`kanban-wrap`），原因是合成页面没造出真实条件——flex 的 `td` 只有在**旁边那一格
更高**时才会比行矮；`auto-fit` 也只有容器窄到放不下时才换行。

## 写新检查时的三条经验

1. **量出来的数**优先于文本匹配：`getBoundingClientRect`、`elementFromPoint`、
   `scrollWidth/clientWidth`。文本里"有没有某个 class"用静态断言就够了，不必开浏览器。
2. **先滚进视口再量**：`elementFromPoint` 对视口外坐标返回 `null`，不滚会把
   "量不到"误报成"点不到"。
3. **避开圆角**：格子有 6px 圆角，贴着角取样会落在圆角外侧、命中父容器。
   内缩至少 12px，并取多个点（左下、右下、下沿中点）。

## 可选：把浏览器能力挂成 MCP 工具

如果希望**交互式**地用浏览器（而不是跑脚本），可以装 Playwright MCP 服务器，
让 agent 直接调用"打开页面 / 截图 / 点击 / 读可访问性树"：

```bash
npx @playwright/mcp@latest        # 需要 node（本机 v22 已装）
```

然后在 AtomCode 的 MCP 配置里登记这个命令。与 `tools/ui_shot.py` 的分工：

| | `tools/ui_shot.py` | Playwright MCP |
|---|---|---|
| 形态 | 一次性脚本，进 CI | 常驻工具，交互式 |
| 优点 | 断言明确、可回归、产物固定 | 能现场探索、随手点 |
| 适合 | 提交前/CI 守卫 | 排查具体问题、验证一次性假设 |

两者可以并存：日常回归用脚本，遇到新问题时用 MCP 现场点两下，再把结论固化成
脚本里的一条检查。

## 交互 E2E：`tools/ui_e2e.py`

`ui_shot.py` 看的是"页面长成什么样"，`ui_e2e.py` 验的是"点了会发生什么"——两者互补。
E2E 会自起一个**隔离实例**（临时 SQLite + `demo/textkit` 5 条数据 + 1 条「失败且有上一版」的
部署记录），然后在真 chromium 里逐步操作。

```bash
python tools/ui_e2e.py                 # 全场景，产出截图 + report.json
python tools/ui_e2e.py --keep          # 跑完保持服务，方便自己接着点
python tools/ui_e2e.py --base-url http://127.0.0.1:8000   # 对着已有实例
```

五个场景（都对应真实的"以为生效了"陷阱）：

| 场景 | 断言什么 | 为什么必须真点 |
|---|---|---|
| 主题三态 | 点两次 `data-theme-pref` 走到 `dark`，刷新后仍是 `dark` | 只改 DOM 不写 localStorage 时，刷新就回到浅色 |
| 流水线三视图 | 目标视图可见、其余全部 hidden | 属性名撞车那次的症状正是"切完弹回看板" |
| 三档修复策略 | 切到「草稿 PR」→ 保存 → **覆盖层文件里真有** `mode: fork` / `draft: true` → 刷新仍选中 | 只看选中态会漏掉"保存没写盘" |
| 失败行回滚 | 点按钮 → 弹确认框 → 确认后**真的发出** `POST /deployments/{id}/rollback`（请求被拦截，不连真机） | 只看按钮存在，等于没测 |
| 引导三档 | 每档点击后 `localStorage['fissue.guide.mode']` 跟着变 | 切了不记，下次打开又回默认 |

### 三个必须知道的坑（都是这轮 E2E 自己踩出来的）

1. **测"主题持久化"不能注入主题偏好**。`ui_shot._new_page` 默认会往 localStorage 写主题，
   每次导航都写——于是刷新后看起来"偏好没存住"，其实是测试工具把它改回去了。
   需要 `set_theme_pref=False`。
2. **测"保存设置"要把库地址写进 `.env`**。保存会触发配置重载，而 `.env` 优先级高于
   config/覆盖层；不写它，重载后数据库回落成 `./data/fissue.db`，后面的 `/ops` 看到的是
   另一个空库，表现为"回滚按钮不存在"。
3. **覆盖层要在清理临时目录之前读**。自建实例的覆盖层落在临时目录里，先 cleanup 就只剩
   "未验证"。

## 容器形态的渲染检查

```bash
FISSUE_WEB_PORT=8012 docker compose up -d
python tools/ui_shot.py --url http://127.0.0.1:8012 --only /login /settings
FISSUE_WEB_PORT=8012 docker compose down -v
```

⚠️ 在 Git Bash 下跑时记得 `export MSYS_NO_PATHCONV=1`：否则 `/login` 会被改写成
`C:/Program Files/Git/login`，Playwright 报 `Cannot navigate to invalid URL`。
