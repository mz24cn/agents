// 嵌套终端链注入：双击终端层节点后，在同一个 PTY 里按顺序执行各层的进入
// 指令（ssh user@host → wsl → docker exec ... → psql ...），直到目标环境。
//
// v2 设计（marker 探针，不再单纯依赖提示符正则猜测）：
// - 发送第 i 层（非最后一层）后进入 waiting：监视输出。
//   * 命中提示符启发式 → 不直接发下一层，而是发一行探针 `echo __TCH_xxx__`
//     进入 probing；在输出中看到 marker（回显或执行结果）才证明到达了开启
//     echo 的活 shell，随即发送下一层。误判的提示符（如密码等待）不会回显
//     marker，探针超时而 paused，不会把下一层错发进密码框。
//   * 命中 password/yes-no 等交互提示 → await-input：完全停手，用户在终端
//     里输入密码/确认；之后提示符出现 → 探针确认 → 继续。
//   * readyTimeout 内既无提示符也无 marker → paused，由用户点"继续"
//     （继续 = 重新探针，而不是盲目发下一条）。
// - 最后一层发出后直接 done（psql 密码之类由用户在终端里自行完成）。
// - 链状态变化通过 onState 通知 UI 状态条。

const ANSI = /\x1b\[[0-9;?]*[0-9;]*[a-zA-Z@`~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[=>()]|\x1b[0-9;]*[0-9]*[a-zA-Z]/g

export function stripAnsi(text) {
  return String(text || '').replace(ANSI, '')
}

// 常见交互壳的提示符结尾：bash/zsh '$'/'#'、cmd 'C:\>'、powershell 'PS ...>'、
// psql 'db=#'、mysql 'mysql>'、redis 'host:port>'、fish '>'。取输出尾部匹配。
export function looksLikePrompt(tail) {
  const t = String(tail || '').trimEnd()
  if (!t) return false
  return /[\$#>%]\s*$/.test(t) || /PS [^\n]*>\s*$/.test(t)
}

// 需要用户手工应答的提示（密码、口令、host key 确认等）。这些提示不要求
// 行尾，出现在输出尾部附近即可。
export function looksLikeInteractivePrompt(tail) {
  return /(password|passphrase|pass\s?code|verification code|密码|口令|are you sure|yes\/no|\(y\/n\)|authenticity|token for)/i
    .test(String(tail || '').slice(-200))
}

let markerSeq = 0

export function createChainRunner(commands, options = {}) {
  const {
    send = () => {},
    onState = () => {},
    delayFirst = 700,
    promptWait = 6000,
    probeWait = 8000,
  } = options
  const list = (commands || []).filter((c) => typeof c === 'string' && c.trim())
  let index = -1
  let state = list.length ? 'idle' : 'off'
  let buffer = ''
  let timer = null
  let marker = null

  const emit = () => onState({ state, index, total: list.length })
  const clearTimer = () => { if (timer) { clearTimeout(timer); timer = null } }
  function onTimeout() {
    // waiting 超时（提示符启发式没命中，如 WSL 花哨 PS1）：自动探针确认一次；
    // probing 也超时才真正 paused。
    if (state === 'waiting') { probe(); return }
    if (state === 'probing') { state = 'paused'; emit() }
  }
  const armTimer = (ms) => {
    clearTimer()
    timer = setTimeout(onTimeout, ms)
  }

  function sendNext() {
    marker = null
    index += 1
    if (index >= list.length) { state = 'done'; clearTimer(); emit(); return }
    state = 'sending'
    try { send(list[index] + '\r') } catch { /* channel may be down */ }
    if (index === list.length - 1) {
      // 最后一层发出即完成：之后的密码/提示由用户在终端里直接处理。
      state = 'done'; clearTimer(); emit(); return
    }
    state = 'waiting'
    buffer = ''
    emit()
    armTimer(promptWait)
  }

  // 探针：向疑似已就绪的 shell 发一行 echo <marker>。看到 marker 回到输出
  // （回显即可，无需等执行结果）才认定 shell 可用。
  function probe() {
    markerSeq = (markerSeq + 1) % 1000
    marker = `__TCH${Date.now().toString(36)}${markerSeq.toString(36)}__`
    try { send(`echo ${marker}\r`) } catch { /* channel may be down */ }
    state = 'probing'
    buffer = ''
    emit()
    armTimer(probeWait)
  }

  function begin() {
    if (state !== 'idle') return
    // 给 PTY 一点启动/首提示符时间，再注入第一条指令。
    setTimeout(() => { if (state === 'idle') sendNext() }, delayFirst)
  }

  // 状态条"继续"：paused / await-input 时重新探针确认，而不是盲目发下一条。
  function resume() {
    if (state === 'paused' || state === 'await-input') probe()
  }

  function onOutput(chunk) {
    if (state !== 'waiting' && state !== 'probing' && state !== 'await-input') return
    buffer = (buffer + stripAnsi(chunk)).slice(-600)
    if (marker && buffer.includes(marker)) {
      // shell 确认就绪：立即发送下一层。
      buffer = ''
      sendNext()
      return
    }
    if (looksLikeInteractivePrompt(buffer)) {
      if (state !== 'await-input') { state = 'await-input'; clearTimer(); emit() }
      buffer = ''
      return
    }
    if ((state === 'waiting' || state === 'await-input') && looksLikePrompt(buffer)) {
      buffer = ''
      probe()
      return
    }
  }

  function cancel() {
    clearTimer()
    if (state !== 'off' && state !== 'done') { state = 'off'; emit() }
  }

  return {
    begin,
    resume,
    onOutput,
    cancel,
    get state() { return state },
    get index() { return index },
    get total() { return list.length },
  }
}

/**
 * 打开会话终端时决定"进入指令链"该怎么生效。
 *
 * 双击终端层往往发生在该会话的终端已经打开之后（先双击环境行开 PTY，再双击
 * 其中的某一层进去）。若链只在创建终端实例时记一次，第二次的双击就静默失效：
 * 用户看到的还是最外层 shell，且没有任何提示。
 *
 * @param existing   该会话已有的终端实例条目（null/undefined = 本次新建）
 * @param commands   本次双击收集的逐层进入指令
 * @param connected  该终端是否已经连上（连着才能就地注入）
 * @returns {{chain: string[]|null, runNow: string[]|null}}
 *   chain  —— 需要写入实例的链（null = 不动已有链）
 *   runNow —— 需要立即运行的链（null = 交给首次连接时注入）
 */
export function planTerminalChain({ existing, commands, connected = false } = {}) {
  const list = (commands || []).filter((c) => typeof c === 'string' && c.trim())
  if (!existing) return { chain: list, runNow: null }
  if (!list.length) return { chain: null, runNow: null }
  // 已连接：立刻逐层注入；未连接：写进实例，连上时由 Terminal 自己注入。
  return { chain: list, runNow: connected ? list : null }
}

/**
 * 在森林 roots 中按会话执行环境定位终端环境节点（供会话"打开终端"面板使用）。
 *
 * 多环境森林：终端根下有"本地"（env_id 为空）与各远程环境（env_id=登记地址）
 * 子节点，返回匹配子节点及 id 路径（`<rootId>/<envId>`，collectTerminalTarget
 * 语义）。单环境森林：只有根"本地终端"且无 env 子节点，本地会话以根为目标。
 * 远程环境未注册/已注销时返回 null（面板显示兜底提示 + 直接打开）。
 */
export function locateTerminalEnvNode(roots, envId) {
  const root = (roots || []).find(n => n && n.type === 'terminal')
  if (!root) return null
  const kids = (root.children || []).filter(c => c && typeof c === 'object')
  const envKids = kids.filter(c => c.type === 'terminal')
  let node = null
  if (envId) {
    node = envKids.find(c => String(c.env_id || '') === envId) || null
  } else {
    // 单环境形态（根即"本地"环境节点，无环境子级）：根就是目标，即使根下已有
    // 用户自建的 terminal_layer，也不能因此判定"该会话环境没有节点"。
    node = envKids.find(c => !(c.env_id || '')) || (envKids.length ? null : root)
  }
  if (!node) return null
  const path = node === root ? String(root.id ?? '') : `${root.id ?? ''}/${node.id ?? ''}`
  return { node, path }
}
