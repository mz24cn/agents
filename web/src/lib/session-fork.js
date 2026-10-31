/**
 * 衍生新会话（fork）的前端判定规则。
 *
 * 与后端 runtime/context_manager.py 的 _resolve_fork_cut 保持一致：
 * 只有 role=assistant 且没有 tool_calls 的消息才是「一轮推理循环的收尾回复」，
 * 把它及之前的所有轮次复制成新会话，上下文才是自洽的——截在一条带工具调用的
 * 回复上，会留下没有对应 tool 结果的 tool_calls，模型侧会拒收这段历史。
 *
 * 流式中的消息（streaming=true）同样不可 fork：内容还在增长，后端 conversation.json
 * 里也还没有它（本轮尚未落盘），截点会落到错误的轮次上。ChatPage 另外在会话推理
 * 期间整体禁用 fork 按钮，避免读到正在被写入的 conversation.json。
 */
export function canForkMessage(message) {
  if (!message || typeof message !== 'object') return false
  if (message.role !== 'assistant') return false
  if (message.streaming === true) return false
  const toolCalls = message.tool_calls
  if (Array.isArray(toolCalls) && toolCalls.length > 0) return false
  return true
}

/**
 * 紧凑模式下一个 agent block 里最后一条可 fork 的消息下标。
 *
 * 紧凑模式把整轮的 assistant/tool 消息折进一张卡片，卡片头部只有一排按钮；
 * fork 按钮挂在卡片头部，语义上取该块最后一条收尾回复。找不到时返回 -1。
 */
export function lastForkableIndex(messages, indices = []) {
  for (let i = indices.length - 1; i >= 0; i--) {
    const index = indices[i]
    if (canForkMessage(messages?.[index])) return index
  }
  return -1
}

/**
 * 衍生响应里的 context_guard → 需要展示的告警文案（无需提示时返回空串）。
 *
 * 摘要被丢弃时，复制进来的历史自己就可能超出上下文预算；后端会先就地为新会话
 * 重建 summary.md，只有重建没做成、或重建后仍超预算时 warning 才非空。fork 本身
 * 已经建好，所以前端不做阻断，只说明首次推理可能被模型拒绝。
 */
export function forkContextWarning(guard, translate) {
  if (!guard || typeof guard !== 'object' || !guard.warning) return ''
  return translate('forkContextWarning', {
    estimated: guard.estimated_tokens ?? '?',
    budget: guard.budget ?? '?',
  })
}
