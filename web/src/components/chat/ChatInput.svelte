<script>
  import { t } from '../../lib/i18n.svelte.js'

  let { disabled = false, onSend, onStop, onClear, text = $bindable(''), hasMessages = false, isStreaming = false } = $props()

  function handleSend() {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend?.(trimmed)
    text = ''
  }

  function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (isStreaming) return
      handleSend()
    }
  }
</script>

<div class="chat-input">
  <textarea
    class="input-box"
    placeholder={t('inputPlaceholder')}
    bind:value={text}
    onkeydown={handleKeydown}
    {disabled}
    rows="2"
  ></textarea>
  <button
    class="send-btn"
    class:stop={isStreaming}
    onclick={isStreaming ? () => onStop?.() : handleSend}
    disabled={isStreaming ? false : (disabled || !text.trim())}
  >
    {#if isStreaming}⏹{:else}↑{/if}
  </button>
  <button
    class="clear-btn"
    onclick={() => onClear?.()}
    title={t('clearChat')}
    disabled={!hasMessages}
  >
    🗑
  </button>
</div>

<style>
  .chat-input {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    padding: 12px;
    border-top: 1px solid var(--border);
    background: var(--bg);
  }
  .input-box {
    flex: 1;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 0.9rem;
    resize: none;
    font-family: inherit;
    line-height: 1.4;
  }
  .input-box:focus { outline: none; border-color: var(--primary); }
  .input-box:disabled { opacity: 0.6; cursor: not-allowed; }
  .send-btn {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    border: none;
    background: var(--primary);
    color: #fff;
    font-size: 1.1rem;
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  .send-btn:hover:not(:disabled) { background: var(--primary-hover); }
  .send-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .send-btn.stop { background: var(--danger, #e53e3e); }
  .send-btn.stop:hover { background: #c53030; }
  .clear-btn {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 1rem;
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background-color 0.15s, color 0.15s;
  }
  .clear-btn:hover:not(:disabled) { background: var(--danger); color: #fff; border-color: var(--danger); }
  .clear-btn:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
