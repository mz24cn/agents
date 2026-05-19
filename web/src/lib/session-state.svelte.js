// session-state.svelte.js — 全局会话恢复状态（Svelte 5 module-level $state）
// Sidebar 设置 pending，ChatPage 监听并消费

export const sessionRestore = $state({ pending: null })
// pending: { sessionId: string, messages: Array<{role, content}>, meta?: object } | null

// 当前活跃的会话 ID（用于侧边栏高亮）
export const currentSession = $state({ sessionId: null })

// 新会话创建通知：ChatPage 通知 Sidebar 动态添加新会话条目
// firstUserMessage: 用户第一条消息文本，用于在标题生成前作为临时标题
export const newSessionCreated = $state({ sessionId: null, firstUserMessage: null, title: null })
// sessionId: string | null — 新创建的会话 ID
// title: string | null — 后端生成的会话标题

// 新建会话请求：Sidebar 触发，ChatPage 消费
export const newSessionRequest = $state({ token: 0 })
// token: number — 每点击一次新建会话递增，确保连续点击也能被响应

// 会话删除通知：Sidebar 通知 ChatPage 同步清空右侧面板
export const sessionDeleted = $state({ sessionId: null })
// sessionId: string | null — 被删除的会话 ID
