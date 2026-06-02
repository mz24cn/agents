<script>
  import { onDestroy, tick } from 'svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { disabled = false, onSend, onStop, onStopForce, onOpenTemplatePanel, onOpenWorkspacePanel, text = $bindable(''), isStreaming = false } = $props()

  let editorEl = $state(null)
  let lastRenderedText = ''

  // 输入框为空且不在流式状态时，显示"?"按钮（提示词模板入口）
  let showTemplateBtn = $derived(!isStreaming && !text.trim())

  function createFileChip(ref) {
    const chip = document.createElement('span')
    chip.className = 'file-ref-chip'
    chip.contentEditable = 'false'
    chip.dataset.fileRef = ref
    chip.textContent = ref
    return chip
  }

  function renderEditorFromText(value) {
    if (!editorEl) return
    const source = String(value ?? '')
    const re = /<file>\s*([^<]+?)\s*<\/file>/g
    const fragment = document.createDocumentFragment()
    let index = 0
    let match

    while ((match = re.exec(source)) !== null) {
      if (match.index > index) {
        fragment.appendChild(document.createTextNode(source.slice(index, match.index)))
      }
      fragment.appendChild(createFileChip(match[1]))
      index = re.lastIndex
    }
    if (index < source.length) {
      fragment.appendChild(document.createTextNode(source.slice(index)))
    }

    editorEl.replaceChildren(fragment)
    lastRenderedText = source
  }

  function serializeNode(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue ?? ''
    if (node.nodeType !== Node.ELEMENT_NODE) return ''

    const el = node
    if (el.dataset?.fileRef) {
      return `<file>${el.dataset.fileRef}</file>`
    }
    if (el.tagName === 'BR') return '\n'

    let out = ''
    for (const child of el.childNodes) out += serializeNode(child)
    if (el.tagName === 'DIV' || el.tagName === 'P') out += '\n'
    return out
  }

  function serializeEditor() {
    if (!editorEl) return text
    let out = ''
    for (const child of editorEl.childNodes) {
      // When a <div>/<p> follows a text node (e.g. first line is bare text,
      // subsequent lines wrapped in <div> by the browser), insert a newline
      // before the <div> to prevent lines from merging.
      if (out && !out.endsWith('\n') &&
        child.nodeType === Node.ELEMENT_NODE &&
        (child.tagName === 'DIV' || child.tagName === 'P')) {
        out += '\n'
      }
      out += serializeNode(child)
    }
    return out.replace(/\n$/g, '')
  }

  function placeCaretAtEnd() {
    if (!editorEl) return
    const range = document.createRange()
    const selection = window.getSelection()
    range.selectNodeContents(editorEl)
    range.collapse(false)
    selection.removeAllRanges()
    selection.addRange(range)
  }

  function syncTextFromEditor() {
    const serialized = serializeEditor()
    text = serialized
    lastRenderedText = serialized
  }

  function handleInput() {
    syncTextFromEditor()
  }

  function handlePaste(e) {
    e.preventDefault()
    const pasted = e.clipboardData?.getData('text/plain') ?? ''
    document.execCommand('insertText', false, pasted)
    syncTextFromEditor()
  }

  // 外部更新 text（例如工作区选择文件、撤销重填、发送后清空）时，同步到富文本输入区。
  $effect(() => {
    const current = String(text ?? '')
    if (editorEl && current !== lastRenderedText) {
      renderEditorFromText(current)
      tick().then(() => placeCaretAtEnd())
    }
  })

  // Double-click detection for forced abort.
  // Wait 300ms to decide whether this is a single click or a double click, then
  // send exactly one abort request: single → normal abort, double → forced abort.
  let _stopClickTimer = null
  let _stopClickCount = 0

  function handleStopClick() {
    _stopClickCount += 1
    if (_stopClickTimer) return

    _stopClickTimer = setTimeout(() => {
      const clickCount = _stopClickCount
      _stopClickTimer = null
      _stopClickCount = 0
      if (clickCount >= 2) {
        onStopForce?.()
      } else {
        onStop?.()
      }
    }, 300)
  }

  onDestroy(() => {
    if (_stopClickTimer) clearTimeout(_stopClickTimer)
  })

  function handleSend() {
    syncTextFromEditor()
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
    } else if (e.key === 'Enter' && e.shiftKey) {
      e.preventDefault()
      document.execCommand('insertText', false, '\n')
      syncTextFromEditor()
    }
  }
</script>

<div class="chat-input">
  <div class="input-shell">
    <div
      bind:this={editorEl}
      class="input-box"
      class:empty={!text.trim()}
      contenteditable={!disabled}
      role="textbox"
      tabindex={disabled ? -1 : 0}
      aria-multiline="true"
      aria-label={t('inputPlaceholder')}
      data-placeholder={t('inputPlaceholder')}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onpaste={handlePaste}
    ></div>
  </div>
  <div class="btn-stack">
    <!-- 工作区文件管理器按钮 -->
    <button
      class="send-btn workspace-btn"
      onclick={() => onOpenWorkspacePanel?.()}
      title={t('workspaceFileManager')}
      disabled={disabled}
    >
      +
    </button>
    
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
  .input-shell {
    flex: 1;
    min-width: 0;
  }
  .input-box {
    width: 100%;
    box-sizing: border-box;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 0.9rem;
    font-family: inherit;
    line-height: 1.4;
    min-height: 56px;
    max-height: 300px;
    overflow-y: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    outline: none;
  }
  .input-box:focus {
    border-color: var(--primary);
  }
  .input-box[contenteditable="false"] {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .input-box.empty::before {
    content: attr(data-placeholder);
    color: var(--text-secondary);
    pointer-events: none;
  }
  .input-box :global(.file-ref-chip) {
    display: inline-flex;
    align-items: center;
    vertical-align: baseline;
    max-width: min(520px, 100%);
    margin: 0 2px;
    padding: 1px 7px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--primary) 14%, var(--bg-secondary));
    border: 1px solid color-mix(in srgb, var(--primary) 35%, var(--border));
    color: var(--primary);
    font-size: 0.78rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    line-height: 1.6;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    user-select: all;
  }
  .input-box :global(.file-ref-chip)::before {
    content: '📎';
    margin-right: 4px;
    font-family: system-ui, sans-serif;
  }
  .btn-stack {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex-shrink: 0;
  }
  .send-btn {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    border: none;
    background: var(--primary);
    color: #fff;
    font-size: 0.95rem;
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
  /* "+"工作区按钮样式：使用次要色调 */
  .send-btn.workspace-btn {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border);
    font-size: 1rem;
    font-weight: 600;
  }
  .send-btn.workspace-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  /* "?"模板按钮样式：使用次要色调，区别于发送按钮 */
  .send-btn.template-btn {
    background: var(--bg-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border);
    font-size: 0.85rem;
    font-weight: 700;
  }
  .send-btn.template-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
</style>
