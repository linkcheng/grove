#!/usr/bin/env python3
"""Generate the WS-6 human-review sheet for the frozen golden dataset.

Runs each golden case through the real asset-risk kernel (real provider via
the issued release chain, real PostgreSQL) and captures the typed artifacts
the owner reviews by hand: the portfolio the run saw, the frozen policy
corpus it cited, and the model's answer.  Gated exactly like the G3 E2E:
without the release-chain environment everything fails closed; no mock
provider is ever used for review evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.asset_risk.golden import GOLDEN_CASES, golden_dataset_hash  # noqa: E402

# The review portfolio: representative, fixed values so the sheet is
# reproducible; the ref names carry the class signal the graph's inference
# context uses (asset_ref + exposure_amount only).
REVIEW_PORTFOLIO = {
    "asset.golden.credit-1": {"asset_class": "credit", "exposure_amount": 1200, "status": "active"},
    "asset.golden.collateral-1": {"asset_class": "collateral", "exposure_amount": 800, "status": "frozen"},
}

POLICY_CORPUS = (
    ("policy.exposure@1", "Board exposure policy"),
    ("policy.collateral@1", "Collateral policy"),
)


def _release_chain_configured() -> bool:
    return all(
        os.environ.get(name)
        for name in (
            "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
            "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
            "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
            "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
            "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
            "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
            "AI_GATEWAY_RELEASE_POLICY_REF",
            "AI_GATEWAY_RELEASE_POLICY_VERSION",
            "AI_GATEWAY_RELEASE_POLICY_SHA256",
        )
    )


async def _seed(migration_url: str, tenant: str) -> None:
    engine = create_async_engine(migration_url)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        for ref, values in REVIEW_PORTFOLIO.items():
            await conn.execute(
                text(
                    "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                    "currency, status, source_revision) VALUES (:t, :ref, :class, :amount, 'CNY', :status, 'rev-1') "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "t": tenant,
                    "ref": ref,
                    "class": values["asset_class"],
                    "amount": values["exposure_amount"],
                    "status": values["status"],
                },
            )
    await engine.dispose()


async def run_cases(runtime_url: str, app_env: str, build_hash: str, tenant: str) -> list[dict[str, Any]]:
    from app.asset_risk.composition import compose_asset_risk_kernel
    from app.worker.inference import production_inference_lifespan

    engine = create_async_engine(runtime_url)
    rows: list[dict[str, Any]] = []
    try:
        async with production_inference_lifespan(app_env=app_env, runtime_build_hash=build_hash) as (port, factory):
            kernel = compose_asset_risk_kernel(
                inference_port=port,
                inference_request_factory=factory,
                runtime_session_factory=async_sessionmaker(engine, expire_on_commit=False),
                worker_tenant_id=tenant,
            )
            graph: Any = kernel.build_graph()
            for case in GOLDEN_CASES:
                terminal = await graph.ainvoke(
                    {
                        "stage": "start",
                        "tenant_id": tenant,
                        "run_id": str(uuid4()),
                        "asset_refs": case.asset_refs,
                    }
                )
                if terminal.get("stage") != "terminal":
                    raise RuntimeError(f"golden case {case.case_ref} failed: {terminal.get('failure_class')}")
                rows.append(
                    {
                        "case_ref": case.case_ref,
                        "assets": terminal["asset_view"]["assets"],
                        "answer": terminal["report"]["answer"],
                        "knowledge_items": terminal["report"]["knowledge_items"],
                    }
                )
    finally:
        await engine.dispose()
    return rows


def render_markdown(rows: list[dict[str, Any]], executed_at: str) -> str:
    lines = [
        "# WS-6 Human Review 评审表（golden dataset 答案质量）",
        "",
        "> 由 `scripts/ws6_human_review_sheet.py` 在真实环境生成（真实 PostgreSQL + 真实 provider，",
        f"> 签发链门控）；golden dataset hash `{golden_dataset_hash()}`，执行时间 {executed_at}。",
        "> 评审对象：模型答案的业务质量。结构完整性已由确定性评估器覆盖（`8bdaad2`），不在本表。",
        "> 每例给出：Run 实际看到的组合、引用的冻结政策、模型答案、docs/31 §7 业务预期。",
        '> **请逐例在"负责人评审"栏填写 pass / fail + 意见。**',
        "",
        "## 评审输入（固定组合，可复现）",
        "",
        "| asset_ref | asset_class | exposure_amount (CNY) | status |",
        "|---|---|---|---|",
    ]
    for ref, values in REVIEW_PORTFOLIO.items():
        lines.append(f"| `{ref}` | {values['asset_class']} | {values['exposure_amount']} | {values['status']} |")
    lines += [
        "",
        "## 冻结政策语料（所有 Run 引用同一 Knowledge Snapshot）",
        "",
    ]
    for ref, title in POLICY_CORPUS:
        lines.append(f"- `{ref}` — {title}")
    lines += [
        "",
        "## 逐例评审",
        "",
    ]
    expectations = {
        "golden.asset-risk.credit-single@1": (
            "答案应以信用敞口政策为依据，就单一信用资产 1,200 CNY 是否在董事会批准的类别限额内"
            "给出明确判断或明确说明所需限额信息；不得编造未引用的事实。"
        ),
        "golden.asset-risk.collateral-1@1": (
            "答案应以抵押品政策为依据：该资产为 frozen 状态（政策明确 frozen 资产不提供敞口减免）；"
            "不得将 frozen 资产当作有效减免来源。"
        ),
        "golden.asset-risk.mixed-portfolio@1": (
            "答案应同时覆盖两条政策：组合内信用资产限额判断 + frozen 抵押资产零减免；"
            "对组合给出整体结论，不得遗漏任一资产。"
        ),
    }
    for row in rows:
        lines += [f"### {row['case_ref']}", ""]
        lines += ["**Run 看到的资产组合**：", "", "| asset_ref | class | exposure | status |", "|---|---|---|---|"]
        for entry in row["assets"]:
            lines.append(
                f"| `{entry['asset_ref']}` | {entry['asset_class']} | {entry['exposure_amount']} | {entry['status']} |"
            )
        lines += ["", f"**引用知识条目数**：{row['knowledge_items']}（冻结语料为 2 条政策）", ""]
        lines += ["**模型答案**：", "", "```text", str(row["answer"]), "```", ""]
        lines += ["**业务预期（docs/31 §7 基线）**：", "", expectations.get(row["case_ref"], "—"), ""]
        lines += ["**负责人评审**：", "", "- [ ] pass", "- [ ] fail（意见：____________________）", "", "---", ""]
    lines += [
        "## 评审结论（全部用例 pass 方可关闭 G3 human review 项）",
        "",
        "- 评审人：____________________",
        "- 日期：____________________",
        "- 结论：☐ 全部通过  ☐ 存在 fail（列出用例与处理决定）",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
        ),
    )
    parser.add_argument(
        "--seed-database-url",
        default=os.environ.get("GROVE_MIGRATION_DATABASE_URL", _default_seed()),
    )
    parser.add_argument("--app-env", default=os.environ.get("GROVE_WS6_APP_ENV", "test"))
    parser.add_argument("--runtime-build-hash", default="e" * 64)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/work-packages/WS-6-human-review-sheet.md")
    args = parser.parse_args()
    if not _release_chain_configured():
        print("release chain environment is not configured; refusing to fake review evidence", file=sys.stderr)
        return 1

    tenant = f"ws6-review-{uuid4().hex[:8]}"
    asyncio.run(_seed(args.seed_database_url, tenant))
    rows = asyncio.run(run_cases(args.database_url, args.app_env, args.runtime_build_hash, tenant))
    from datetime import UTC, datetime

    markdown = render_markdown(rows, datetime.now(UTC).isoformat())
    args.output.write_text(markdown + "\n")
    print(json.dumps({"cases": len(rows), "output": str(args.output)}, indent=2))
    return 0


def _default_seed() -> str:
    runtime = os.environ.get(
        "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
    )
    return runtime.replace("grove_runtime:grove_runtime_ws0", "grove_migration:grove_migration_ws0")


if __name__ == "__main__":
    raise SystemExit(main())
