import { describe, expect, it } from "vitest";
import { FlowNode, columnOf, layoutFlow, strokeFor, usd } from "./flow";

const node = (id: string, depth: number, roles: string[], flagged = false): FlowNode => ({
  account_id: id, owner_customer_id: null, depth, roles, flagged, is_hub: false, in_degree: 0, out_degree: 0,
});

describe("money-flow layout", () => {
  it("puts the subject in the middle, upstream to the left and downstream to the right", () => {
    expect(columnOf(node("A", 0, ["subject"]))).toBe(0);
    expect(columnOf(node("B", 1, ["upstream"]))).toBe(-1);
    expect(columnOf(node("C", 2, ["downstream"]))).toBe(2);
    expect(columnOf(node("D", 1, ["upstream", "downstream"]))).toBe(1);
  });
  it("places every node once, with columns left to right and flagged accounts first in a column", () => {
    const { placed, width, height } = layoutFlow([
      node("S", 0, ["subject"]), node("U1", 1, ["upstream"]), node("U2", 1, ["upstream"], true), node("D1", 1, ["downstream"]),
    ]);
    expect(placed).toHaveLength(4);
    const by = Object.fromEntries(placed.map((p) => [p.account_id, p]));
    expect(by.U1.x).toBeLessThan(by.S.x);
    expect(by.S.x).toBeLessThan(by.D1.x);
    expect(by.U2.y).toBeLessThan(by.U1.y);
    expect(width).toBeGreaterThanOrEqual(320);
    expect(height).toBeGreaterThan(0);
  });
  it("handles an empty graph", () => {
    expect(layoutFlow([]).placed).toEqual([]);
  });
  it("scales stroke width with amount within bounds and abbreviates dollars", () => {
    expect(strokeFor(10)).toBeGreaterThanOrEqual(1.5);
    expect(strokeFor(1e9)).toBeLessThanOrEqual(7);
    expect(strokeFor(100000)).toBeGreaterThan(strokeFor(100));
    expect(usd(950)).toBe("$950");
    expect(usd(1500)).toBe("$1.5k");
    expect(usd(25000)).toBe("$25k");
  });
});
