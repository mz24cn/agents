import { describe, it, expect } from "vitest";
import {
  groupKeyOf,
  sortGroupKeys,
  groupTools,
  groupBadge,
  groupIcon,
  groupName,
} from "./tool-groups.js";

const LABELS = {
  client_side: "客户端工具",
  function: "函数工具",
  skill: "技能工具",
};

function tool(toolId, tool_type, mcp_server_name) {
  return { tool_id: toolId, name: toolId, tool_type, mcp_server_name };
}

describe("groupKeyOf", () => {
  it("buckets each tool type into its group", () => {
    expect(groupKeyOf(tool("a", "function"))).toBe("function");
    expect(groupKeyOf(tool("b", "skill"))).toBe("skill");
    expect(groupKeyOf(tool("c", "client_side"))).toBe("client_side");
  });

  it("groups MCP tools by their server, so each server reads as one entry", () => {
    expect(groupKeyOf(tool("a", "mcp", "email"))).toBe("mcp:email");
    // An MCP tool with no server name is still an MCP group, not a mystery one.
    expect(groupKeyOf(tool("a", "mcp"))).toBe("mcp");
  });

  it("keeps an unknown type in a bucket of its own instead of hiding it", () => {
    expect(groupKeyOf(tool("a", "brand_new_type"))).toBe("brand_new_type");
    expect(groupKeyOf({ tool_id: "a" })).toBe("other");
  });
});

describe("sortGroupKeys", () => {
  it("puts client tools first and MCP servers last, in a stable order", () => {
    const sorted = sortGroupKeys([
      "mcp:zeta",
      "skill",
      "mcp:alpha",
      "function",
      "client_side",
    ]);
    expect(sorted).toEqual([
      "client_side",
      "function",
      "skill",
      "mcp:alpha",
      "mcp:zeta",
    ]);
  });

  it("sorts the MCP servers by name so the list stops reshuffling", () => {
    expect(sortGroupKeys(["mcp:rea", "mcp:email", "mcp:android-use"])).toEqual([
      "mcp:android-use",
      "mcp:email",
      "mcp:rea",
    ]);
  });
});

describe("groupTools", () => {
  it("groups a flat tool list in display order", () => {
    const groups = groupTools(
      [
        tool("mcp:rea:search", "mcp", "rea"),
        tool("fn:exec_shell", "function"),
        tool("cs:AskUser", "client_side"),
        tool("sk:docx", "skill"),
        tool("mcp:email:send", "mcp", "email"),
      ],
      LABELS
    );
    expect([...groups.keys()]).toEqual([
      "client_side",
      "function",
      "skill",
      "mcp:email",
      "mcp:rea",
    ]);
    expect(groups.get("client_side").map((x) => x.tool_id)).toEqual([
      "cs:AskUser",
    ]);
  });
});

describe("groupBadge", () => {
  it("badges every known type, so a group never reads as a bare name", () => {
    expect(groupBadge("client_side")).toEqual({
      text: "CLIENT",
      className: "badge-client",
    });
    expect(groupBadge("function")).toEqual({
      text: "FUNCTION",
      className: "badge-function",
    });
    expect(groupBadge("skill")).toEqual({
      text: "SKILL",
      className: "badge-skill",
    });
    expect(groupBadge("mcp:email")).toEqual({
      text: "MCP",
      className: "badge-mcp",
    });
  });

  it("invents nothing for an unknown type", () => {
    expect(groupBadge("brand_new_type")).toBeNull();
  });
});

describe("groupIcon / groupName", () => {
  it("gives the compact picker a distinct marker per type", () => {
    const icons = ["client_side", "function", "skill", "mcp:email"].map(
      groupIcon
    );
    expect(new Set(icons).size).toBe(4);
    // The old picker drew client tools with the fallback dot, which read as
    // "something unsorted" rather than as a tool type.
    expect(groupIcon("client_side")).not.toBe("•");
    expect(groupIcon("brand_new_type")).toBe("•");
  });

  it("names MCP groups after their server and the rest after their type", () => {
    expect(groupName("mcp:firecrawl-mcp", LABELS)).toBe("firecrawl-mcp");
    expect(groupName("client_side", LABELS)).toBe("客户端工具");
    expect(groupName("function", LABELS)).toBe("函数工具");
    expect(groupName("skill", LABELS)).toBe("技能工具");
    // An unlabelled type falls back to its key: still better than vanishing.
    expect(groupName("brand_new_type", LABELS)).toBe("brand_new_type");
  });
});
