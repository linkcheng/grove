# WS-5 本地签发与 G2 运行手册

适用范围（ROADMAP 2026-08-20 范围调整）：development/test/integration 与 MVP G2。
外部 issuer ceremony 与 Core IAR 属于 WS-7 前置，不在本手册范围。

## 0. 前提

- `uv sync --frozen` 后的环境（签发与运行使用同一环境，manifest 中的
  openai/pydantic-ai 分发指纹与 adapter 文件指纹按当前安装计算）。
- 真实 AI gateway 的凭据只在环境变量中出现，绝不进入仓库、日志或 evidence。
- `GROVE_RUNTIME_BUILD_HASH`（64 位 sha256）已确定；worker 侧使用相同值。

## 1. 生成密钥材料（一次性，仓库外保存）

```bash
uv run python scripts/ws5_issue_provider_binding.py --generate-keys /secure/grove-g2-keys
```

产出 `root-private.seed` 与 `issuer-private.seed`（32 字节 Ed25519 seed，0600）。
目录必须在仓库外；丢失 root seed 即失去该信任根的全部签发能力。

## 2. 签发 release 链

```bash
uv run python scripts/ws5_issue_provider_binding.py \
  --output-dir /run/grove/release \
  --root-private-key /secure/grove-g2-keys/root-private.seed \
  --issuer-private-key /secure/grove-g2-keys/issuer-private.seed \
  --app-env test \
  --gateway-url "$AI_GATEWAY_URL" \
  --gateway-model "$AI_GATEWAY_MODEL" \
  --credential-slot-id "$AI_GATEWAY_CREDENTIAL_SLOT_ID" \
  --runtime-build-hash "$GROVE_RUNTIME_BUILD_HASH"
```

产物：`authority/`（root 公钥 + trust policy + policy 签名，只读）、
`core-release-identity.json`、`core-release-expected-facts.json`、
`core-release-expected-facts.signature.json`、`provider-binding-manifest.json`、
`release-pins.json`。stdout 逐行打印 9 个 `export` 语句。

注意：

- 签发**不需要**真实 API key（manifest 只绑定 url/model/slot id）。
- runtime build、依赖或 adapter 代码任何变化都会改变指纹，必须重新签发。
- 输出目录必须为空；工具拒绝覆盖。

## 3. 运行 G2（本地 transport 协议验证 / 真实 provider）

```bash
# 协议验证（无网络）：由测试签发链 + MockTransport 完成，
# 见 tests/releases/test_ws5_issuance_tool.py。

# 真实 provider（生产环境需 HTTPS 网关）：
export AI_GATEWAY_URL=... AI_GATEWAY_API_KEY=... AI_GATEWAY_MODEL=... \
       AI_GATEWAY_CREDENTIAL_SLOT_ID=...
# 加上第 2 步打印的 9 个 export
GROVE_RUN_PROVIDER_G2=1 uv run pytest tests/integration/test_pydantic_ai_gateway.py -m integration
```

通过标准：恰好 1 次物理请求（transport 计数）、structured output 正确、
usage/pricing 与 Manifest 一致；任何 pin/签名/指纹不匹配都 fail closed
（`INVALID_BINDING`），且不会发出 provider 请求。

## 4. 常见失败

| 症状 | 含义 |
|---|---|
| `invalid_binding`（验证器前） | 9 个环境变量缺失/为空，或文件打不开 |
| verifier 非零退出 | 签名/pin/候选不一致 — 重签发，勿改 pins 去迁就 |
| `_validate_runtime_binding` 失败 | build/SDK/adapter 指纹漂移 — 环境与签发时不一致，重签发 |
| `AI_GATEWAY_CREDENTIAL_SLOT_ID` 不匹配 | 环境槽位名与 Manifest 不同 — 使用签发时的同一槽位名 |
