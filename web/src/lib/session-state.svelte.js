// session-state.svelte.js — 全局会话恢复状态（Svelte 5 module-level $state）
// Sidebar 设置 pending，ChatPage 监听并消费

export const sessionRestore = $state({ pending: null })
// pending: { sessionId: string, messages: Array<{role, content}> } | null
