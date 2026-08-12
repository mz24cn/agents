<script>
  import { onMount, onDestroy } from 'svelte';
  import { copyToClipboard } from '$lib/clipboard.js';
  import '@xterm/xterm/css/xterm.css';

  let { sessionId, workspace = '', visible = true, onStatusChange = null } = $props();

  let termEl;
  let term;
  let fitAddon;
  let ws;
  let connected = false;
  let loading = false;
  let resizeObserver;
  let retryTimeout;
  let destroyed = false;

  // ── mouse-tracking: persistently disable ──────────────────
  const MOUSE_MODES = [1000, 1002, 1003, 1006];

  /** Write disable-mouse sequence to the terminal emulator. */
  function disableMouseTracking() {
    try {
      term?.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
    } catch {}
  }

  function getWsUrl(sid) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let url = `${proto}//${location.host}/ws?terminal_id=${sid}`;
    if (workspace) {
      url += `&workspace=${encodeURIComponent(workspace)}`;
    }
    if (term && fitAddon) {
      try {
        url += `&cols=${term.cols}&rows=${term.rows}`;
      } catch {}
    }
    return url;
  }

  function connect() {
    if (destroyed) return;
    if (ws) { try { ws.close(); } catch {} }
    loading = true;
    connected = false;
    notifyStatus();

    ws = new WebSocket(getWsUrl(sessionId));
    const thisWs = ws;
    let terminalIdReceived = false;

    ws.onopen = () => {
      loading = false;
      connected = true;
      notifyStatus();
      try { fitAddon?.fit(); } catch {}
      if (term) handleResize({ cols: term.cols, rows: term.rows });
      disableMouseTracking();
    };

    ws.onmessage = (evt) => {
      if (!terminalIdReceived) {
        try {
          const data = JSON.parse(evt.data);
          if (data.__terminal_id) { terminalIdReceived = true; return; }
        } catch {}
      }
      try {
        term?.write(evt.data);
      } catch (e) {
        console.warn('[term] write error caught:', e.message || e);
      }
    };

    ws.onclose = () => {
      loading = false;
      connected = false;
      notifyStatus();
      // Only auto-retry if this is still the *active* websocket.  connect()
      // deliberately closes the previous socket when opening a new one; that
      // old socket must not schedule another retry, otherwise the 3s retry
      // would fight with the fresh connection and create an endless
      // connect/disconnect loop (visible as a flickering "Connecting...").
      if (!destroyed && thisWs === ws) scheduleRetry();
    };

    ws.onerror = () => {};
  }

  function notifyStatus() {
    onStatusChange?.({ connected, loading, error: null, terminalId: sessionId });
  }

  function scheduleRetry() {
    clearTimeout(retryTimeout);
    retryTimeout = setTimeout(connect, 3000);
  }

  function handleResize(size) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ __resize: true, cols: size.cols, rows: size.rows }));
    }
  }

  function doFit() {
    try { if (term && termEl?.offsetParent) fitAddon?.fit(); } catch {}
  }

  export function destroy() {
    destroyed = true;
    clearTimeout(retryTimeout);
    try { ws?.close(); } catch {}
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
      if (ws?.readyState === WebSocket.OPEN) ws.send(data);
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
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(`\x1b[?${mode};${pm}$y`);
            }
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
    clearTimeout(retryTimeout);
    resizeObserver?.disconnect();
    try { ws?.close(); } catch {}
    try { term?.dispose(); } catch {}
  });

  $effect(() => {
    requestAnimationFrame(() => {
      doFit();
      if (visible) term?.focus();
    });
  });

  $effect(() => {
    if (visible && sessionId && !destroyed) {
      if (!ws || ws.readyState === WebSocket.CLOSED) {
        const timer = setTimeout(() => connect(), 0);
        return () => clearTimeout(timer);
      }
    }
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
