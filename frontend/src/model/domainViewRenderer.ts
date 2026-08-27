// Typed domain-view renderer contract — TS port of app/observation/rendering.py.
// Renderers are owned by a Business Profile and selected strictly by
// viewSchemaRef through a closed registry; an unknown ref yields a partial
// marker that carries the schema ref and nothing else.  A generic JSON
// renderer is forbidden (docs/06 §15).

import type { DomainViewMilestone } from "./types";

export const MAX_RENDERED_FIELDS = 8;
export const SHORT_HASH_LENGTH = 12;

export type RenderedFieldKind = "observed_at" | "item_count" | "completeness" | "provenance";

export interface RenderedField {
  kind: RenderedFieldKind;
  label: string;
  value: string;
}

export interface RenderedDomainView {
  kind: "rendered";
  viewSchemaRef: string;
  title: string;
  fields: RenderedField[];
  shortResultHash: string;
}

export interface PartialDomainView {
  kind: "partial";
  viewSchemaRef: string;
}

export type DomainViewRenderResult = RenderedDomainView | PartialDomainView;

export interface DomainViewRenderer {
  readonly viewSchemaRef: string;
  readonly title: string;
  render(milestone: DomainViewMilestone): RenderedField[];
}

function isMilestone(value: DomainViewMilestone): boolean {
  return (
    typeof value.toolRequestId === "string" &&
    value.toolRequestId.length > 0 &&
    typeof value.viewSchemaRef === "string" &&
    value.viewSchemaRef.length > 0 &&
    typeof value.observedAt === "string" &&
    value.observedAt.length > 0 &&
    typeof value.sourceRef === "string" &&
    value.sourceRef.length > 0 &&
    typeof value.resultHash === "string" &&
    value.resultHash.length === 64 &&
    (value.itemCount === null || typeof value.itemCount === "number")
  );
}

export class RendererRegistry {
  private readonly byRef: Map<string, DomainViewRenderer>;

  constructor(renderers: readonly DomainViewRenderer[]) {
    this.byRef = new Map();
    for (const renderer of renderers) {
      if (this.byRef.has(renderer.viewSchemaRef)) {
        throw new Error(`duplicate renderer for view_schema_ref: ${renderer.viewSchemaRef}`);
      }
      this.byRef.set(renderer.viewSchemaRef, renderer);
    }
  }

  get viewSchemaRefs(): ReadonlySet<string> {
    return new Set(this.byRef.keys());
  }

  render(milestone: DomainViewMilestone): DomainViewRenderResult {
    if (!isMilestone(milestone)) {
      throw new Error("render requires an exact DomainViewMilestone");
    }
    const renderer = this.byRef.get(milestone.viewSchemaRef);
    if (renderer === undefined) {
      return { kind: "partial", viewSchemaRef: milestone.viewSchemaRef };
    }
    const fields = renderer.render(milestone);
    if (fields.length < 1 || fields.length > MAX_RENDERED_FIELDS) {
      throw new Error(`renderer for ${milestone.viewSchemaRef} exceeded the bounded field count`);
    }
    return {
      kind: "rendered",
      viewSchemaRef: milestone.viewSchemaRef,
      title: renderer.title,
      fields,
      shortResultHash: milestone.resultHash.slice(0, SHORT_HASH_LENGTH),
    };
  }
}
