"""提示词模板（全部中文，因为我们面向中文社区 + 中文模型表现更稳）。

设计原则
--------
1. **结构化输出**：所有提示都要求返回 JSON，字段与 :mod:`fissue.models` 对齐。
2. **证据优先**：要求模型给出 ``evidence``（引用原文片段），便于人工复核与反刷子。
3. **不臆造**：明确要求「信息不足时降低 confidence，而不是编造」。
4. **可注入**：条目内容统一走 :func:`render_item`，控制截断长度，避免超长 prompt 烧钱。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..models import AuthorKind, ItemType, RawItem

# 单条内容注入上限（字符），防止超长 Issue 烧掉大量 token
MAX_BODY_CHARS = 6000
MAX_COMMENT_CHARS = 1200
MAX_COMMENTS = 15
MAX_DIFF_CHARS = 20000


SYSTEM_TRIAGE = """你是资深开源项目维护者与技术评审专家。你的任务是判断 Issue / Pull Request 的\
真实性、重要性、可行性与质量，帮助维护者决定「先处理哪个、能不能自动修、要不要合并」。

铁律：
1. 只依据给定的材料判断，**不得臆造**材料中没有的信息。信息不足时降低 confidence，而不是猜。
2. 打分必须有依据，evidence 字段要引用原文片段（可以是中文或英文原句）。
3. 用户抱怨 ≠ 真 Bug。要区分「用户误用」「环境问题」「真缺陷」「重复提交」「刷量」。
4. 涉及安全、数据丢失、崩溃、核心功能不可用的问题，重要性要高。
5. 输出必须是**合法 JSON**，不要输出任何 JSON 之外的解释文字。"""


SYSTEM_VERIFIER = """你是资深测试工程师。你的任务是为一个已知问题编写**可执行的验证器**，\
用来证明「问题在修复前存在、修复后消失」（fail-to-pass）。

铁律：
1. 验证器必须能自动判定通过/失败：优先写可运行的测试代码（退出码 0=通过，非 0=失败）。
2. 验证器**不能**依赖网络、不能依赖私有数据、不能修改被测项目源码。
3. 验证器必须精确复现问题：断言应指向问题根因，而不是「随便跑跑就过」。
4. 如果该问题**无法**用自动化测试验证，请明确返回 kind="checklist" 并给出可逐条判断的清单，
   不要编造一个假测试。
5. 输出必须是**合法 JSON**，不要输出任何 JSON 之外的解释文字。"""


SYSTEM_FIX = """你是资深软件工程师，正在修复一个真实的开源项目 Issue。
你会拿到仓库代码、问题描述、以及一个用于验证修复的验证器。

