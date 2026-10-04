<!--
title: 支持按小时计费与自定义计费周期
labels: enhancement
target: FEATURE（只评测打标签，不生成验证器、不自动修复）
-->

## 需求背景

`prorate` / `refund` 目前只支持**按天**分摊。但 SaaS 订阅里
按小时（按量计费的云资源）、按周、按月的计费周期同样常见，
调用方不得不在业务层自己乘除，容易和库里的取整规则打架。

## 期望能力

1. **可指定周期单位**：
   ```python
   prorate_hours(total, hours_used, hours_total)
   # 或
   prorate(total, used, total, *, unit="day" | "hour" | "week")
   ```
2. **自定义周期**：允许传入 `period=timedelta(...)`，按时间比例分摊。
3. **与统一舍入口径联动**（见 issue #6）：新增的取整规则必须与既有 API 一致，
   不能又引入一套新的舍入方式。
4. **兼容现有行为**：`prorate(total, days_used, days_total)` 的默认语义保持不变。

## 实现提示

- 内部的「按比例 + 取整」逻辑可以抽成一个私有函数，`prorate`/`refund`/新 API 共用，
  避免第三份拷贝。
- 单位换算建议用 `decimal` 或按最小单位整数运算（同样受 #6 约束）。
- 建议新增参数而非改变默认行为：
  ```python
  prorate(total, used, total, *, unit="day")
  ```

## 验收标准

- 新增按小时/按周分摊的用例
- 既有按天用例全部保持通过
- README 补充新的 `unit` 参数与计费周期说明
- 舍入行为与既有 API 一致

## 环境

- ratekit 0.4.0
- Python 3.10+
