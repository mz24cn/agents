/**
 * 嵌套终端链注入状态机测试（v2 marker 探针）：ssh/docker/wsl 链按序执行、
 * 提示符启发式 + echo 探针二次确认、密码等待、未知提示符超时暂停、
 * ANSI 剥离与提示符识别。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  createChainRunner,
  stripAnsi,
  looksLikePrompt,
  looksLikeInteractivePrompt,
  locateTerminalEnvNode,
  planTerminalChain,
} from './terminalChain.js'

// 从发出的探针行里取 marker（`echo __TCH...__\r`）；多个探针时取最后一个。
function markerOf(sent) {
  const lines = sent.filter((s) => /^echo __TCH/.test(s))
  const line = lines[lines.length - 1]
  return line ? line.match(/(__TCH[^_]*__)/)[1] : null
}

describe('stripAnsi', () => {
  it('removes CSI/OSC sequences but keeps text', () => {
    expect(stripAnsi('\x1b[?1000luser@box:~$ \x1b[0m')).toBe('user@box:~$ ')
    expect(stripAnsi('\x1b]0;title\x07PS C:\\>')).toBe('PS C:\\>')
    // ST-terminated OSC（无 BEL）也要剥掉
    expect(stripAnsi('\x1b]633;P;Cwd=/home/x\x1b\\u@h:~$ ')).toBe('u@h:~$ ')
  })
})

describe('looksLikePrompt', () => {
  it('accepts common shell prompts', () => {
    for (const t of ['user@box:~$ ', 'root@box:#', 'C:\\> ', 'PS C:\\Users\\x> ', 'db=#', 'mysql> ', 'r:6379> ']) {
      expect(looksLikePrompt(t)).toBe(true)
    }
  })
  it('rejects non-prompts', () => {
    expect(looksLikePrompt('hello world')).toBe(false)
    expect(looksLikePrompt('')).toBe(false)
    expect(looksLikePrompt('apt install done')).toBe(false)
  })
})

describe('looksLikeInteractivePrompt', () => {
  it('detects password/host-key/yes-no prompts', () => {
    expect(looksLikeInteractivePrompt('user@host\'s password:')).toBe(true)
    expect(looksLikeInteractivePrompt('Are you sure you want to continue connecting (yes/no)?')).toBe(true)
    expect(looksLikeInteractivePrompt('Enter passphrase for key')).toBe(true)
    expect(looksLikeInteractivePrompt('normal output')).toBe(false)
  })
})

describe('createChainRunner', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  function setup(commands, options = {}) {
    const sent = []
    const states = []
    const runner = createChainRunner(commands, {
      send: (text) => sent.push(text),
      onState: (st) => states.push(st.state),
      ...options,
    })
    return { runner, sent, states }
  }

  it('advances only after the probe marker comes back (real WSL→docker flow)', () => {
    const { runner, sent } = setup(['wsl', 'docker exec -it cc bash'])
    runner.begin()
    vi.advanceTimersByTime(700)
    expect(sent).toEqual(['wsl\r'])
    // 提示符启发式命中 → 先发探针，不直接发下一层
    runner.onOutput('wsl\r\nWelcome to Ubuntu 24.04.2 LTS\r\nuser@PC:~$ ')
    expect(sent[1]).toMatch(/^echo __TCH/)
    // 探针被新 shell 回显 → 确认就绪 → 发送下一层
    const marker = markerOf(sent)
    runner.onOutput(`echo ${marker}\r\n${marker}\r\n`)
    expect(sent[2]).toBe('docker exec -it cc bash\r')
    // 最后一层发出即完成
    expect(runner.state).toBe('done')
    expect(sent).toHaveLength(3)
  })

  it('auto-probes when the prompt regex never matches (the real WSL case)', () => {
    const { runner, sent } = setup(['wsl', 'docker exec -it cc bash'],
      { promptWait: 100, probeWait: 100 })
    runner.begin()
    vi.advanceTimersByTime(700)
    // WSL 花哨 PS1 没被启发式识别：waiting 超时后自动发探针
    runner.onOutput('\x1b]633;A\x07Welcome!\r\nuser@PC  \u276f ')
    expect(sent).toHaveLength(1)
    vi.advanceTimersByTime(100)
    expect(sent[1]).toMatch(/^echo __TCH/)
    // 探针回显 → 下一层立即发出，全程无需用户点击
    const marker = markerOf(sent)
    runner.onOutput(`echo ${marker}\r\n${marker}\r\n`)
    expect(sent[2]).toBe('docker exec -it cc bash\r')
    expect(runner.state).toBe('done')
  })

  it('a misdetected prompt never sends the next layer without marker echo', () => {
    const { runner, sent } = setup(['ssh user@host', 'wsl'], { probeWait: 100 })
    runner.begin()
    vi.advanceTimersByTime(700)
    // 某行输出恰好以 % 结尾 → 误判提示符 → 探针发出但没有回显（如密码等待）
    runner.onOutput('loading 90%\n')
    expect(sent[1]).toMatch(/^echo __TCH/)
    vi.advanceTimersByTime(100)
    expect(runner.state).toBe('paused')
    // 下一层从未被发送
    expect(sent.some((s) => s === 'wsl\r')).toBe(false)
  })

  it('waits for the password, then auto-continues via probe once the prompt appears', () => {
    const { runner, sent, states } = setup(['ssh user@host', 'wsl'])
    runner.begin()
    vi.advanceTimersByTime(700)
    runner.onOutput('The authenticity of host ...\nAre you sure you want to continue connecting (yes/no)? ')
    expect(runner.state).toBe('await-input')
    // 用户手动输入不回显；错误密码再次出现 password 提示也保持等待
    runner.onOutput('\r\nPermission denied, please try again.\r\nuser@host\'s password: ')
    expect(sent).toHaveLength(1)
    // 成功登录后出现 shell 提示符 → 探针确认 → 自动进入下一层
    runner.onOutput('\r\nuser@host:~$ ')
    expect(sent[1]).toMatch(/^echo __TCH/)
    const marker = markerOf(sent)
    runner.onOutput(`${marker}\r\n`)
    expect(sent[2]).toBe('wsl\r')
    expect(runner.state).toBe('done')
    expect(states).toContain('await-input')
  })

  it('Continue re-probes instead of blindly sending the next layer', () => {
    const { runner, sent, states } = setup(['weird-shell', 'next'],
      { promptWait: 100, probeWait: 100 })
    runner.begin()
    // waiting 超时自动探针，探针也超时 → paused
    vi.advanceTimersByTime(700 + 100 + 100)
    expect(runner.state).toBe('paused')
    expect(sent[1]).toMatch(/^echo __TCH/)
    runner.resume()
    expect(sent[2]).toMatch(/^echo __TCH/)
    expect(sent.some((s) => s === 'next\r')).toBe(false)
    const marker = markerOf(sent)
    runner.onOutput(`${marker}\r\n`)
    expect(sent[3]).toBe('next\r')
    expect(runner.state).toBe('done')
    expect(states).toContain('paused')
  })

  it('the last layer is always sent even without any prompt', () => {
    const { runner, sent } = setup(['psql -U postgres'])
    runner.begin()
    vi.advanceTimersByTime(700)
    expect(sent).toEqual(['psql -U postgres\r'])
    expect(runner.state).toBe('done')
    // 之后 psql 自己弹密码提示也不干扰
    runner.onOutput('Password for user postgres: ')
    expect(sent).toHaveLength(1)
  })

  it('is inert without commands', () => {
    const { runner, sent } = setup([])
    expect(runner.state).toBe('off')
    runner.begin()
    vi.advanceTimersByTime(1000)
    expect(sent).toEqual([])
  })

  it('cancel stops a pending chain', () => {
    const { runner, sent } = setup(['a', 'b'], { probeWait: 50 })
    runner.begin()
    vi.advanceTimersByTime(700)
    runner.onOutput('$ ')
    expect(sent[1]).toMatch(/^echo __TCH/)
    runner.cancel()
    expect(runner.state).toBe('off')
    runner.onOutput(`${markerOf(sent)}\r\n`)
    expect(sent.some((s) => s === 'b\r')).toBe(false)
  })
})

describe('planTerminalChain（双击终端层时进入指令链该怎么生效）', () => {
  it('新建终端实例：链记在实例上，首次连上时注入，不立即运行', () => {
    expect(planTerminalChain({ existing: null, commands: ['ssh a@h', 'wsl'] }))
      .toEqual({ chain: ['ssh a@h', 'wsl'], runNow: null })
  })

  it('实例已存在且已连着：链换上并就地逐层注入（先开环境再进层的常见顺序）', () => {
    const existing = { chain: [], status: { connected: true } }
    const plan = planTerminalChain({
      existing, commands: ['docker exec -it cc bash'], connected: true,
    })
    expect(plan.chain).toEqual(['docker exec -it cc bash'])
    expect(plan.runNow).toEqual(['docker exec -it cc bash'])
  })

  it('实例已存在但还没连上：只写链，连上时再由 Terminal 注入', () => {
    const plan = planTerminalChain({
      existing: { chain: [], status: { connected: false } }, commands: ['wsl'], connected: false,
    })
    expect(plan.chain).toEqual(['wsl'])
    expect(plan.runNow).toBeNull()
  })

  it('只是打开终端（没有链）：既不动已有链，也不运行', () => {
    const plan = planTerminalChain({
      existing: { chain: ['wsl'], status: { connected: true } }, commands: [], connected: true,
    })
    expect(plan.chain).toBeNull()
    expect(plan.runNow).toBeNull()
  })

  it('空白 / 非字符串指令被剔除', () => {
    expect(planTerminalChain({ existing: null, commands: ['wsl', '  ', null, 7] }).chain)
      .toEqual(['wsl'])
  })
})

describe('locateTerminalEnvNode（会话打开终端面板：按执行环境定位终端节点）', () => {
  const multiEnvRoots = [
    { id: 3, name: '终端', type: 'terminal', children: [
      { id: 4, name: '本地', type: 'terminal', env_id: '', children: [] },
      { id: 5, name: '10.0.0.9:7988', type: 'terminal', env_id: 'http://10.0.0.9:7988', children: [
        { id: 9, name: 'WSL', type: 'terminal_layer', uid: 'u9', command: 'wsl', children: [
          { id: 10, name: '容器[cc]', type: 'terminal_layer', uid: 'u10', command: 'docker exec -it cc bash', children: [] },
        ] },
      ] },
    ] },
  ]
  const singleEnvRoots = [
    { id: 3, name: '本地终端', type: 'terminal', env_id: '', children: [] },
  ]

  it('远程会话命中注册环境子节点，路径可供 collectTerminalTarget 解析命令链', () => {
    const found = locateTerminalEnvNode(multiEnvRoots, 'http://10.0.0.9:7988')
    expect(found.path).toBe('3/5')
    expect(found.node.uid).toBeUndefined()
    expect(found.node.children[0].children[0].command).toBe('docker exec -it cc bash')
  })

  it('本地会话在多环境森林命中"本地"子节点', () => {
    const found = locateTerminalEnvNode(multiEnvRoots, '')
    expect(found.path).toBe('3/4')
    expect(found.node.name).toBe('本地')
  })

  it('单环境森林没有 env 子节点时，本地会话以终端根为目标', () => {
    const found = locateTerminalEnvNode(singleEnvRoots, '')
    expect(found.path).toBe('3')
    expect(found.node.name).toBe('本地终端')
  })

  it('单环境根下已有用户自建终端层时，本地会话仍以根为目标（不误判为无节点）', () => {
    // 只有本地环境时根就是"本地"环境节点，terminal_layer 直接挂在它下面。
    // 若这里因为"没有 env 子节点"而返回 null，侧边栏会显示"该会话环境没有
    // 对应节点"，本地会话再也打不开自己那条嵌套终端链。
    const roots = [{
      id: 3, name: '本地终端', type: 'terminal', environment: 'local', env_id: '',
      children: [{ id: 9, name: 'WSL', type: 'terminal_layer', uid: 'u9', command: 'wsl', children: [] }],
    }]
    const found = locateTerminalEnvNode(roots, '')
    expect(found.path).toBe('3')
    expect(found.node.id).toBe(3)
  })

  it('环境未注册（如已注销的隧道）返回 null → 面板显示兜底', () => {
    expect(locateTerminalEnvNode(multiEnvRoots, 'tunnel:dead')).toBeNull()
    expect(locateTerminalEnvNode([], '')).toBeNull()
  })
})
