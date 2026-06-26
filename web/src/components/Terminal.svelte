<script>
  import { onMount, onDestroy } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import { FitAddon } from '@xterm/addon-fit';
  import '@xterm/xterm/css/xterm.css';

  let { sessionId, visible = true, onStatusChange = null, onDestroyRequest = null } = $props();

  let termEl;
  let term;
  let fitAddon;
  let ws;
  let connected = $state(false);
  let loading = $state(false);
  let resizeObserver;
  let retryTimeout;
  let destroyed = false;  // Flag to prevent reconnection after explicit destroy

  function getWsUrl(sid) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws?terminal_id=${sid}`;
  }

  function connect() {
    if (ws) { try { ws.close(); } catch {} }
    loading = true;
    connected = false;
    notifyStatus();

    ws = new WebSocket(getWsUrl(sessionId));
    let terminalIdReceived = false;

    ws.onopen = () => {
      loading = false;
      connected = true;
      notifyStatus();
      setTimeout(() => fitAddon?.fit(), 100);
    };

    ws.onmessage = (evt) => {
      if (!terminalIdReceived) {
        try {
          const data = JSON.parse(evt.data);
          if (data.__terminal_id) { terminalIdReceived = true; return; }
        } catch {}
      }
      term?.write(evt.data);
    };

    ws.onclose = () => {
      loading = false;
      connected = false;
      notifyStatus();
      if (!destroyed) {
        scheduleRetry();
      }
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

  export function reconnect() {
    clearTimeout(retryTimeout);
    connect();
  }

  export function destroy() {
    destroyed = true;
    clearTimeout(retryTimeout);
    try { ws?.close(); } catch {}
  }

  onMount(() => {
    term = new Terminal({
      cursorBlink: true,
      theme: { background: '#1e1e1e' },
      fontFamily: "'courier-new', courier, monospace",
      fontSize: 15
    });
    fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.onResize(handleResize);
    term.onData((data) => {
      if (ws?.readyState === WebSocket.OPEN) ws.send(data);
    });
    term.open(termEl);
    fitAddon.fit();

    resizeObserver = new ResizeObserver(() => { if (visible) requestAnimationFrame(doFit); });
    if (termEl) resizeObserver.observe(termEl);

    // 初始连接
    if (sessionId) connect();
  });

  onDestroy(() => {
    destroyed = true;  // Prevent ws.onclose from scheduling retry
    clearTimeout(retryTimeout);
    resizeObserver?.disconnect();
    try { ws?.close(); } catch {}
    try { term?.dispose(); } catch {}
  });

  // 仅用于 fit 和 focus，不触发连接
  $effect(() => {
    requestAnimationFrame(() => {
      doFit();
      if (visible) term?.focus();
    });
  });

  // 监听 visible 变化：重新显示时重连
  $effect(() => {
    if (visible && sessionId && !destroyed) {
      // 仅在完全断开时连接（不包括正在连接中）
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
