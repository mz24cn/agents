<script>
  import { onMount, onDestroy } from 'svelte';
  import { copyToClipboard } from '$lib/clipboard.js';
  import { LOCAL_ENV_ID, openTerminalChannel } from '$lib/terminals-mux.js';
  import '@xterm/xterm/css/xterm.css';

  // remoteEnvId: the child environment this channel belongs to ('' = local).
  // It is only an id: the tab's ONE shared terminal socket carries it as
  // `env_id` on the open frame and the parent dials that environment (over its
  // tunnel or directly).  The browser therefore needs nothing else about the
  // child — no URL, no credential, no extra connection.
  let { sessionId, workspace = '', visible = true, onStatusChange = null, remoteEnvId = '' } = $props();

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
    fitAddon.fit();

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
  // 时也走这里。visible 只决定何时首次挂载通道：隐藏期间仍然保持附着 ——
  // 后端 PTY 一直在跑，但脱离期间打印的内容不会补发，浏览器里那份 xterm
  // 回滚缓冲就是唯一的历史；共享 socket 让这份"随时可恢复"不再多占连接。
  let wsTarget = ''
  $effect(() => {
    // 目标 = 执行环境（local 或某个子环境的 id）+ 会话。用 env id 而不是 host：
    // 两个子环境可能同 host 不同部署路径，host 相同但环境不同。
    const target = `${remoteEnvId || LOCAL_ENV_ID}\u0000${sessionId ?? ''}`
    if (!visible || !sessionId || destroyed) return
    if (target === wsTarget && chan) return
    wsTarget = target
    const timer = setTimeout(() => connect(), 0);
    return () => clearTimeout(timer);
  });
</script>

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
</style>
