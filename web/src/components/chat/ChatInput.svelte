<script>
  import { t } from '../../lib/i18n.svelte.js'

  let { disabled = false, onSend, onStop, onStopForce, onOpenTemplatePanel, text = $bindable(''), isStreaming = false } = $props()

  // 输入框为空且不在流式状态时，显示"?"按钮（提示词模板入口）
  let showTemplateBtn = $derived(!isStreaming && !text.trim())

  // Double-click detection for forced abort.
  // Single click → normal abort; double-click → forced abort (kills tool processes).
  let _stopClickTimer = null

  function handleStopClick() {
    if (_stopClickTimer) {
      // Second click within 300ms → forced abort
      clearTimeout(_stopClickTimer)
      _stopClickTimer = null
      onStopForce?.()
    } else {
      // First click → start timer, fire normal abort on expiry
      _stopClickTimer = setTimeout(() => {
        _stopClickTimer = null
        onStop?.()
      }, 300)
    }
  }

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
  {#if showTemplateBtn}
    <!-- 输入框为空时显示"?"提示词模板按钮 -->
    <button
      class="send-btn template-btn"
      onclick={() => onOpenTemplatePanel?.()}
      title={t('promptTemplatePanelTitle')}
      disabled={disabled}
    >
      ?
    </button>
  {:else}
    <!-- 有内容或流式时显示发送/停止按钮 -->
    <button
      class="send-btn"
      class:stop={isStreaming}
      onclick={isStreaming ? handleStopClick : handleSend}
      disabled={isStreaming ? false : (disabled || !text.trim())}
      title={isStreaming ? t('stopBtnTooltip') : ''}
    >
      {#if isStreaming}⏹{:else}↑{/if}
    </button>
  {/if}
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
  /* "?"模板按钮样式：使用次要色调，区别于发送按钮 */
  .send-btn.template-btn {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border);
    font-size: 1rem;
    font-weight: 700;
  }
  .send-btn.template-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
</style>
