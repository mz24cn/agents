// How a flat tool list is grouped for display, shared by every page that shows
// the tool catalogue (the chat's tool picker, the tools page). Grouping was
// duplicated in two components and drifted: one knew about client_side, the
// other did not, and the same type was labelled "函数工具" in one place and
// shown as a raw `client_side` key in another. Keep the rules in one file.

/** Group key of a tool, as used by every tool list in the UI. */
export function groupKeyOf(tool) {
  if (tool.tool_type === "mcp" && tool.mcp_server_name) {
    return `mcp:${tool.mcp_server_name}`;
  }
  if (["function", "mcp", "skill", "client_side"].includes(tool.tool_type)) {
    return tool.tool_type;
  }
  return tool.tool_type || "other";
}

const TYPE_RANK = { client_side: 0, function: 1, skill: 2, mcp: 3 };

/**
 * Stable display order: client tools, functions, skills, then MCP servers by
 * name. Without it the groups appear in whatever order the API happened to
 * return, which reads as random.
 */
export function sortGroupKeys(keys) {
  return [...keys].sort((a, b) => {
    const rankA = TYPE_RANK[groupTypeOf(a)] ?? 9;
    const rankB = TYPE_RANK[groupTypeOf(b)] ?? 9;
    if (rankA !== rankB) return rankA - rankB;
    if (rankA === TYPE_RANK.mcp) return nameOf(a).localeCompare(nameOf(b));
    return 0;
  });
}

function groupTypeOf(key) {
  return key.startsWith("mcp:") ? "mcp" : key;
}

/** Display name of a group: server name for MCP groups, type name otherwise. */
export function groupName(key, labels) {
  if (key.startsWith("mcp:")) return key.slice(4);
  return labels[groupTypeOf(key)] || key;
}

/**
 * The type badge shown before a group name. Every group carries one so the
 * list reads the same everywhere; unknown types get no badge rather than a
 * made-up one.
 */
export function groupBadge(key) {
  const type = groupTypeOf(key);
  if (type === "mcp") return { text: "MCP", className: "badge-mcp" };
  if (type === "client_side") return { text: "CLIENT", className: "badge-client" };
  if (type === "function") return { text: "FUNCTION", className: "badge-function" };
  if (type === "skill") return { text: "SKILL", className: "badge-skill" };
  return null;
}

/** Compact marker for the tight pickers, where there is no room for a badge. */
export function groupIcon(key) {
  const type = groupTypeOf(key);
  if (type === "mcp") return "⚡";
  if (type === "client_side") return "◉";
  if (type === "function") return "𝒻";
  if (type === "skill") return "★";
  return "•";
}

function nameOf(key) {
  return key.startsWith("mcp:") ? key.slice(4) : key;
}

/** Group a tool list into `Map<key, tools[]>`, in display order. */
export function groupTools(tools, labels) {
  const map = new Map();
  for (const tool of tools) {
    const key = groupKeyOf(tool);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(tool);
  }
  const ordered = new Map();
  for (const key of sortGroupKeys([...map.keys()])) ordered.set(key, map.get(key));
  return ordered;
}
