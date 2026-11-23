/**
 * 「这条会话有终端窗口」—— ChatPage 顶栏那个 💻 标记、以及窗口本身要不要打开的
 * 唯一判据。
 *
 * 会话的终端实例存在 ChatPage 的 ``terminals`` 映射里，那是**一次页面加载**的内存
 * 状态：刷新页面就清空。于是开过终端的会话在顶栏上不再有任何痕迹，窗口也没了，用
 * 户只能回侧栏的终端树里再双击一次。会话 meta 里的 ``terminal: "enabled"`` 就是
 * 为这件事准备的持久化判据（母端 runtime/server_state.py 的 mark_session_terminal
 * 在**终端真的被打开那一刻**写一笔，跟 remote_env / workspace 一样从
 * ``GET /v1/sessions``、``GET /v1/sessions/{id}``、``GET /v1/sessions/{id}/meta``
 * 原样返回；双击销毁走 ``DELETE /v1/terminals/{id}`` 把它抹掉）。
 *
 * 为什么不是「tool_ids 里有 exec_cli」：那只能说这一轮允许用终端。模型一次都没调
 * 它，终端就根本没开过，刷新后凭空给这条会话起一个 shell 是替用户做他没做过的决
 * 定。标记记的是「窗口打开过」这个状态本身，销毁过就消失。
 *
 * 为什么不是「终端此刻活着」：母端的终端注册表是内存态，会被
 * ``TERMINAL_IDLE_TIMEOUT`` / 重启收掉，拿它当判据的话标记会闪一下又消失。
 */

/** meta 里那个会话级标记的键与值（后端 server_state.TERMINAL_META_*）。 */
export const TERMINAL_META_KEY = 'terminal'
export const TERMINAL_META_ENABLED = 'enabled'

/**
 * @param {object|null|undefined} meta 会话 meta（conversation.json 的 meta 块）
 * @returns {boolean} 这条会话要不要把终端窗口（连同顶栏标记）恢复出来
 */
export function sessionHasTerminal(meta) {
  if (!meta || typeof meta !== 'object') return false
  // 缺字段就是它的否定形式：后端只在 True 时写，销毁时又把字段删掉，
  // 早于这次改动存下的会话同样读成「没有终端」。
  return meta[TERMINAL_META_KEY] === TERMINAL_META_ENABLED
}

/**
 * 一条「还没连上」的终端条目：顶栏标记的来源。
 *
 * ChatPage 用它把刷新前的窗口补回来。条目一进去 Terminal.svelte 就挂载并 attach
 * （通道开着、PTY 在后端跑着），面板仍然收起 —— 也就是「窗口是开着的，只是没显
 * 示」。PTY 若已被 ``TERMINAL_IDLE_TIMEOUT`` 回收，attach 时原地新建一个。
 */
export function unconnectedTerminal(sessionId) {
  return {
    ref: null,
    chain: [],
    status: { connected: false, error: null, loading: false, terminalId: sessionId },
  }
}
