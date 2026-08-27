// Golden contract tests ported from tests/test_ws6_domain_view_renderer.py.
// The Python reference owns the semantics; this port must never diverge.

import { describe, expect, it } from "vitest";
import { DOMAIN_VIEW_SCHEMA, reduceRunView } from "../src/model/runInteractionModel";
import {
  RendererRegistry,
  type DomainViewRenderer,
  type DomainViewRenderResult,
  type RenderedField,
} from "../src/model/domainViewRenderer";
import {
  ASSET_STATE_VIEW_SCHEMA_REF,
  assetRiskRendererRegistry,
} from "../src/model/assetRiskRenderers";
import type { DomainViewMilestone, UIProjectionEvent } from "../src/model/types";

const RUN_ID = "00000000-0000-0000-0000-000000000001";
const RUN_ID_B = "00000000-0000-0000-0000-000000000002";
const TOOL_REQUEST_ID = "00000000-0000-0000-0000-0000000000aa";
const TOOL_REQUEST_ID_B = "00000000-0000-0000-0000-0000000000bb";
const RESULT_HASH = "a".repeat(64);
const RESULT_HASH_B = "b".repeat(64);
const OBSERVED_AT = "2026-08-21T09:30:00+00:00";

function domainViewEvent(
  seq: number,
  overrides: Partial<{
    toolRequestId: string;
    resultHash: string;
    viewSchemaRef: string;
    itemCount: number | null;
    tenantId: string;
    targetRef: string;
  }> = {},
): UIProjectionEvent {
  return {
    eventId: `event-${seq}-${Math.random().toString(36).slice(2)}`,
    tenantId: overrides.tenantId ?? "tenant-a",
    targetRef: overrides.targetRef ?? RUN_ID,
    projectionSeq: seq,
    payloadSchemaRef: DOMAIN_VIEW_SCHEMA,
    payload: {
      kind: "domain_view_accepted",
      run_id: overrides.targetRef ?? RUN_ID,
      tool_request_id: overrides.toolRequestId ?? TOOL_REQUEST_ID,
      view_schema_ref: overrides.viewSchemaRef ?? ASSET_STATE_VIEW_SCHEMA_REF,
      observed_at: OBSERVED_AT,
      source_ref: "asset-state:postgres:rev-42",
      result_hash: overrides.resultHash ?? RESULT_HASH,
      item_count: overrides.itemCount ?? 3,
    },
  };
}

function milestone(overrides: Partial<DomainViewMilestone> = {}): DomainViewMilestone {
  return {
    toolRequestId: TOOL_REQUEST_ID,
    viewSchemaRef: ASSET_STATE_VIEW_SCHEMA_REF,
    observedAt: OBSERVED_AT,
    sourceRef: "asset-state:postgres:rev-42",
    resultHash: RESULT_HASH,
    itemCount: 3,
    ...overrides,
  };
}

describe("domain-view reducer", () => {
  it("accumulates the milestone in the view state", () => {
    const state = reduceRunView([domainViewEvent(1)]);
    expect(state.domainViews).toEqual([milestone()]);
    expect(state.appliedEventCount).toBe(1);
  });

  it("dedupes on tool_request_id + result_hash", () => {
    const state = reduceRunView([
      domainViewEvent(1),
      domainViewEvent(2),
      domainViewEvent(3, { resultHash: RESULT_HASH_B }),
      domainViewEvent(4, { toolRequestId: TOOL_REQUEST_ID_B }),
    ]);
    expect(state.domainViews).toHaveLength(3);
    expect(state.appliedEventCount).toBe(4);
  });

  it("clears milestones on a tenant switch", () => {
    const state = reduceRunView([
      domainViewEvent(1),
      domainViewEvent(2, { tenantId: "tenant-b", targetRef: RUN_ID_B }),
    ]);
    expect(state.tenantId).toBe("tenant-b");
    expect(state.domainViews).toEqual([
      milestone({ toolRequestId: TOOL_REQUEST_ID }),
    ]);
  });

  it("marks a non-terminal run partial and a terminal run complete", () => {
    const status = (seq: number, status: string): UIProjectionEvent => ({
      eventId: `status-${seq}-${Math.random().toString(36).slice(2)}`,
      tenantId: "tenant-a",
      targetRef: RUN_ID,
      projectionSeq: seq,
      payloadSchemaRef: "grove.ui.run-status-changed.v1",
      payload: { kind: "run_status_changed", run_id: RUN_ID, status, run_revision: seq },
    });
    expect(reduceRunView([status(1, "running")]).completeness).toBe("partial");
    expect(reduceRunView([status(1, "running"), status(2, "succeeded")]).completeness).toBe("complete");
  });

  it("marks the view partial across a tenant switch", () => {
    const state = reduceRunView([
      domainViewEvent(1),
      domainViewEvent(2, { tenantId: "tenant-b", targetRef: RUN_ID_B }),
    ]);
    expect(state.completeness).toBe("partial");
  });
});

describe("renderer registry", () => {
  it("renders an unknown ref as partial with no payload echo", () => {
    const rendered: DomainViewRenderResult = assetRiskRendererRegistry().render(
      milestone({ viewSchemaRef: "OtherDomainView@9" }),
    );
    expect(rendered).toEqual({ kind: "partial", viewSchemaRef: "OtherDomainView@9" });
  });

  it("rejects duplicate registration", () => {
    expect(() => new RendererRegistry([rogueRenderer()])).not.toThrow();
    expect(() => new RendererRegistry([rogueRenderer(), rogueRenderer()])).toThrow(
      "duplicate renderer",
    );
  });

  it("rejects a malformed milestone", () => {
    const registry = assetRiskRendererRegistry();
    expect(() => registry.render({ ...milestone(), resultHash: "short" })).toThrow(
      "exact DomainViewMilestone",
    );
  });

  it("enforces the bounded field count", () => {
    const registry = new RendererRegistry([rogueRenderer()]);
    expect(() => registry.render(milestone({ viewSchemaRef: "RogueView@1" }))).toThrow(
      "bounded field count",
    );
  });
});

function rogueRenderer(): DomainViewRenderer {
  return {
    viewSchemaRef: "RogueView@1",
    title: "rogue",
    render: (): RenderedField[] =>
      Array.from({ length: 9 }, (_, index) => ({
        kind: "provenance" as const,
        label: `row-${index}`,
        value: "xxxxxxxx",
      })),
  };
}

describe("asset risk profile renderer", () => {
  it("renders the golden milestone with item count", () => {
    const rendered = assetRiskRendererRegistry().render(milestone());
    expect(rendered).toEqual({
      kind: "rendered",
      viewSchemaRef: ASSET_STATE_VIEW_SCHEMA_REF,
      title: "资产状态已固定",
      shortResultHash: RESULT_HASH.slice(0, 12),
      fields: [
        { kind: "observed_at", label: "观测时间", value: OBSERVED_AT },
        { kind: "item_count", label: "记录数", value: "3" },
        { kind: "completeness", label: "完整性", value: "complete" },
        {
          kind: "provenance",
          label: "数据来源",
          value: `asset-state:postgres:rev-42 @ ${RESULT_HASH.slice(0, 12)}`,
        },
      ],
    });
  });

  it("omits the count row when item_count is null", () => {
    const rendered = assetRiskRendererRegistry().render(milestone({ itemCount: null }));
    expect(rendered.kind).toBe("rendered");
    if (rendered.kind === "rendered") {
      expect(rendered.fields.map((field) => field.kind)).toEqual([
        "observed_at",
        "completeness",
        "provenance",
      ]);
    }
  });
});
