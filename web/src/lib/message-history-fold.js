/**
 * message-history-fold.js — 长会话打开时的「只渲染第一轮 + 最后两轮」窗口
 *
 * 打开一条几十轮的会话，把每一轮的 markdown / mermaid / 工具卡片全部渲染出来是很贵的
 * （MessageList 每轮都要 buildTurnAgentGrouping + MessageBubble + MarkdownRenderer）。
 * 所以进入会话时只渲染：第一轮问答、一条全宽横幅（说明中间被省略了多少轮）、最后两轮问答。
 * 点横幅才把中间轮次的用户消息和回复（紧凑或详细）全部补上。
 *
 * 这里只做纯计算：scanTurns 给出轮次边界，computeHistoryFold 决定哪些轮次不进 DOM。
 * 索引一律保持原始 messages 下标 —— fork / retry / 撤销 / 文件变更锚点全都按原始下标寻址，
 * 所以「隐藏」必须是跳过渲染，绝不能是过滤数组。
 */

/** 用户消息条数超过这个数才折叠（4 条起：中间正好还剩 1 轮可省）。 */
export const HISTORY_FOLD_USER_THRESHOLD = 3
/** 折叠时保留开头几轮问答。 */
export const HISTORY_FOLD_HEAD_TURNS = 1
/** 折叠时保留结尾几轮问答。 */
export const HISTORY_FOLD_TAIL_TURNS = 2

/**
 * 按 user 消息切轮次：每条 user 消息连同其后的 assistant/tool 消息算一轮；非 user 开头
 * 的消息（例如开头的 system）单列为 ungrouped，不参与折叠。与 MessageList 原先的内联扫描
 * 等价：startIndex / endIndex 是 messages 下标，lastAssistantIndex 是该轮最后一条 assistant。
 */
export function scanTurns(messages) {
  const turns = []
  let i = 0
  const list = Array.isArray(messages) ? messages : []
  while (i < list.length) {
    if (list[i]?.role === 'user') {
      const startIndex = i
      let endIndex = i
      let lastAssistantIndex = -1
      for (let j = i + 1; j < list.length; j++) {
        if (list[j].role === 'user') break
        endIndex = j
        if (list[j].role === 'assistant') lastAssistantIndex = j
      }
      turns.push({ startIndex, endIndex, lastAssistantIndex, ungrouped: false })
      i = endIndex + 1
    } else {
      turns.push({ startIndex: i, endIndex: i, lastAssistantIndex: -1, ungrouped: true })
      i++
    }
  }
  return turns
}

/**
 * 「进入会话时」的快照终点：第一次看见非空轮次就把当时的最后一轮 endIndex 定下来，
 * 之后原样返回。会话中途新流进来的轮次因此落在快照外，永远不会被折叠 —— 否则用户发完
 * 第 4 条消息，刚看过的上一轮回答会突然被收到横幅后面。
 */
export function snapshotEndIndex(turns, previous = null) {
  if (previous !== null) return previous
  const list = Array.isArray(turns) ? turns : []
  return list.length > 0 ? list[list.length - 1].endIndex : null
}

function emptyFold() {
  return {
    collapsed: false,
    hiddenTurns: 0,
    hiddenMessages: 0,
    hiddenStarts: new Set(),
    // 横幅挂在哪一轮之后（该轮的 startIndex）；null 表示没有横幅。
    bannerAfterStart: null,
  }
}

/**
 * @param turns scanTurns 的结果（已过滤掉 ungrouped），按顺序，且只包含「进入会话时」
 *        已经存在的那些轮次 —— 会话中途新流进来的轮次不该再被折叠，否则用户刚发完消息，
 *        上一轮回答就凭空消失了。
 * @param options.expanded 用户已经点过横幅：整段历史都渲染。
 * @returns {collapsed, hiddenTurns, hiddenMessages, hiddenStarts, bannerAfterStart}
 *          hiddenStarts 装的是需要跳过渲染的轮的 startIndex。
 */
export function computeHistoryFold(turns, options = {}) {
  const {
    expanded = false,
    threshold = HISTORY_FOLD_USER_THRESHOLD,
    headTurns = HISTORY_FOLD_HEAD_TURNS,
    tailTurns = HISTORY_FOLD_TAIL_TURNS,
  } = options

  if (expanded) return emptyFold()
  const list = Array.isArray(turns) ? turns : []
  // 轮次不够多（用户消息 <= threshold，或刚好等于 head+tail）时没有可省的中间轮次。
  if (list.length <= threshold || list.length <= headTurns + tailTurns) return emptyFold()

  const hidden = list.slice(headTurns, list.length - tailTurns)
  if (hidden.length === 0) return emptyFold()

  const fold = emptyFold()
  fold.collapsed = true
  fold.hiddenTurns = hidden.length
  fold.hiddenMessages = hidden.reduce((sum, turn) => sum + (turn.endIndex - turn.startIndex + 1), 0)
  fold.hiddenStarts = new Set(hidden.map(turn => turn.startIndex))
  fold.bannerAfterStart = list[headTurns - 1].startIndex
  return fold
}

/** 下标落在哪一轮（含该轮的 user/assistant/tool 全区间）；用于「跳到某条消息」时先展开。 */
export function turnContaining(turns, index) {
  return (Array.isArray(turns) ? turns : []).find(turn => index >= turn.startIndex && index <= turn.endIndex) || null
}
