---
status: accepted
scope: core-migration-recovery
---

# 含运行事实的数据库不执行 schema downgrade

GROVE 将 `upgrade head → downgrade base → upgrade head` 限定为 WS-0 对空白、可销毁
迁移验证数据库的证据流程。migration report 必须在当前 PostgreSQL 实例中创建严格命名
的一次性数据库，完成真实迁移回环和 catalog 查询后终止连接并删除该数据库；不得在
integration 主库、线上库或任何包含 Execution 运行事实的数据库上制造该证据。

线上回滚使用已知可运行制品、channel rollback、数据库 restore 或 roll-forward。schema
downgrade 不是运行数据恢复协议。当目标 schema 无法表达 `consumed`、`leased`、
`dead_letter`、`running`、`succeeded`、非零 command sequence、lease/fence/retry、
Runtime Build、checkpoint 或 claim provenance 时必须 fail closed，稳定返回
`WS3_DOWNGRADE_INCOMPATIBLE_LIVE_DATA`，且必须发生在任何 DDL 前。

禁止通过以下有损映射让旧约束表面通过：

- `consumed`、`leased` 或 `dead_letter` 改回 `pending`；
- `running`、`succeeded`、`failed` 或 `cancelled` 改回 `accepted`；
- 删除 checkpoint、Runtime Build 或 provenance 后把同一 command 当作未执行；
- 在失败后删除固定 integration volume、刷新 collation version 或重复执行确定性错误来
  获得绿色结果。

运维降级只使用 `scripts/ws3_downgrade.py`。wrapper 先执行独立只读预检；Alembic
environment 随后在实际 downgrade 的同一事务中再次取得写互斥锁并执行相同预检，从而
关闭“预检通过后、DDL 前又写入运行事实”的竞态。offline downgrade 因无法检查 live
data 而拒绝。已发布的 `0003` 及其他历史 revision 不因该政策重新改写。

PostgreSQL 测试镜像必须在 Compose 启动前解析为精确 `sha256:` image ID。已有 test
volume 若来自不同 libc/collation 版本，应作为明确的可销毁测试资产重建；不得只执行
`REFRESH COLLATION VERSION` 掩盖未重建索引的风险。

## Consequences

- integration 主库只执行 roll-forward `upgrade head`；migration report 的完整回环与
  主库运行数据隔离。
- 空白一次性数据库仍必须真实完成完整迁移回环，成功、失败、超时和取消都必须清理。
- 含 Execution 运行事实的 downgrade 在 revision、数据、函数、约束改变前稳定失败。
- 连接未就绪、数据库启动、暂态网络和明确锁竞争可以有界重试；constraint violation、
  migration drift、SQL syntax error 与 hash mismatch 立即退出。
- 数据库 rollback 证据由 restore/roll-forward 演练提供，不再由 live schema downgrade
  冒充。
