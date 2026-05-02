// session-state.svelte.js — 全局会话恢复状态（Svelte 5 module-level $state）
// Sidebar 设置 pending，ChatPage 监听并消费

export const sessionRestore = $state({ pending: null })
// pending: { sessionId: string, messages: Array<{role, content}> } | null

// 新会话创建通知：ChatPage 通知 Sidebar 动态添加新会话条目
// firstUserMessage: 用户第一条消息文本，用于在标题生成前作为临时标题
export const newSessionCreated = $state({ sessionId: null, firstUserMessage: null })
// sessionId: string | null — 新创建的会话 ID
