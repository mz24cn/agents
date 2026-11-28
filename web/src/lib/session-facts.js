/**
 * 会话权威事实（session facts）—— 侧边栏所有分类列表共用的唯一真相。
 *
 * 为什么需要这一层：同一个 session 会同时出现在「主题分类 / 工作区目录 / 标签 /
 * 历史月份」等多个列表里，每个列表各自分页、各自刷新；而 SSE 的状态事件与 init
 * 快照根本不区分分类。把标题、旗标、标签、推理状态只写回"当前这一行的副本"，同一
 * 会话在不同分类里的副本就会各自漂移：
 *
 *   * 在主题分类下改完名，切到标签分类看到的还是旧名；
 *   * 在 A 分类里改了旗标，B 分类的条目要重新拉一次才对得上；
 *   * A 分类里晚到的列表响应会把 B 分类刚收到的 streaming / client_tool_calls
 *     盖回上一轮的 done_success_unread。
 *
 * 服务端的权威同样只有一份（index.json 的元数据 + 内存里的 _session_statuses），
 * 所以前端也只维护一份按 session_id 索引的事实，列表行只是它在"某分类某页"下的
 * 投影：切分类、翻页、SSE、刷新快照读写的是同一份数据。
 *
 * 本模块刻意不依赖 Svelte：合并与投影都是纯函数，可以直接单测；Sidebar 只负责把
 * 结果放进一个响应式 Map 里。
 */

/**
 * 客户端工具等待态。与后端 runtime/server_state.SESSION_STATUS_CLIENT_TOOL_CALLS
 * 必须是同一个拼写：模型请求了只能在浏览器侧执行的工具，服务端把这一轮交出去等回传，
 * 此时会话既不是"推理中"也绝不是"已成功结束"。侧边栏把它渲染成蓝色，并且不许被任何
 * done_success（绿色）覆盖 —— 绿色会让用户以为已经跑完、离开页面，等待就被无声忽略了。
 */
export const CLIENT_TOOL_STATUS = 'client_tool_calls'

/** 结束且未读（绿色 / 红色）：会带未读徽标。 */
const DONE_UNREAD_STATUSES = new Set(['done_success_unread', 'done_error_unread'])

/** 需要用户注意的状态：后端会把它们记成未读，SSE 帧上也带权威标题。 */
const ATTENTION_STATUSES = new Set([...DONE_UNREAD_STATUSES, CLIENT_TOOL_STATUS])

/** 还在跑、不接受任何"已结束"覆盖的状态。 */
const IN_FLIGHT_STATUSES = new Set(['streaming', CLIENT_TOOL_STATUS])

export function isDoneUnread(status) {
  return DONE_UNREAD_STATUSES.has(status)
}

/** 该状态是否代表"这一代推理已经结束"（done_*），用于终态标题回填。 */
export function isTerminalStatus(status) {
  return DONE_UNREAD_STATUSES.has(status)
}

/** 该状态是否带权威标题（后端只在推理落盘完成后才广播这些状态）。 */
export function carriesAuthoritativeTitle(status) {
  return ATTENTION_STATUSES.has(status)
}

export function emptyFacts(sid) {
  return {
    session_id: sid,
    title: null,
    titleGiven: null,     // true = 用户命名；false = 自动生成；null = 不知道
    flag: '',
    labels: [],
    status: null,
    source: null,         // 'sse' | 'snapshot' | 'user'
    updatedAt: 0,         // 标题 / 旗标 / 标签的可采信时刻
    statusAt: 0,          // 推理状态的可采信时刻
  }
}

/**
 * 列表 / 快照响应行 → 事实补丁。
 *
 * 状态取 ``_status``（后端把内存里的实时状态覆盖到分页行上），没有 ``_status`` 时
 * 退回持久化的 ``paused`` 标记：服务重启后内存态是空的，暂停中的会话若不接管就和
 * "结束且已读"毫无区别，极易被忽略。
 */
export function factsFromRow(row) {
  if (!row || !row.session_id) return null
  const status = typeof row._status === 'string' && row._status
    ? row._status
    : (row.status === 'paused' ? 'paused' : null)
  return {
    title: row.title,
    titleGiven: typeof row.title_given === 'boolean' ? row.title_given : null,
    flag: typeof row.flag === 'string' ? row.flag : '',
    labels: Array.isArray(row.labels) ? row.labels : [],
    status,
  }
}

/**
 * 把一条补丁并进已有事实，返回新的合并结果（无变化返回 null）。
 *
 * @param existing 现有事实，可为 undefined
 * @param patch    只认 title / titleGiven / flag / labels / status；其余忽略
 * @param opts     ``at`` 与 ``kind`` 决定这条事实能不能盖掉已有值：
 *                 - ``at``：这条事实的"可采信时刻"。SSE 用收到时刻，用户写入用服务端
 *                   确认时刻，而列表 / 快照响应必须用**请求发起时刻** —— 一份响应不可能
 *                   比它自己的请求更早，于是"晚到的旧响应"天然输给请求期间到达的事件。
 *                 - ``kind``：'sse' | 'snapshot' | 'user'。
 */
