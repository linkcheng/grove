// Asset Risk Reference Profile renderer — TS port of app/asset_risk/rendering.py.
// The "资产状态已固定" milestone shows only observed_at, record count,
// completeness and the safe provenance reference (docs/31 §6); SQL, table
// names and raw tool payloads are never part of the output surface.

import {
  type DomainViewRenderer,
  type RenderedField,
  RendererRegistry,
} from "./domainViewRenderer";
import type { DomainViewMilestone } from "./types";

export const ASSET_STATE_VIEW_SCHEMA_REF = "AssetStateView@1";
export const ASSET_STATE_VIEW_TITLE = "资产状态已固定";

class AssetStateViewRenderer implements DomainViewRenderer {
  readonly viewSchemaRef = ASSET_STATE_VIEW_SCHEMA_REF;
  readonly title = ASSET_STATE_VIEW_TITLE;

  render(milestone: DomainViewMilestone): RenderedField[] {
    const fields: RenderedField[] = [
      { kind: "observed_at", label: "观测时间", value: milestone.observedAt },
    ];
    if (milestone.itemCount !== null) {
      fields.push({ kind: "item_count", label: "记录数", value: String(milestone.itemCount) });
    }
    fields.push({ kind: "completeness", label: "完整性", value: "complete" });
    fields.push({
      kind: "provenance",
      label: "数据来源",
      value: `${milestone.sourceRef} @ ${milestone.resultHash.slice(0, 12)}`,
    });
    return fields;
  }
}

export function assetRiskRendererRegistry(): RendererRegistry {
  return new RendererRegistry([new AssetStateViewRenderer()]);
}
