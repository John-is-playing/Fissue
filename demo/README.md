# Fissue 测试夹具

五套**互相独立**的对抗性夹具，用来真实验证完整链路：抓取 → AI 评测 → 生成验证器 → 沙盒 F2P
验证 → 回归门 → 自动修复提 PR。[BENCHMARK.md](../BENCHMARK.md) 的跑分数字全部出自这五套。

| 夹具 | 领域 | 形态 | 考什么 | 规模 |
|---|---|---|---|---|
| [`textkit`](textkit/README.md) | 文本处理 | 单模块、独立函数 | 单函数语义；**识破「看似正确」的假修复** | 5 Issue + 2 PR |
| [`ratekit`](ratekit/README.md) | 金额 / 费率 | 单模块、数值边界 | 数值边界；**中文标题的重复检测**、刷量与误报拦截 | 10 Issue + 3 PR |
| [`pipekit`](pipekit/README.md) | 分片流水线 / 区间 | 多模块、调用链耦合 | **跨模块根因定位**（同一约定散落三处）、回归门 | 15 Issue + 5 PR |
| [`envkit`](envkit/README.md) | 配置 / 缓存 / 校验 | 有状态对象 + 环境依赖 | 状态副作用、环境依赖、输入校验 | 15 Issue + 5 PR |
| [`lockkit`](lockkit/README.md) | 并发原语 / 资源管理 | 不变量约束 + 时间注入 | 资源不变量、超时上限、一次性语义 | 15 Issue + 5 PR |

合计 **60 Issue + 20 PR**。每套各占一个独立领域与语法形状，避免「照着同一类缺陷出题」的偏差。
除 `textkit` 外，每套都刻意混入**错误 / 无效 / 同源但不重复 / 刷量**的请求——它们是「反刷子」与
「重复检测」的实测素材。

## 目录布局（五套完全一致）

```
demo/<kit>/
├── README.md      设计意图与期望结果对照表
├── fixtures/      每个 Issue 的内容与元数据（标题 / 标签在文件头注释里）
├── scripts/       setup_github.py：一键建仓库 / Issue / PR
└── repo/          待推送的独立 git 仓库（外层 .gitignore 忽略）
```

`repo/` 不进外层仓库：它自带 `.git`，嵌套会被当成 embedded repository，产生 submodule 陷阱。

## 怎么用

```bash
# 1) 推送到平台（会产生写操作，请自行执行）
python demo/<kit>/scripts/setup_github.py --owner 你的用户名 --dry-run   # 先看计划
python demo/<kit>/scripts/setup_github.py --owner 你的用户名            # 真跑

# 2) 跑完整链路
fissue fetch  --repo 你的用户名/<kit> --limit 50
fissue eval   --repo 你的用户名/<kit> --all
fissue verify --repo 你的用户名/<kit>
fissue flush
fissue fix    --repo 你的用户名/<kit> --dry-run --limit 6

# 3) 出报告，与本套 README 的期望对照表逐项核对
fissue report --repo 你的用户名/<kit> --format markdown --out data/exports/<kit>.md
```

单测完全离线，不需要这些仓库：`python -m pytest -q`。

## 加一套新夹具

门槛最低、收益最高的贡献路径（见 [CONTRIBUTING.md](../CONTRIBUTING.md)）：在你自己关心的场景里
验证 Fissue 是否可靠，而夹具正是这个项目最重要的资产。按上面的布局放好四个部分，再把
`demo/<kit>/repo/` 加进 `.gitignore`。

---

相关：[BENCHMARK.md](../BENCHMARK.md) · [CONTRIBUTING.md](../CONTRIBUTING.md) ·
[验证器与 F2P](../docs/wiki/Verifier-and-F2P.md)