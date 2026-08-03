# GROVE WS-0 工程约束

- 依赖通过 uv 管理，提交 `uv.lock`，目标 Python 3.12.12。
- 应用是按角色启动的模块化单体；角色只能是 `api`、`runtime_worker`、`projection_reconciliation`、`offline_governance`。
- WS-0 不创建业务表，不启用 DBOS，不添加 broker，也不声明生产 Gate 已验证。
- 构建证据只能写入被忽略的 `ci-evidence/`，secret 不得进入日志或 manifest。

## 长期工程经验

- **生命周期集中管理**：`AsyncEngine`、连接池和其他外部资源由应用 lifespan 创建、复用和释放；禁止在每次请求或 Session 创建时重复建立资源。
- **关键配置 fail closed**：角色、schema、数据库 URL、证据模式和镜像 ID 必须显式校验。未知值、拼写错误、dirty source 或缺失证据不得静默回退或返回成功。
- **错误边界和观测性分层**：路由层只转换预期的基础设施异常；程序缺陷继续暴露。真实进程必须输出结构化请求完成日志，至少包含 `trace_id`、`duration_ms` 和 `status`，不能只依赖测试注入的 logger handler。
- **证据内容寻址**：固定文件名只能作为便利别名；Manifest 必须绑定内容寻址路径和 SHA-256，并在验证时读取文件重新计算 hash。篡改证据或重算 Manifest hash 后仍必须被拒绝。
- **迁移证据来自真实数据库**：迁移报告必须实际执行 `upgrade head → downgrade base → upgrade head`，并从数据库查询 head 和关系状态。迁移 hash 统一实现，覆盖 `alembic.ini`、`alembic/env.py`、模板和全部 revisions。
- **明确可复现边界**：Docker daemon 的原始 image ID 可能包含 `CreatedAt` 等构建元数据；需要定义并比较 canonical runtime content digest，同时记录原始 image ID，不得把二者混为一谈。
- **运行镜像最小化**：镜像只复制运行时所需文件，排除测试、数据库初始化脚本和无关工具；使用固定基础镜像 digest 和非 root 用户，并通过实际容器检查验证。
- **测试覆盖反向路径**：除正常流程外，必须测试非法 role、篡改 Manifest/SBOM、数据库不可达、慢探针、权限隔离和真实 PostgreSQL 扩展加载；集成验收不得用 mock DB 冒充真实验证。
- **区分开发 CI 与发布门禁**：`make ci` 只代表开发检查；`make release-check` 才能验证 clean source、完整证据和可发布 Manifest。开发 CI 通过不等于 WS-0 或产品发布完成。
- **控制设计和变更范围**：保持 `create_app` 等入口职责清晰但不过度拆分；优先修复根因，不为未来需求预留空壳。冻结文件、白名单和禁止提交等约束必须保持不变，无法完成的事项记录在 `BLOCKED.md`。