export function mergeFacts(existing, patch, opts = {}) {
  if (!patch) return existing || null
  const sid = opts.session_id || patch.session_id || existing?.session_id
  if (!sid) return existing || null
  const kind = opts.kind || 'user'
  const at = Number(opts.at) || 0

  const next = existing ? { ...existing } : emptyFacts(sid)
  next.session_id = sid
  let changed = false

  // ---- 标题 / 旗标 / 标签：按 updatedAt 比较 ----
  const staleMetadata = existing && at > 0 && at < (Number(existing.updatedAt) || 0)
  if (!staleMetadata) {
    const title = typeof patch.title === 'string' && patch.title.trim() ? patch.title.trim() : null
    if (title && title !== next.title) {
      const incomingGiven = typeof patch.titleGiven === 'boolean' ? patch.titleGiven : null
      // 用户手填的标题是人的决定：只有另一次用户写入能替换它。自动生成的标题则可以
      // 被任何更权威的来源更新（后端生成完会广播 title_update / 终态事件）。
      const canReplace = !next.title || next.titleGiven !== true || kind === 'user'
      if (canReplace) {
        next.title = title
        next.titleGiven = incomingGiven === null ? next.titleGiven : incomingGiven
        next.updatedAt = at
        changed = true
      }
    }
    if (typeof patch.flag === 'string' && patch.flag !== next.flag) {
      next.flag = patch.flag
      next.updatedAt = at
      changed = true
    }
    if (Array.isArray(patch.labels)) {
      const labels = patch.labels.map(l => String(l || '').trim()).filter(Boolean)
      if (labels.join('\u0000') !== (next.labels || []).join('\u0000')) {
        next.labels = labels
        next.updatedAt = at
        changed = true
      }
    }
  }

  // ---- 推理状态：单独按 statusAt + 来源比较 ----
  // 状态是瞬时事实，和元数据不同源，所以时间戳也独立；'idle'（已读）由 mark-read 明确
  // 发出，同样走这套规则，不需要特殊通道。
  const status = typeof patch.status === 'string' && patch.status ? patch.status : null
  if (status && status !== next.status) {
    const statusAt = Number(patch.statusAt) || at || 0
    const hadStatusAt = Number(existing?.statusAt) || 0
    const hadSource = existing?.source || null
    let accept = false
    if (!existing?.status) {
      accept = true
    } else if (kind === 'sse' && hadSource === 'snapshot') {
      // 实时事件赢过快照：快照在请求那一刻取数，事件是之后才发生的。
      accept = true
    } else if (kind === 'snapshot') {
      // 快照不得倒退实时状态：尤其不能把 streaming / client_tool_calls 盖成
      // done_success_unread —— 分页响应里的状态只是取数那一刻的值，可能已经过期。
      accept = statusAt > hadStatusAt && !IN_FLIGHT_STATUSES.has(existing.status)
    } else {
      // 同为实时来源（SSE / 本地操作）：按时间先后，晚到的旧事件输。
      accept = statusAt >= hadStatusAt
    }
    // 客户端工具等待意味着「这一轮还没结束」：绿色成功与「无状态」都不许把它冲掉。
    // 后端 transition_session_stream_status 有同样的保护，这里是第二道闸，防的是乱序
    // 或重连补发的旧帧把蓝色洗成绿色（用户据此判断「已经跑完」就会走开，等待被无声忽略）。
    // 能离开蓝色的只有：新一轮 streaming、错误终态 done_error_unread、或同类快照。
    if (accept && next.status === CLIENT_TOOL_STATUS
        && (status === 'done_success_unread' || status === 'idle' || status === 'paused')) {
      accept = false
    }
    if (accept) {
      next.status = status
      next.source = kind
      next.statusAt = statusAt
      changed = true
    }
  }

  return changed ? next : null
}

/**
 * 列表行 ← 权威事实。事实永远赢过行上的旧副本：这样切换分类、翻页、刷新分页时，
 * 同一会话在任何分类下看到的都是同一份标题 / 旗标 / 标签 / 状态。
 */
export function projectRow(row, facts) {
  if (!row) return row
  if (!facts || facts.session_id !== row.session_id) return row
  const out = { ...row }
  if (facts.title) {
    out.title = facts.title
    out.title_given = facts.titleGiven === true
  }
  if (typeof facts.flag === 'string') out.flag = facts.flag
  if (Array.isArray(facts.labels)) out.labels = facts.labels
  if (facts.status) out._status = facts.status
  return out
}

export function projectRows(rows, factsById) {
  return (rows || []).map(row => projectRow(row, factsById?.get(row?.session_id)))
}

/**
 * 标签改名后同步一份标签列表：旧名换成新名，若新名已存在则去重（后端同名合并语义）。
 * 保持原有顺序，便于列表不跳动。
 */
export function applyLabelRename(labels, from, to) {
  const out = []
  for (const raw of labels || []) {
    const label = String(raw || '').trim()
    if (!label) continue
    const value = label === from ? to : label
    if (!out.includes(value)) out.push(value)
  }
  return out
}
