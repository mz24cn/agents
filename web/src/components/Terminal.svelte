<script>
  import { onMount, onDestroy } from 'svelte';
  import { copyToClipboard } from '$lib/clipboard.js';
  import { LOCAL_ENV_ID, openTerminalChannel } from '$lib/terminals-mux.js';
  import { createChainRunner } from '$lib/terminalChain.js';
  import { t } from '$lib/i18n.svelte.js';
  import '@xterm/xterm/css/xterm.css';

  // remoteEnvId: the child environment this channel belongs to ('' = local).
  // It is only an id: the tab's ONE shared terminal socket carries it as
  // `env_id` on the open frame and the parent dials that environment (over its
  // tunnel or directly).  The browser therefore needs nothing else about the
  // child — no URL, no credential, no extra connection.
  // chainCommands: 嵌套终端层链（双击 terminal_layer 树节点时传入）。连接
  // 成功后按序注入各层进入指令；遇到密码/确认提示停手等用户，检测到 shell
  // 提示符再继续（$lib/terminalChain.js）。
  let { sessionId, workspace = '', visible = true, onStatusChange = null, remoteEnvId = '', chainCommands = [] } = $props();

  let termEl;
  let term;
  let fitAddon;
  // Channel handle on this origin's shared terminal socket (see
  // $lib/terminals-mux.js).  Not reactive: the $effect below must not
  // re-trigger when it is (re)assigned.
  let chan;
  let connected = false;
  let loading = false;
  let resizeObserver;
  let destroyed = false;

  // ── 嵌套终端链 ─────────────────────────────────────────────
  // runner 在首次连接成功时创建（每终端实例只跑一次）；状态供状态条展示。
  let chainRunner = null;
  let chainState = $state(null);
  let chainBarVisible = $derived(
    visible && chainState && !['off', 'idle', 'done'].includes(chainState.state));
  const chainHint = $derived.by(() => {
    if (!chainState) return ''
    if (chainState.state === 'await-input') return t('terminalChainAwaiting')
    if (chainState.state === 'probing') return t('terminalChainProbing')
    if (chainState.state === 'paused') return t('terminalChainPaused')
    return t('terminalChainStep', { index: chainState.index + 1, total: chainState.total })
  })

  function makeChainRunner(commands) {
    return createChainRunner(commands, {
      send: (text) => chan?.send(text),
      onState: (st) => { chainState = st },
    })
  }

  function startChainIfNeeded() {
    if (chainRunner || !chainCommands?.length) return
    chainRunner = makeChainRunner(chainCommands)
    chainRunner.begin()
  }

  // 双击终端层时，这个会话的终端可能早就连着（先双击环境行开 PTY、再双击其中
  // 一层进得去是最常见的顺序）：就地按链逐层注入，而不是等一次可能永远不来的
  // 重连。ChatPage 通过 bind:this 调用它（见 lib/terminalChain.js 的 planTerminalChain）。
  export function runChain(commands) {
    const list = (commands || []).filter((c) => typeof c === 'string' && c.trim())
    if (!list.length) return
    chainRunner?.cancel()
    chainRunner = makeChainRunner(list)
    chainRunner.begin()
  }

  // ── mouse-tracking: persistently disable ──────────────────
  const MOUSE_MODES = [1000, 1002, 1003, 1006];

  /** Write disable-mouse sequence to the terminal emulator. */
  function disableMouseTracking() {
    try {
      term?.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
    } catch {}
  }

  // 本地终端和子环境终端挂的是**同一个端点**：本 tab 只有一条终端 WS，通道属于
  // 哪个环境由 open 帧里的 env_id 决定（$lib/terminals-mux.js 与母端的
  // runtime/terminal_env_bridge.py）。所以多开几个子环境不会再多占浏览器每原点
  // ~6 条连接的额度，子端凭据也始终留在母端。
  function muxUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/v1/terminals/ws`;
  }

  function closeChannel() {
    try { chan?.close(); } catch {}
    chan = null;
  }

  function connect() {
    if (destroyed) return;
    closeChannel();
    loading = true;
    connected = false;
    notifyStatus();

    // Every terminal of this tab shares one WebSocket; the hub dials it,
    // re-dials on loss (re-opening each channel) and drops the socket once the
    // last channel closes.
    chan = openTerminalChannel({
      url: muxUrl(),
      terminalId: sessionId,
      // 会话绑定到子环境时，这条通道路由到那个环境；否则归 local。
      envId: remoteEnvId || LOCAL_ENV_ID,
      ...(workspace ? { workspace } : {}),
      cols: term?.cols ?? 80,
      rows: term?.rows ?? 24,
      onData: (chunk) => {
        chainRunner?.onOutput(chunk);
        try {
          term?.write(chunk);
        } catch (e) {
          console.warn('[term] write error caught:', e.message || e);
        }
      },
      onStatus: ({ connected: up, loading: busy, error = null }) => {
        connected = up;
        if (busy !== undefined) loading = busy;
        if (up) {
          try { fitAddon?.fit(); } catch {}
          disableMouseTracking();
          startChainIfNeeded();
        }
        notifyStatus(error);
      },
    });
  }

  function notifyStatus(error = null) {
    onStatusChange?.({ connected, loading, error, terminalId: sessionId });
  }

  function handleResize(size) {
    chan?.resize(size.cols, size.rows);
  }

  function doFit() {
    try { if (term && termEl?.offsetParent) fitAddon?.fit(); } catch {}
  }

  export function destroy() {
    destroyed = true;
    chainRunner?.cancel();
    closeChannel();
  }

  onMount(async () => {
    const [{ Terminal: _Terminal }, { FitAddon: _FitAddon }] = await Promise.all([
      import('@xterm/xterm'),
      import('@xterm/addon-fit')
    ]);
    term = new _Terminal({
      cursorBlink: true,
      theme: { background: '#1e1e1e' },
      fontFamily: "'courier-new', courier, monospace",
      fontSize: 15,
    });
    fitAddon = new _FitAddon();
    term.loadAddon(fitAddon);
    term.onResize(handleResize);

    term.onData((data) => {
      chan?.send(data);
    });

    term.open(termEl);
    // 隐藏挂载是常态了（刷新后恢复出来的窗口就是「开着但收起」）：display:none
    // 下 offsetParent 为 null，fit() 量出 0 会把 1x1 的 winsize 发给 PTY。跳过
    // 它，通道按 xterm 默认的 80x24 起；面板展开时下面的 ResizeObserver 与
    // effect 会 fit 并把真实尺寸 resize 过去。
    if (termEl?.offsetParent) fitAddon.fit();

    // ── Ctrl+C / Cmd+C → copy when there is a selection ──────────
    // xterm.js sends Ctrl+C to the PTY as SIGINT by default.  When the user
    // has an active selection, intercept it and copy to the clipboard instead
    // (returning false stops the keystroke from reaching the PTY).  With no
    // selection, let it through so it still behaves as a normal interrupt.
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== 'keydown') return true;
      const key = String(ev.key || '').toLowerCase();
      if ((ev.ctrlKey || ev.metaKey) && key === 'c') {
        if (term.hasSelection && term.hasSelection()) {
          const sel = term.getSelection ? term.getSelection() : '';
          if (sel) copyToClipboard(sel).catch(() => {});
          return false; // swallow → no \x03 is sent to the PTY
        }
      }
      return true;
    });

    // ── DECRPM workaround (CSI ? Ps $ p) ───────────────────
    try {
      term.parser.registerCsiHandler(
        { prefix: '?', intermediates: '$', final: 'p' },
        (params) => {
          const modes = [];
          try {
            for (let i = 0; i < params.length; i++) modes.push(params[i]);
          } catch {
            try { modes.push(...params); } catch {}
          }
          for (const mode of modes) {
            const pm = (mode === 1) ? 2 : 0;
            chan?.send(`\x1b[?${mode};${pm}$y`);
          }
          return true;
        }
      );
    } catch (e) {
      console.warn('[term] registerCsiHandler for DECRPM failed:', e.message || e);
    }

    // ── DECSET interceptor: block shell from enabling mouse tracking ──
    //     CSI ? Ps h   (private-mode SET).  If any Ps is a mouse mode
    //     (1000/1002/1003/1006) we swallow the sequence so the terminal
    //     never enters mouse-tracking and text-selection keeps working.
    try {
      term.parser.registerCsiHandler(
        { prefix: '?', final: 'h' },
        (params) => {
          let hasMouse = false;
          const modes = [];
          try {
            for (let i = 0; i < params.length; i++) modes.push(params[i]);
          } catch {
            try { modes.push(...params); } catch {}
          }
          for (const m of modes) {
            if (MOUSE_MODES.includes(m)) {
              hasMouse = true;
            }
          }
          if (hasMouse) {
            // Also ensure the internal mode flag is off
            disableMouseTracking();
            return true; // handled — swallow the sequence
          }
          return false; // not a mouse mode — let xterm.js process normally
        }
      );
    } catch (e) {
      console.warn('[term] registerCsiHandler for DECSET failed:', e.message || e);
    }

    // ── Initial mouse-tracking disable ─────────────────────
    disableMouseTracking();

    resizeObserver = new ResizeObserver(() => { if (visible) requestAnimationFrame(doFit); });
    if (termEl) resizeObserver.observe(termEl);
  });

  onDestroy(() => {
    destroyed = true;
    chainRunner?.cancel();
    resizeObserver?.disconnect();
    closeChannel();
    try { term?.dispose(); } catch {}
  });

  $effect(() => {
    requestAnimationFrame(() => {
      doFit();
      if (visible) term?.focus();
    });
  });

  // 连接目标 = 执行环境（本地/子端）+ 会话。目标变化（切换执行环境、
  // 绑定异步解析完成）时关掉旧通道并在新的共享 socket 上重新打开；首次挂载
  // 时也走这里。**不看 visible**：ChatPage 只为「有终端窗口的会话」挂载本组件
  // （手工打开的，或刷新后按会话 meta.terminal 恢复出来的），所以挂载就是「窗口
  // 开着」，收起只是没显示 —— 后端 PTY 由此立刻存在，隐藏期间也一直在跑。脱离
  // 期间打印的内容不会补发，浏览器里那份 xterm 回滚缓冲就是唯一的历史；共享
  // socket 让这份"随时可恢复"不再多占连接。
  let wsTarget = ''
  $effect(() => {
    // 目标 = 执行环境（local 或某个子环境的 id）+ 会话。用 env id 而不是 host：
    // 两个子环境可能同 host 不同部署路径，host 相同但环境不同。
    const target = `${remoteEnvId || LOCAL_ENV_ID}\u0000${sessionId ?? ''}`
    if (!sessionId || destroyed) return
    if (target === wsTarget && chan) return
    wsTarget = target
    const timer = setTimeout(() => connect(), 0);
    return () => clearTimeout(timer);
  });
</script>

{#if chainBarVisible}
  <div class="terminal-chain-bar" data-state={chainState.state}>
    <span class="terminal-chain-text">{chainHint}</span>
    {#if chainState.state === 'paused' || chainState.state === 'await-input'}
      <button class="terminal-chain-continue" onclick={() => chainRunner?.resume()}>{t('terminalChainContinue')}</button>
    {/if}
  </div>
{/if}
<div class="terminal-container" class:visible bind:this={termEl}></div>

<style>
  .terminal-container {
    width: 100%;
    height: 100%;
    background: #1e1e1e;
  }

  .terminal-container:not(.visible) {
    display: none;
  }

  .terminal-chain-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 5px 10px;
    background: #233041;
    color: #cfe3f5;
    font-size: 0.78rem;
  }
  .terminal-chain-bar[data-state="await-input"] { background: #3d3324; color: #f0dcb8; }
  .terminal-chain-bar[data-state="paused"] { background: #40282a; color: #f2c9c4; }
  .terminal-chain-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .terminal-chain-continue {
    padding: 3px 10px;
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 5px;
    background: transparent;
    color: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .terminal-chain-continue:hover { background: rgba(255, 255, 255, 0.12); }
</style>
