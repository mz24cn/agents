/**
 * 标签"发起新会话"要送出去的两样东西。
 *
 * 1) 首轮系统消息带 ``label_origin`` 标记：后端据此知道这条系统消息是标签聚合
 *    上下文（概要+记忆），必须与所选 AI 代理自己的系统提示词合并进同一条
 *    ``system``，而不是被覆盖掉。没有这个标记的系统消息（用户自己"作为系统提示
 *    词"贴进来的）仍按原语义被代理提示词替换。
 * 2) 创建会话的那次请求带 ``source_labels``：来源标签跟着会话建档落到
 *    index.json / tree.json 的标签根下，而不只是在前端草稿上显示一次。
 *
 * 与后端 ``runtime/system_context.py``（读标记、合并）和
 * ``SessionManager.on_session_created``（写 labels）成对。
 */

/** 会话还没建立（草稿态）即为"创建会话的那一次请求"。 */
export function isNewSessionRequest(sessionId) {
  return sessionId === null || sessionId === undefined || sessionId === 'new' || sessionId === '__new__'
}

/**
 * 组装首轮请求里的系统消息（最多一条）。
 *
 * @param {{text?: string, template?: {template_id: string, arguments?: object}|null, origin?: string}} prompt
 * @returns {Array<object>} 可直接 push 进 ``messages`` 的数组
 */
export function buildFirstTurnSystemMessages(prompt = {}) {
  const origin = String(prompt.origin || '').trim()
  const marker = origin ? { label_origin: origin } : {}
  const template = prompt.template
  if (template && template.template_id) {
    const message = {
      role: 'system',
      content: '',
      prompt_template: template.template_id,
      ...marker,
    }
    // arguments 缺省时不发该字段：后端把 undefined 与 {} 都当作"无参数"，
    // 但少一个字段少一处协议面差异（旧代码就是不发的）。
    if (template.arguments !== undefined && template.arguments !== null) {
      message.arguments = template.arguments
    }
    return [message]
  }
  const text = String(prompt.text || '')
  if (text) return [{ role: 'system', content: text, ...marker }]
  return []
}

/**
 * 创建会话请求里的 ``source_labels``；非创建请求或无来源标签时返回 ``null``
 * （调用方据此决定完全不写这个字段，而不是送一个空数组）。
 */
export function sourceLabelsForRequest({ sessionId, origin }) {
  if (!isNewSessionRequest(sessionId)) return null
  const label = String(origin || '').trim()
  return label ? [label] : null
}
