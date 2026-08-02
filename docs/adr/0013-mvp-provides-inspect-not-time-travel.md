---
status: accepted
scope: mvp-baseline
---

# MVP 提供 Run Inspect 而不提供 Time Travel

最小 Product MVP Baseline 提供 Run History 和授权只读 Run Inspect，并支持 Worker
从最新权威 checkpoint 自动恢复以及用户通过 exact InterruptRef 恢复等待中的 Run。
它不支持 replay、fork dry-run、fork commit 或从任意历史 checkpoint 继续执行。

用户可以重新提交相同业务输入，但这会创建普通 live Run，使用新的 submission
和当前受控 binding；系统不宣称它重现历史模型或依赖结果。

## Consequences

- MVP 前端不显示 replay/fork/time-travel 入口，terminal Run 只能 Inspect 或
  重新提交新任务。
- MVP 不实现 nondeterministic seam recording、历史 Runtime Build worker 路由、
  replay retention 或长期 artifact pinning。
- POC-B 和 N-26 不阻断 Core Release 或最小 Product MVP；ADR-0005 仍定义未来
  Time Travel 的安全语义，避免后续以原地回退或重新调用真实 seam 冒充 replay。
- Time Travel 只有在所有已启用非确定性 seam 都能稳定录制、完整匹配且缺失时
  fail fast 后才能形成独立发布能力。
