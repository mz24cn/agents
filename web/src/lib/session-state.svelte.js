// session-state.svelte.js — 全局会话恢复状态（Svelte 5 module-level $state）
// Sidebar 设置 pending，ChatPage 监听并消费

export const sessionRestore = $state({ pending: null })
// pending: { sessionId: string, messages: Array<{role, content}>, meta?: object } | null

// 会话文件下载进度。500ms 内完成时不显示；进度值只使用实际已下载字节和 Content-Length。
export const sessionDownload = $state({ loading: false, visible: false, received: 0, total: 0, token: 0 })

// 用户消息定位请求：Sidebar 选择历史用户输入后，由 MessageList 在目标会话渲染完成后消费。
export const messageScrollRequest = $state({ sessionId: null, messageIndex: null, token: 0 })

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

// 终端打开请求：Sidebar 菜单触发，ChatPage 监听并显示终端
export const terminalOpen = $state({ sessionId: null, token: 0 })
// sessionId: string | null — 要打开终端的会话 ID
// token: number — 每次点击递增，确保重复打开也能响应

// 会话日志目录打开请求：Sidebar 菜单触发，ChatPage 监听并打开文件管理器导航到该目录。
// 会话目录始终在本地（父端）：推理在父端发生，conversation.json 所在目录必然存在，
// 即使会话绑定远程环境（file journal 在子端）也打开本地会话目录。
export const openSessionLogDir = $state({ path: null, remoteJournal: null, token: 0 })
// path: string | null — 本地会话日志目录（conversation.json 所在目录）的绝对路径
// remoteJournal: { env_id: string, path: string } | null — 会话绑定远程环境且
//   子端已有 file journal 时，父端 log-dir 响应附带的"软链接"信息（子端会话目录）
// token: number — 每次点击递增，确保重复打开也能响应
