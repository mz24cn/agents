<script>
  import { onDestroy, tick } from 'svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { extractPastedFiles, buildFileRefs } from '../../lib/clipboard-paste.js'
  import { uploadFilesToPasteDir } from '../../lib/workspace-upload.js'
  import { workspace as workspaceApi } from '../../lib/api.js'
  import { remoteWorkspace } from '../../lib/remote-execution.svelte.js'
  import { serializeEditor } from '../../lib/chat-input-serialize.js'
  import { parseFileRefParts, restoreFileRefTags, hasFileRefs } from '../../lib/file-ref.js'
  import { VoiceRunWriter } from '../../lib/voice-insert.js'
  import VoiceInput from './VoiceInput.svelte'

  let { disabled = false, onSend, onStop, onStopForce, onPause, onResume, onEndPause, onInterject, isPaused = false, pauseRequested = false, canPause = true, onToggleTemplatePanel, onToggleWorkspacePanel, workspacePanelOpen = false, templatePanelOpen = false, text = $bindable(''), isStreaming = false, selectedAgentIds = [], agentList = [], remote = null, onError = () => {} } = $props()

  let editorEl = $state(null)
  let lastRenderedText = ''

  // Voice input: recognized words are written straight into the editor at the
  // caret (a selection is replaced), so they can be edited or discarded before
  // sending. The writer owns the in-flight run and keeps it from being
  // duplicated as the service refines the same segment.
  let voiceWriter = null
  let voiceInput = $state(null)   // VoiceInput instance (stop() on send/destroy)

  // @mention 相关状态
  let mentionMenuOpen = $state(false)
  let mentionMenuStyle = $state('')
  let mentionQuery = $state('')
  let mentionSelectedIndex = $state(0)
  let mentionStartOffset = $state(-1) // @ 符号在文本中的位置

  // 计算当前选中的AI代理列表
  let selectedAgents = $derived(
    selectedAgentIds
      .map(id => agentList.find(a => a.agent_id === id))
      .filter(Boolean)
  )

  // 过滤匹配的AI代理（显示所有可用的AI代理）
  let filteredAgents = $derived(
    selectedAgentIds.length === 0
      ? []  // 没有选中的AI代理，不显示@菜单
      : mentionQuery
        ? selectedAgents.filter(a =>
            (a.nickname || a.agent_id).toLowerCase().includes(mentionQuery.toLowerCase())
          )
        : selectedAgents
  )

  // 输入框为空且不在流式状态时，显示"?"按钮（提示词模板入口）
  let showTemplateBtn = $derived(!isStreaming && !isPaused && !text.trim())

  function createFileChip(ref) {
    const chip = document.createElement('span')
    chip.className = 'file-ref-chip'
    chip.contentEditable = 'false'
    chip.dataset.fileRef = ref
    chip.textContent = ref
    return chip
  }

  // The editor holds the <file> form; a revoked / re-edited / re-pasted message
  // may arrive in the backend's expanded form ([Image/Text file attached: …]
  // plus inlined content). Both become chips, so what is shown is what gets sent.
  function renderEditorFromText(value) {
    if (!editorEl) return
    const source = String(value ?? '')
    const fragment = document.createDocumentFragment()
    for (const part of parseFileRefParts(source)) {
      if (part.type === 'file') {
        if (part.ref) fragment.appendChild(createFileChip(part.ref))
      } else {
        fragment.appendChild(document.createTextNode(part.value))
      }
    }

    editorEl.replaceChildren(fragment)
    lastRenderedText = source
    // The live ASR run was part of the old DOM; a later partial starts a new
    // one at the caret instead of writing into a detached node.
    voiceWriter?.reset()
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
    const serialized = serializeEditor(editorEl, text)
    text = serialized
    lastRenderedText = serialized
  }

  // 检测 @ 符号并显示菜单
  function checkForMention() {
    const sel = window.getSelection()
    if (!sel.rangeCount || !editorEl) return

    const range = sel.getRangeAt(0)
    const node = range.startContainer
    if (node.nodeType !== Node.TEXT_NODE) {
      closeMentionMenu()
      return
    }

    const textContent = node.textContent
    const cursorPos = range.startOffset
    
    // 从光标位置向前查找 @ 符号
    let atIndex = -1
    for (let i = cursorPos - 1; i >= 0; i--) {
      if (textContent[i] === '@') {
        atIndex = i
        break
      }
      // 如果遇到空格或换行，停止查找
      if (textContent[i] === ' ' || textContent[i] === '\n') {
        break
      }
    }

    if (atIndex >= 0) {
      const query = textContent.slice(atIndex + 1, cursorPos)
      mentionQuery = query
      mentionStartOffset = atIndex
      mentionSelectedIndex = 0

      // 计算菜单位置（使用 fixed 定位，基于光标的视口坐标）
      const rect = range.getBoundingClientRect()
      if (rect) {
        const menuTop = rect.bottom + 4
        const menuLeft = rect.left
        mentionMenuStyle = `top: ${menuTop}px; left: ${menuLeft}px;`
      } else {
        mentionMenuStyle = ''
      }

      // 如果有匹配的AI代理，显示菜单
      if (filteredAgents.length > 0) {
        mentionMenuOpen = true
        adjustMenuPosition()
      } else {
        mentionMenuOpen = false
      }
    } else {
      closeMentionMenu()
    }
  }

  function closeMentionMenu() {
    mentionMenuOpen = false
    mentionQuery = ''
    mentionStartOffset = -1
  }

  // 菜单右下角超出窗口时平移
  async function adjustMenuPosition() {
    await tick()
    const menu = document.querySelector('.mention-menu')
    if (!menu) return
    const rect = menu.getBoundingClientRect()
    let adjusted = false
    let top = parseFloat(menu.style.top)
    let left = parseFloat(menu.style.left)
    // 右下角 y 超出窗口底部 → 上移
    if (rect.bottom > window.innerHeight) {
      top -= rect.bottom - window.innerHeight
      adjusted = true
    }
    // 右下角 x 超出窗口右侧 → 左移
    if (rect.right > window.innerWidth) {
      left -= rect.right - window.innerWidth
      adjusted = true
    }
    if (adjusted) {
      mentionMenuStyle = `top: ${top}px; left: ${left}px;`
    }
  }

  // 选择AI代理
  function selectAgent(agent) {
    if (mentionStartOffset < 0 || !editorEl) return

    const sel = window.getSelection()
    if (!sel.rangeCount) return

    const range = sel.getRangeAt(0)
    const node = range.startContainer
    if (node.nodeType !== Node.TEXT_NODE) return

    const textContent = node.textContent
    const cursorPos = range.startOffset

    // 构建新文本：@之前的部分 + AI代理名称 + 空格 + @之后的部分
    const before = textContent.slice(0, mentionStartOffset)
    const after = textContent.slice(cursorPos)
    const agentName = agent.nickname || agent.agent_id
    const newText = before + '@' + agentName + ' ' + after

    node.textContent = newText

    // 将光标移到AI代理名称后面的空格之后
    const newCursorPos = mentionStartOffset + agentName.length + 2
    const newRange = document.createRange()
    newRange.setStart(node, Math.min(newCursorPos, newText.length))
    newRange.setEnd(node, Math.min(newCursorPos, newText.length))
    sel.removeAllRanges()
    sel.addRange(newRange)

    closeMentionMenu()
    syncTextFromEditor()
  }

  function handleInput() {
    syncTextFromEditor()
    checkForMention()
  }

  /**
   * Insert restored text (chips + text) at the caret. Used when the clipboard
   * carries a previously sent message: its file references must come back as
   * chips, which execCommand('insertText') cannot produce.
   */
  function insertRestoredTextAtCaret(value) {
    const selection = window.getSelection()
    const range = selection && selection.rangeCount ? selection.getRangeAt(0) : null
    if (!editorEl || !range || !editorEl.contains(range.startContainer)) {
      // No caret inside the editor: go through the text pipeline, the $effect
      // below re-renders the editor from the new value.
      text = text ? `${text}${value}` : value
      return
    }
    const fragment = document.createDocumentFragment()
    let last = null
    for (const part of parseFileRefParts(value)) {
      const node = part.type === 'file' && part.ref
        ? createFileChip(part.ref)
        : (part.value ? document.createTextNode(part.value) : null)
      if (!node) continue
      fragment.appendChild(node)
      last = node
    }
    range.deleteContents()
    range.insertNode(fragment)
    if (last) {
      range.setStartAfter(last)
      range.collapse(true)
      selection.removeAllRanges()
      selection.addRange(range)
    }
  }

  /**
   * Paste handling:
   * - If the clipboard carries files (image / PDF / DOCX ...), upload them into
   *   the backend paste directory (/tmp on Linux, OS temp dir on Windows) and
   *   insert <file> references — equivalent to the workspace file manager's
   *   "paste upload" + "select file" operations.
   * - Otherwise insert the clipboard text at the caret (existing behaviour).
   */
  async function handlePaste(e) {
    e.preventDefault()
    const pastedFiles = extractPastedFiles(e.clipboardData)
    if (pastedFiles.length > 0) {
      await handlePasteFiles(pastedFiles)
      return
    }
    const pasted = (e.clipboardData?.getData('text/plain') ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    // Copying a chat message yields the backend's expanded form; fold it back
    // into chips instead of pasting "[Image file attached: …]" as prose.
    if (hasFileRefs(pasted)) {
      insertRestoredTextAtCaret(restoreFileRefTags(pasted))
      syncTextFromEditor()
      return
    }
    document.execCommand('insertText', false, pasted)
    syncTextFromEditor()
  }

  async function handlePasteFiles(files) {
    try {
      // Paste uploads must land in the environment this session actually runs
      // in: the parent (default) for local sessions, or the bound child env for
      // remote ones. `remote` is the same resolved env ChatPage hands the
      // workspace file manager, so the <file> refs we insert point at a real
      // file on the host that will execute the message.
      const api = remote ? remoteWorkspace : workspaceApi
      const paths = await uploadFilesToPasteDir(files, { api, cacheKey: remote?.id })
      const refs = buildFileRefs(paths)
      // Append the file references; the $effect below re-renders them as chips
      // and moves the caret to the end (same UX as selecting files from the
      // workspace file manager).
      text = text && !text.endsWith(' ') ? `${text} ${refs}` : `${text || ''}${refs}`
    } catch (err) {
      onError?.(`${t('pasteUploadFailed')}: ${err.message || err}`)
    }
  }

  // 外部更新 text（例如工作区选择文件、撤销重填、发送后清空）时，同步到富文本输入区。
  $effect(() => {
    const current = String(text ?? '')
    if (editorEl && current !== lastRenderedText) {
      renderEditorFromText(current)
      tick().then(() => placeCaretAtEnd())
    }
  })

  // The stop button becomes a small action menu while inference is streaming:
  // pause / stop / force stop. A click toggles the upward-opening menu; clicks
  // outside or Escape close it. (The former double-click force-kill gesture is
  // replaced by an explicit menu item — it was undiscoverable and conflicted
  // with the menu.)
  let stopMenuOpen = $state(false)
  let stopMenuEl = $state(null)
  let stopBtnEl = $state(null)

  // Idempotency lock for stop-menu actions: once an option is picked the
  // button goes disabled and stays that way until the session state actually
  // changes (streaming / paused / pause-requested flips) — the resulting
  // status event re-enables it. A page reload clears the lock by design
  // (e.g. stop didn't land and the user wants to escalate to force-stop).
  let actionPending = $state(false)
  $effect(() => {
    void [isStreaming, isPaused, pauseRequested]
    actionPending = false
  })

  function toggleStopMenu() {
    stopMenuOpen = !stopMenuOpen
  }

  function closeStopMenu() {
    stopMenuOpen = false
  }

  function pickStopAction(fn) {
    if (actionPending) return
    closeStopMenu()
    actionPending = true
    fn?.()
  }

  function handleDocumentClick(e) {
    if (!stopMenuOpen) return
    const target = e.target
    if (stopMenuEl?.contains(target) || stopBtnEl?.contains(target)) return
    closeStopMenu()
  }

  onDestroy(() => {
    stopMenuOpen = false
  })

  // Paused state: the send button resumes the interrupted inference. With a
  // draft in the box the draft wins (a normal send continues the intact
  // history — the backend treats the paused conversation as ordinary context).
  function handleResumeOrSend() {
    if (actionPending) return
    syncTextFromEditor()
    actionPending = true
    if (text.trim()) {
      handleSend()
      return
    }
    onResume?.()
  }

  function handleSend() {
    syncTextFromEditor()
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    // Stop capturing before handing the text over: a trailing ASR segment
    // would otherwise land in the box that has just been cleared.
    voiceInput?.stop?.({ discard: true })
    voiceWriter?.reset()
    onSend?.(trimmed)
    text = ''
  }

  // 推理途中的“附加新的消息”：草稿排队到当前推理的下一个轮边界。
  // 不清空输入框 —— 由 ChatPage 在收到 queued 确认后清空（请求失败时
  // 草稿保留，可直接重试）。
  // 与 stop-menu 其它动作不同，插话是同 generation 的续跑，不会翻转
  // isStreaming/isPaused/pauseRequested 中任何一个 —— 状态驱动的解锁
  // 永远不会触发。所以这里只上“请求在途”的瞬时锁，请求一落地就释放，
  // 让用户能立刻插下一条；幂等保护由在途锁本身承担。
  // 两个入口（推理中回车、菜单里的 💬）都必须走 submitInterject：漏掉
  // 解锁的那条路会把这一代推理的 暂停/停止/强制停止 全部禁用掉。
  function handleInterjectText() {
    syncTextFromEditor()
    const trimmed = text.trim()
    if (!trimmed || disabled) return null
    return onInterject?.(trimmed) ?? null
  }

  function submitInterject() {
    if (actionPending) return
    closeStopMenu()
    actionPending = true
    const release = () => { actionPending = false }
    let done = null
    try {
      done = handleInterjectText()
    } catch (err) {
      // A failure before the request even starts must not cost the user the
      // stop controls for the rest of the generation either.
      release()
      onError?.(String(err?.message || err))
      return
    }
    if (done && typeof done.then === 'function') {
      done.then(release, release)
    } else {
      release()
    }
  }

  /**
   * Voice input: write recognized words into the editor at the caret.
   *
   * `partial` events rewrite the in-flight run in place (the service sends the
   * whole segment again as it refines it); `commit` settles it. Both keep the
   * caret right after what was just spoken, so the next words continue there
   * and anything typed around them stays put.
   */
  function handleVoicePartial(value) {
    if (!voiceWriter && editorEl) voiceWriter = new VoiceRunWriter(editorEl)
    if (!voiceWriter) return
    voiceWriter.update(value)
    syncTextFromEditor()
  }

  function handleVoiceCommit(value) {
    if (!voiceWriter && editorEl) voiceWriter = new VoiceRunWriter(editorEl)
    if (!voiceWriter) return
    voiceWriter.commit(value)
    syncTextFromEditor()
  }

  function handleKeydown(e) {
    // 如果菜单打开，处理菜单导航
    if (mentionMenuOpen) {
      const agents = filteredAgents
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        mentionSelectedIndex = (mentionSelectedIndex + 1) % agents.length
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        mentionSelectedIndex = (mentionSelectedIndex - 1 + agents.length) % agents.length
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        if (agents.length > 0) {
          selectAgent(agents[mentionSelectedIndex])
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        closeMentionMenu()
        return
      }
    }

    if (e.key === 'Escape' && stopMenuOpen) {
      e.preventDefault()
      closeStopMenu()
      return
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (isStreaming) {
        // 推理中回车 = “附加新的消息”（与下拉菜单项同一动作）：不打断当前
        // 模型轮，草稿排队到下一个轮边界。输入框为空则不动作。
        if (onInterject) {
          submitInterject()
        }
        return
      }
      if (isPaused) {
        handleResumeOrSend()
        return
      }
      handleSend()
    } else if (e.key === 'Enter' && e.shiftKey) {
      e.preventDefault()
      document.execCommand('insertText', false, '\n')
      syncTextFromEditor()
    }
  }
</script>

<svelte:window onclick={handleDocumentClick} />
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
    {#if mentionMenuOpen && filteredAgents.length > 0}
      <div 
        class="mention-menu"
        style={mentionMenuStyle}
      >
        {#each filteredAgents as agent, index}
          <div
            class="mention-item"
            class:selected={index === mentionSelectedIndex}
            onmousedown={(e) => {
              e.preventDefault()
              selectAgent(agent)
            }}
            onmouseenter={() => mentionSelectedIndex = index}
          >
            <span class="mention-icon">🤖</span>
            <span class="mention-name">{agent.nickname || agent.agent_id}</span>
          </div>
        {/each}
      </div>
    {/if}
  </div>
  <!-- Two rows of two buttons. Top row: workspace file manager + voice input.
       Bottom row: send/stop (or the prompt-template entry) + the slot reserved
       for "pause inference", which will let a user message be injected while
       the agent is reasoning. -->
  <div class="btn-grid">
    <!-- 工作区文件管理器按钮：打开时显示减号，关闭时显示加号 -->
    <button
      class="send-btn workspace-btn"
      class:active={workspacePanelOpen}
      onclick={() => onToggleWorkspacePanel?.()}
      title={t('workspaceFileManager')}
      disabled={disabled}
    >
      {workspacePanelOpen ? '−' : '+'}
    </button>

    <VoiceInput
      bind:this={voiceInput}
      {disabled}
      onCommit={handleVoiceCommit}
      onPartial={handleVoicePartial}
      {onError}
    />

    {#if templatePanelOpen}
      <!-- 提示词面板打开时显示叉号关闭按钮 -->
      <button
        class="send-btn template-btn active"
        onclick={() => onToggleTemplatePanel?.()}
        title={t('promptTemplatePanelTitle')}
        disabled={disabled}
      >
        ✕
      </button>
    {:else if showTemplateBtn}
      <!-- 输入框为空时显示"?"提示词模板按钮 -->
      <button
        class="send-btn template-btn"
        onclick={() => onToggleTemplatePanel?.()}
        title={t('promptTemplatePanelTitle')}
        disabled={disabled}
      >
        ?
      </button>
    {:else if isStreaming}
      <!-- 推理中：⏹+▼ 动作菜单（暂停 / 停止 / 强制停止） -->
      <div class="stop-cell">
        {#if stopMenuOpen}
          <div class="stop-menu" bind:this={stopMenuEl}>
            {#if canPause}
              <button class="stop-menu-item" onclick={() => pickStopAction(onPause)}><span class="stop-menu-icon">⏸️</span>{t('pauseInference')}</button>
            {/if}
            <button class="stop-menu-item" onclick={() => pickStopAction(onStop)}><span class="stop-menu-icon">⏹️</span>{t('stopInference')}</button>
            <button class="stop-menu-item danger" onclick={() => pickStopAction(onStopForce)}><span class="stop-menu-icon">⛔</span>{t('forceStopInference')}</button>
            {#if onInterject && text.trim()}
              <button class="stop-menu-item" onclick={() => submitInterject()}><span class="stop-menu-icon">💬</span>{t('attachNewMessage')}</button>
            {/if}
          </div>
        {/if}
        <button
          bind:this={stopBtnEl}
          class="send-btn stop"
          class:pause-pending={pauseRequested}
          disabled={actionPending}
          onclick={toggleStopMenu}
          title={pauseRequested ? t('pausingTooltip') : t('stopBtnTooltip')}
        ><span class="stop-glyph">⏹</span><span class="caret">▼</span></button>
      </div>
    {:else if isPaused}
      <!-- 暂停态：与推理中同款控件——只有 ⏹▼。菜单：恢复执行 / 结束暂停任务 /
           附加新的消息（输入框有内容时；等价普通发送，后端自动从暂停点接管会话）。
           Enter：有内容=发送，空=恢复执行。 -->
      <div class="stop-cell">
        {#if stopMenuOpen}
          <div class="stop-menu" bind:this={stopMenuEl}>
            {#if onResume}
              <button class="stop-menu-item" onclick={() => pickStopAction(onResume)}><span class="stop-menu-icon">▶</span>{t('resumeInference')}</button>
            {/if}
            {#if onEndPause}
              <button class="stop-menu-item danger" onclick={() => pickStopAction(onEndPause)}><span class="stop-menu-icon">⏹️</span>{t('endPausedTask')}</button>
            {/if}
            {#if text.trim()}
              <button class="stop-menu-item" onclick={() => pickStopAction(handleSend)}><span class="stop-menu-icon">💬</span>{t('attachNewMessage')}</button>
            {/if}
          </div>
        {/if}
        <button
          bind:this={stopBtnEl}
          class="send-btn stop"
          class:paused-control={isPaused}
          disabled={actionPending}
          onclick={toggleStopMenu}
          title={t('pausedMenuTooltip')}
        ><span class="stop-glyph">⏸</span><span class="caret">▼</span></button>
      </div>
    {:else}
      <!-- 有内容时显示发送按钮 -->
      <button
        class="send-btn"
        onclick={handleSend}
        disabled={disabled || !text.trim()}
        title=""
      >↑</button>
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
    position: relative;
  }
  .mention-menu {
    position: fixed;
    z-index: 1000;
    width: 200px;
    max-height: 220px;
    overflow-y: auto;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    padding: 4px;
  }
  .mention-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .mention-item:hover,
  .mention-item.selected {
    background: var(--primary);
    color: #fff;
  }
  .mention-icon {
    font-size: 1rem;
  }
  .mention-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
  /* 2x2 button grid: workspace + voice on top, send/stop (+ pause/resume) below. */
  .btn-grid {
    display: grid;
    grid-template-columns: repeat(2, auto);
    gap: 4px;
    flex-shrink: 0;
    justify-content: end;
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
  /* 推理中的 ⏹▼ 停止动作按钮：比纯图标略宽以容纳下拉箭头 */
  .send-btn.stop {
    width: auto;
    padding: 0 6px;
    gap: 2px;
    background: var(--danger, #e53e3e);
  }
  .send-btn.stop:hover { background: #c53030; }
  .send-btn.stop { gap: 1px; }
  .send-btn.stop .stop-glyph { display: flex; align-items: center; line-height: 1; }
  .send-btn.stop:disabled { opacity: 0.55; cursor: not-allowed; }
  .send-btn.stop .caret { font-size: 0.55rem; line-height: 1; display: flex; align-items: center; transform: translateY(-0.5px); }
  /* pause 已受理、正等待轮边界生效 */
  .send-btn.stop.pause-pending { animation: stop-pending-pulse 1.2s ease-in-out infinite; }
  @keyframes stop-pending-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }

  .stop-cell { position: relative; display: flex; justify-content: flex-end; gap: 6px; }
  .stop-menu {
    position: absolute;
    bottom: calc(100% + 6px);
    right: 0;
    z-index: 20;
    display: flex;
    flex-direction: column;
    min-width: 130px;
    background: var(--bg-panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
    padding: 3px;
  }
  .stop-menu-item {
    border: none;
    background: transparent;
    color: var(--text-primary);
    font-size: 0.8rem;
    text-align: left;
    padding: 6px 10px;
    border-radius: 4px;
    cursor: pointer;
    white-space: nowrap;
  }
  .stop-menu-item:hover { background: var(--bg-secondary); }
  .stop-menu-item.danger { color: var(--danger, #e53e3e); }
  /* 菜单项前的示意图标：固定列宽，保证不同 emoji 宽度下文字左对齐 */
  .stop-menu-icon {
    display: inline-block;
    width: 1.5em;
    text-align: center;
  }
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
  /* Active state when panel is open */
  .send-btn.workspace-btn.active,
  .send-btn.template-btn.active {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
</style>