铁律：
1. 只做**最小必要修改**，不要顺手重构、不要改格式、不要动与问题无关的文件。
2. 修复后必须让验证器通过（fail-to-pass）。
3. 不得修改验证器本身来「骗过」测试。
4. 不得触碰受保护路径（如 CI 配置、LICENSE、锁文件）。
5. 输出必须是**合法 JSON**，不要输出任何 JSON 之外的解释文字。"""


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------


def render_labels(labels: Sequence[str]) -> str:
    return "、".join(labels) if labels else "（无）"


def render_item(item: RawItem, *, include_comments: bool = True, include_files: bool = True) -> str:
    """把条目渲染成紧凑的文本，供提示词注入。"""
    parts: list[str] = []
    kind = "Pull Request" if item.item_type is ItemType.PR else "Issue"
    parts.append(f"【{kind}】{item.repo} #{item.number}")
    parts.append(f"标题：{item.title}")
    parts.append(f"状态：{item.state}｜作者：{item.author}（{_author_cn(item.author_kind)}）")
    parts.append(f"标签：{render_labels(item.labels)}")

    if item.item_type is ItemType.PR:
        parts.append(
            f"变更：+{item.additions}/-{item.deletions}，{item.changed_files} 个文件"
            f"｜草稿：{'是' if item.is_draft else '否'}｜已合并：{'是' if item.merged else '否'}"
        )
        if item.linked_issues:
            parts.append(f"关联 Issue：{', '.join('#' + str(n) for n in item.linked_issues)}")

    body = (item.body or "").strip()
    if body:
        parts.append(f"\n正文：\n{_truncate(body, MAX_BODY_CHARS)}")

    if include_comments and item.comments:
        parts.append(f"\n评论区（共 {len(item.comments)} 条，最多展示 {MAX_COMMENTS} 条）：")
        for c in item.comments[:MAX_COMMENTS]:
            parts.append(f"- @{c.author}：{_truncate(c.body.strip(), MAX_COMMENT_CHARS)}")

    if include_files and item.files:
        parts.append("\n变更文件：")
        for f in item.files[:40]:
            parts.append(f"- {f.path}（{f.status}，+{f.additions}/-{f.deletions}）")

    return "\n".join(parts)


def render_files_diff(item: RawItem, *, max_chars: int = MAX_DIFF_CHARS) -> str:
    """把 PR 的 patch 内容拼接（评代码质量用）。"""
    chunks: list[str] = []
    total = 0
    for f in item.files:
        if not f.patch:
            continue
        piece = f"--- {f.path} ---\n{f.patch}"
        if total + len(piece) > max_chars:
            chunks.append("（diff 过长，已截断）")
            break
        chunks.append(piece)
        total += len(piece)
    return "\n".join(chunks) if chunks else "（无 diff 内容）"


def _author_cn(kind: AuthorKind) -> str:
    return {
        AuthorKind.MAINTAINER: "维护者",
        AuthorKind.COMMUNITY: "社区贡献者",
        AuthorKind.AI_AGENT: "AI 代理",
        AuthorKind.BOT: "机器人",
        AuthorKind.UNKNOWN: "未知",
    }[kind]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原文 {len(text)} 字符）"


def render_repo_context(
    *,
    test_hint: str | None = None,
    language_hint: str | None = None,
    file_tree: Sequence[str] | None = None,
    readme: str | None = None,
) -> str:
    """仓库上下文（沙盒/修复阶段用）。"""
    parts: list[str] = []
    if language_hint:
        parts.append(f"主要语言：{language_hint}")
    if test_hint:
        parts.append(f"测试运行方式提示：{test_hint}")
    if file_tree:
        parts.append("仓库文件（节选）：\n" + "\n".join(f"- {p}" for p in file_tree[:200]))
    if readme:
        parts.append("README 摘要：\n" + _truncate(readme.strip(), 3000))
    return "\n".join(parts) if parts else "（无额外上下文）"


# ---------------------------------------------------------------------------
# 1. 分类：BUG / FEATURE
# ---------------------------------------------------------------------------


def classification_prompt(item: RawItem) -> list[dict[str, str]]:
    """判断属于 BUG 还是 FEATURE —— 决定走哪条流水线。"""
    user = f"""请判断下面这个条目属于 BUG 还是 FEATURE。

判定标准：
- **bug**：描述的是「东西坏了 / 行为不符合预期 / 报错 / 崩溃 / 性能退化 / 回归」。
- **feature**：描述的是「希望新增能力 / 增强现有功能 / 改进体验」。
- 无法判断时填 unknown，并把 confidence 调低。

{render_item(item, include_files=False)}

