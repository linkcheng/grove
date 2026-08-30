# WS-7 负责人手工走查清单

> 用途：MVP Functional Completion 验收（任务书 Exit Invariant 2）。
> 走查环境：`compose.local.yaml` 单命令本地全套（README "本地全套走查"）。
> 请逐项勾选；任一 fail 请记录现象与复现步骤。

## 环境就绪

- [ ] `docker compose -f compose.yaml -f compose.local.yaml up -d --build` 后
      `docker compose ... ps` 显示 db / api / runtime-worker / projection-reconciliation
      四个容器 Up（worker 无崩溃重启）。
- [ ] `docker compose ... run --rm migrate` 输出 head=`ws7_lease_cap_300`。
- [ ] `curl http://127.0.0.1:8000/api/v1/health/ready` 返回 200。

## 1. 提交（组合范围内评估）

- [ ] 按 README "本地全套走查" 的种子 SQL 维护租户组合（≥2 个资产）。
- [ ] 打开前端 Execution Launch，网关令牌/租户/主体与 .env 一致。
- [ ] 提交返回 run id，页面进入 Run 视图。

## 2. 观察（执行过程）

- [ ] Run 视图显示连接状态 connected，"资产状态已固定" milestone
      （含来源 provenance 与记录数）按序到达。
- [ ] 租约上限 300s 内完成（真实推理含结构 gate 重试；正常一次生成
      30–120 秒）。

## 3. 答案（稳定性核心）

- [ ] Run 视图"评估答案"区显示完整中文风险评估：非空、逐资产判断、
      组合结论，并附 content hash。
- [ ] 答案不包含 `$your_answer`、schema/格式说明文本或原始 JSON 回声。
- [ ] 抽查通过：连续 10 轮 × golden 三用例的运行时结构校验零失败
      （证据：`ci-evidence/ws7-stability-probe.json`，由
      `scripts/ws7_stability_probe.py` 生成；本清单的手工提交仅为抽查，
      不是 Exit Invariant 1 的量化来源）。

## 4. 历史

- [ ] History 页列出本会话提交的 run（id、状态、时间）。
- [ ] 选择任一历史 run 可进入详情。

## 5. Inspect（typed 摘要）

- [ ] Inspect 显示 typed 摘要：run 状态、答案正文、provenance
      （source_ref / result hash）、知识条目数，而非原始 JSON dump。
- [ ] 失败路径（可选）：若模型输出不稳定被 gate 拦截，Inspect 显示
      `inference_output_invalid` typed failure，而不是垃圾答案。

## 评审结论

- 评审人：负责人
- 日期：2026-08-30
- 结论：☑ 全部通过——负责人在会话中走查 UI 后口头确认"看着没问题"并授权
  squash 合入 main；判卷依据（组合种子 + 两条冻结政策 + 四点检查项）在
  会话中逐项核对。机器预筛：glm-5.3-flash 多模态六步截图判定 6/6 PASS
  （证据 `ci-evidence/ws7-visual-steps/prescreen.json`，本地不入库）。
- 附注（走查发现，移交 WS-8 UX 范围）：UI 不展示评估题目与组合输入，
  验收者无法从界面自足判卷，需依赖外部判卷标准。