只输出 JSON：
{{
  "category": "bug | feature | unknown",
  "confidence": 0.0-1.0,
  "reason": "一句话理由",
  "evidence": ["原文片段1", "原文片段2"]
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 2. 评测：四维评分 + 标签 + 反刷子
# ---------------------------------------------------------------------------


def evaluation_prompt(
    item: RawItem,
    *,
    threshold_hint: str | None = None,
    similar_issues: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """四维评分 + 建议标签 + 处理动作 + 刷子信号。"""
    is_pr = item.item_type is ItemType.PR

    dims = [
        "- **authenticity**（真实性，0-100）：这是真问题/真需求吗？还是误用、环境问题、重复、刷量？",
        "- **importance**（重要性，0-100）：影响多少用户？是否阻塞使用/丢数据/安全隐患？",
        "- **feasibility**（可行性，0-100）：修复或实现的难度与确定性。越高越好做。",
    ]
    if is_pr:
        dims.append(
            "- **pr_quality**（PR 质量，0-100）：改动是否聚焦、是否有测试、是否破坏兼容、是否像刷量或 AI 灌水。"
        )
        dims.append(
            "- **difficulty**（修复难度，0-100）：**越低越好修**。指把这个 PR 改到可合并所需的返工量。"
        )
    else:
        dims.append(
            "- **difficulty**（修复难度，0-100）：**越低越好修**。这是本地化的小改动，还是牵一发动全身？"
        )

    dup_hint = ""
    if similar_issues:
        lines = [
            f"- #{s.get('number')} {s.get('title', '')}（状态 {s.get('state', '')}）"
            for s in similar_issues[:10]
        ]
        dup_hint = (
            "\n\n本仓库中标题相似的既有条目（判断是否为重复提交时参考，注意只是标题相似，"
            "需结合内容判断，不要仅凭标题就判定重复）：\n" + "\n".join(lines)
        )

    kind = "Pull Request" if is_pr else "Issue"
    user = f"""请对下面这个 {kind} 做四维评测。

评分维度：
{chr(10).join(dims)}

打分要求：
- 每维 0-100 整数；**必须**给出 reason，并尽量在 evidence 中引用原文片段。
- 信息不足以判断时，给出中间分（40-60）并把 confidence 调低。
{threshold_hint or ''}

另外请给出：
- `category`：bug | feature | unknown
- `labels_suggested`：建议打上的标签（英文小写，如 bug、needs-info、duplicate、enhancement、security）
- `action`：建议动作，取值之一 fix_now | triage | answer | backlog | close | needs_info
- `summary`：一到两句话的结论，给维护者看
- `spam`：反刷子信号，字段 is_spam / is_duplicate / is_ai_generated / duplicate_of(数组) / reasons(数组)
  - `is_duplicate=true` 时，`duplicate_of` **必须**是重复对象的**编号数组**（如 `[2]`、`[2, 7]`），
    **不要**写标题、也不要只把编号写进 reasons 文字里；找不到编号才留空数组。

{render_item(item)}
{dup_hint}

只输出 JSON：
{{
  "category": "bug | feature | unknown",
  "scores": {{
    "authenticity": {{"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}},
    "importance":   {{"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}},
    "feasibility":  {{"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}},
    "difficulty":   {{"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}}{',' if is_pr else ''}
    {'"pr_quality":  {"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}' if is_pr else ''}
  }},
  "labels_suggested": ["…"],
  "action": "triage",
  "summary": "…",
  "spam": {{"is_spam": false, "is_duplicate": false, "is_ai_generated": false, "duplicate_of": [], "reasons": []}}
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


def pr_quality_prompt(item: RawItem, *, diff: str) -> list[dict[str, str]]:
    """仅在需要深度评 PR 代码质量时调用（Q1 分层：按需升级）。"""
    user = f"""请评估下面这个 Pull Request 的**代码质量**与**是否解决其声称的问题**。

重点关注：
1. 改动是否聚焦（有无夹带无关修改、格式化全文件、大范围重构）。
2. 是否有测试、测试是否真的覆盖了所声称修复的问题。
3. 是否破坏向后兼容（改公开 API、改默认行为、删导出符号）。
4. 是否有明显缺陷（空指针、竞态、资源泄漏、注入风险、错误吞掉）。
5. 是否有刷量特征（几乎无实质改动、仅改注释/README、复制粘贴的样板）。

{render_item(item, include_comments=True)}
{("\n关联 Issue：" + ", ".join('#' + str(n) for n in item.linked_issues)) if item.linked_issues else ""}

变更内容（diff）：
{diff}

只输出 JSON：
{{
  "pr_quality": {{"score": 0, "reason": "…", "evidence": ["…"], "confidence": 0.0}},
  "solves_stated_issue": true,
  "risk_notes": ["潜在风险1"],
  "labels_suggested": ["…"],
  "summary": "…"
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 3. 验证器生成
# ---------------------------------------------------------------------------


def verifier_prompt(
    item: RawItem,
    *,
    repo_context: str,
    mode: str = "hybrid",
    test_hint: str | None = None,
    linked_context: str | None = None,
) -> list[dict[str, str]]:
    """为 Issue/PR 生成验证器（可执行测试优先，无法自动化则清单）。

    ``linked_context``：关联 Issue 的正文。PR 作者往往只在正文里复述自己修的那**一个**
    场景，而该问题完整的复现用例/期望输出写在被修的 Issue 里——不喂这段材料，验证器
    只会覆盖 PR 自己提到的场景，从而放过「只修一半」的修复。
    """
    mode_rule = {
        "executable": '必须生成可执行测试（kind="executable"）。若确实无法自动化，仍选 executable 并尽量给出最接近的检查命令。',
        "checklist": '请生成自然语言验证清单（kind="checklist"），不写代码。',
        "hybrid": '优先生成可执行测试（kind="executable"）；若该问题确实无法用自动化测试验证，则返回 kind="checklist" 并给出可逐条判断的清单。',
    }.get(mode, "优先可执行测试，无法自动化则返回清单。")

    user = f"""请为下面的问题编写一个**验证器**，用于证明「修复前问题存在，修复后问题消失」。

要求：
1. {mode_rule}
2. 验证器运行结果用退出码表达：0=通过（问题已修复），非 0=失败（问题仍存在）。
3. `files` 字段是「沙盒内相对路径 → 文件内容」的映射，路径必须相对仓库根目录。
4. `command` 是在仓库根目录执行的运行命令，例如 `python -m pytest tests/test_xxx.py -q`。
5. **禁止**依赖网络、禁止修改被测项目源码、禁止使用私有数据。
6. 测试必须**精确指向问题根因**：修复前必定失败；不能写成恒真或恒假的测试。
7. **逐条覆盖正文里列出的每一个复现用例与期望输出**：凡是正文中明确给出的输入/期望
   对（复现步骤、期望输出、验收标准、示例表格里的每一行），都必须各写一条断言。
   只挑其中一两个同类场景会导致验证器放过「只修了一半」的修复。
8. 若正文给出了多个语义不同的场景（例如「连续空白」与「空白+连字符混排」），
   它们必须**都有断言**——不要因为它们看起来相似就合并成一条。
9. **正文里的兼容性声明必须转成回归断言**：正文写出「其余用例不受影响」「保持向后兼容」
   「不改变既有行为」「不影响 X」这类**约束性表述**时，除了目标用例，还要为它写**回归断言**
   ——即断言那些**修复前就已成立**的行为，修复后仍须成立。
   * 为什么要写：这类断言在 base 阶段本就通过（不影响 base 是否失败），但它能在修复阶段拦住
     「改对了目标、却弄坏了别处」的修复——整套测试只要有一条失败，验证器即判不通过。
   * 断言什么：优先断言正文已列出的既有行为（如正文说「`word_count("")` 应返回 0，
     其余用例不受影响」并给出 `word_count("hello world") == 2`，就把这条也写进去）；
     若你从被测代码或文档能看出该函数对某类输入的**既有语义**，而某个可能的修复方式会
     改变它，也应一并断言。
   * 只锁「正文明确要求」或「既有实现已具备」的行为，**不要臆造**新的行为契约。
10. 写完后自查一遍：把正文里的每个期望输出逐一对照，确认每一条都有对应断言，
    且目标用例的断言是**能被未修复代码区分开**的（否则 base 阶段会直接通过）；
    同时确认第 9 条要求的回归断言都已写上。
11. 若已有测试框架，优先复用（如项目用 pytest 就写 pytest 用例）。

仓库上下文：
{repo_context}

问题描述：
{render_item(item, include_comments=True)}
{(chr(10) + '被本 PR 修复的原 Issue（复现用例与期望输出以它为准，必须逐条覆盖）：' + chr(10) + _truncate(linked_context.strip(), 6000)) if linked_context and linked_context.strip() else ''}
{("运行测试的方式提示：" + test_hint) if test_hint else ""}

只输出 JSON：
{{
  "kind": "executable | checklist | shell",
  "name": "验证器简短名称",
  "language": "python | javascript | go | java | ruby | shell",
  "files": {{"tests/test_reproduce.py": "文件完整内容"}},
  "command": "python -m pytest tests/test_reproduce.py -q",
  "checklist": [],
  "expect_fail_on_base": true,
  "expect_pass_on_fix": true,
  "timeout_seconds": 600,
  "notes": "验证思路说明：为什么这个验证器能区分修复前后"
}}"""
    return [
        {"role": "system", "content": SYSTEM_VERIFIER},
        {"role": "user", "content": user},
    ]


def checklist_judge_prompt(item: RawItem, *, checklist: Sequence[str], output: str) -> list[dict[str, str]]:
    """把可执行验证器降级为「人/模型读输出判定」时使用。"""
    items = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(checklist))
    user = f"""下面是针对某个问题的验证清单，以及执行验证后得到的输出。
请逐条判断是否满足，并给出整体结论。

验证清单：
{items}

执行输出（可能包含日志、测试结果、报错）：
```
{_truncate(output, 8000)}
```

问题背景：
{render_item(item, include_comments=False)}

只输出 JSON：
{{
  "passed": true,
  "items": [{{"index": 1, "satisfied": true, "reason": "…"}}],
  "conclusion": "一句话结论"
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 4. 批量 flush 结论文案
# ---------------------------------------------------------------------------


def batch_conclusion_prompt(
    *,
    queue_kind: str,
    entries: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    """队列攒够一批后，统一交给 AI 出结论并打标签（Q1 的批量环节）。

    ``queue_kind``：``verify``（Issue-BUG 验证队列）/ ``fix``（PR 合并验证队列）。
    """
    blocks: list[str] = []
    for i, e in enumerate(entries, 1):
        blocks.append(
            f"""### 条目 {i}：{e.get('key')}
标题：{e.get('title')}
分类：{e.get('category')}
既有评分：{e.get('scores')}
验证器类型：{e.get('verifier_kind')}
base 阶段（修复前）结果：{e.get('base_outcome')}（exit={e.get('base_exit')}）
修复/合并后结果：{e.get('fix_outcome')}（exit={e.get('fix_exit')}）
是否满足 fail-to-pass：{e.get('f2p')}
验证输出摘要：
```
{_truncate(str(e.get('output') or ''), 3000)}
```
"""
        )

    if queue_kind == "verify":
        task = """这些是 **Issue-BUG** 的验证结果。请为每一条判断：
1. 问题是否被**成功复现**（验证器在修复前确实失败）。
2. `verdict` 取 fix（值得自动修复）| needs_review（需人工确认）| skip（验证器不可靠/问题不成立）。
3. 打上合适的标签（如 reproduced、verified、needs-info、invalid、ai-verifier）。
4. `confidence` 0-1。"""
    else:
        task = """这些是 **Pull Request** 的合并验证结果。请为每一条判断：
1. 合并该 PR 后，验证器是否通过（功能确实可用）。
2. `verdict` 取 merge（建议合并）| reject（建议关闭）| needs_review（需人工复核）。
3. 打上合适标签（如 verified、functionality-verified、needs-review、ai-verified）。
4. `confidence` 0-1。

注意：是否可以合并**只作建议**，最终由维护者手动合并。"""

    user = f"""{task}

请严格按条目逐条输出（顺序与输入一致）：

{chr(10).join(blocks)}

只输出 JSON（results 数组长度必须等于 {len(entries)}）：
{{
  "results": [
    {{
      "key": "条目 key 原样返回",
      "verdict": "fix | merge | reject | needs_review | skip",
      "labels": ["…"],
      "priority": "tier1 | tier2 | none",
      "reason": "结论理由",
      "confidence": 0.0
    }}
  ]
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


def fix_plan_prompt(
    *,
    items: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    """批量决定「哪些 Issue 先修、按什么顺序修」（Q1 的优先级规则）。"""
    blocks = [
        f"""- {i.get('key')}｜{i.get('title')}
  难度={i.get('difficulty')}｜重要性={i.get('importance')}｜可行性={i.get('feasibility')}｜真实性={i.get('authenticity')}
  是否已验证复现：{i.get('verified')}｜验证器类型：{i.get('verifier_kind')}"""
        for i in items
    ]
    user = f"""下面是已评测的一批 Issue-BUG。维护者的修复优先级规则是：
1. **第一优先**：修复难度低 + 重要性高 → 立即自动修复。
2. **第二优先**：修复难度低 + 重要性低 → 可以自动修复。
3. 其余（难度高）→ **不要自动修复**，打标签等开发者处理。

请为每一条给出 `priority`（tier1 | tier2 | none）与 `reason`。

{chr(10).join(blocks)}

只输出 JSON：
{{
  "results": [
    {{"key": "…", "priority": "tier1 | tier2 | none", "reason": "…", "confidence": 0.0}}
  ]
}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 5. 自动修复 Agent
# ---------------------------------------------------------------------------


def fix_agent_step_prompt(
    item: RawItem,
    *,
    repo_context: str,
    verifier_command: str,
    last_test_output: str | None,
    round_index: int,
    max_rounds: int,
    read_files: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Agent 循环中的单步提示：让模型决定「读哪些文件 / 改哪些文件」。

    模型只需输出结构化的动作，实际文件读写与命令执行由我们（受控地）完成，
    这样沙盒与权限边界始终在我们手里。
    """
    file_blocks = ""
    if read_files:
        parts = []
        for path, content in list(read_files.items())[:20]:
            parts.append(f"--- {path} ---\n{_truncate(content, 6000)}")
        file_blocks = "\n\n已读取的文件内容：\n" + "\n\n".join(parts)

    last_output = ""
    if last_test_output:
        last_output = f"\n上一轮验证器执行输出：\n```\n{_truncate(last_test_output, 4000)}\n```\n"

    user = f"""你正在第 {round_index}/{max_rounds} 轮修复中。

验证器运行命令：`{verifier_command}`
{last_output}
仓库上下文：
{repo_context}
{file_blocks}

请决定下一步动作。可选动作：
- `read`：读取若干文件（给出 paths）。
- `write`：写入文件（给出 files：路径 → 完整内容）。
- `run`：运行验证器（命令由系统固定，你只需说 run）。
- `done`：你认为已修复（系统会跑一次验证器确认）。
- `give_up`：问题超出能力范围，放弃并说明原因。

约束：
- 只允许修改与问题相关的文件；**禁止**修改验证器文件、CI 配置、LICENSE、锁文件。
- 一次最多改 20 个文件、总 diff 不超过 800 行。
- 给出 `files` 时必须是**完整文件内容**（不是片段）。

问题：
{render_item(item, include_comments=False)}

只输出 JSON：
{{
  "action": "read | write | run | done | give_up",
  "paths": ["要读的文件路径"],
  "files": {{"要写入的路径": "完整文件内容"}},
  "reason": "这一步的思路",
  "summary": "若 done：修复要点；若 give_up：原因"
}}"""
    return [
        {"role": "system", "content": SYSTEM_FIX},
        {"role": "user", "content": user},
    ]


def fix_pr_body_prompt(
    item: RawItem,
    *,
    summary: str,
    changed_files: Sequence[str],
    verifier_command: str | None,
    f2p_ok: bool,
) -> list[dict[str, str]]:
    """生成 PR 描述。"""
    user = f"""请为这个自动生成的修复 PR 写一份简洁、诚实的中文描述。

要求：
- 说明修复了什么、根因是什么、改动范围。
- 明确标注「本 PR 由 AI 自动生成」以及验证方式与结果。
- 不要夸大；若验证不充分要写清楚。

原问题：
{render_item(item, include_comments=False, include_files=False)}

修复摘要：{summary}
改动文件：{', '.join(changed_files) or '（无）'}
验证器命令：{verifier_command or '（无）'}
fail-to-pass 验证是否通过：{f2p_ok}

只输出 JSON：
{{
  "title": "简短标题（中文，含修复要点）",
  "body": "Markdown 格式的 PR 描述正文"
}}"""
    return [
        {"role": "system", "content": SYSTEM_FIX},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 6. 维护者可读的摘要与通知文案
# ---------------------------------------------------------------------------


def digest_prompt(*, stats: dict[str, Any], highlights: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """为通知渠道生成一段自然语言的巡检摘要。"""
    blocks = [
        f"- [{h.get('kind')}] #{h.get('number')} {h.get('title')}｜{h.get('line')}"
        for h in highlights[:20]
    ]
    user = f"""请根据下面的扫描与评测统计，写一段给开源维护者看的简短中文日报（200 字以内）。
要点先行，突出「必须今天看的」条目，其余一句话带过。不要客套话。

统计：
{stats}

重点条目：
{chr(10).join(blocks) or '（无）'}

只输出 JSON：
{{"summary": "日报正文（Markdown）", "top_actions": ["建议动作1", "建议动作2"]}}"""
    return [
        {"role": "system", "content": SYSTEM_TRIAGE},
        {"role": "user", "content": user},
    ]
