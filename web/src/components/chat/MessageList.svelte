<script>
  import MessageBubble from './MessageBubble.svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { slide } from 'svelte/transition'
  import { getContext, onDestroy } from 'svelte'
  import AppLogo from '../../lib/components/AppLogo.svelte'
  import IconDisplay from '../../lib/components/IconDisplay.svelte'
  import CopyButton from './CopyButton.svelte'

  import { resolveFileJournalTurnKey } from '../../lib/file-journals.js'
  import { isToolErrorContent } from '../../lib/tool-result.js'
  import { currentSession, messageScrollRequest } from '../../lib/session-state.svelte.js'

  let { messages = [], agentList = [], displayMessageDetails = false, onRevoke, onScrollAtBottom, shouldScrollToBottom = false, collapsedGroups = new Set(), onToggleCollapse, fileJournalTurnKeyMap = {}, fileDiffVisible = new Set(), fileDiffCache = {}, onToggleFileDiff, retryAssistantIndex = -1, onRetryLastInference, retryDisabled = false } = $props()
  let listEl = $state(null)
  let isAtBottom = $state(true)
  // Ephemeral per-turn overrides. This state lives only in this keyed
  // MessageList instance and is discarded when the session changes.
  let replyModeOverrides = $state(new Set())
  
  // 获取 appLogoStore
  const appLogoStore = getContext('appLogoStore')
  let logoConfig = $state('')
  
  if (appLogoStore) {
    appLogoStore.subscribe(value => {
      logoConfig = value
    })
  }

  // 展开/收起动画时长（ms），setTimeout 需要与此保持一致
  const SLIDE_DURATION = 600

  function onScroll() {
    if (!listEl) return
    const threshold = 100
    isAtBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < threshold
    if (onScrollAtBottom) onScrollAtBottom(isAtBottom)
  }

  function scrollToBottom() {
    if (listEl) {
      listEl.scrollTop = listEl.scrollHeight
    }
  }

  $effect(() => {
    // 追踪整个 messages 内容变化（包括流式追加）
    JSON.stringify(messages)
    if (listEl && (isAtBottom || shouldScrollToBottom)) {
      scrollToBottom()
    }
  })

  let handledScrollToken = 0
  let scrollCorrectionTimer = null

  function targetScrollTop(target) {
    const listRect = listEl.getBoundingClientRect()
    const targetRect = target.getBoundingClientRect()
    const targetTop = listEl.scrollTop + targetRect.top - listRect.top
    const centeredTop = targetTop - Math.max(0, (listEl.clientHeight - targetRect.height) / 2)
    return Math.max(0, Math.min(centeredTop, listEl.scrollHeight - listEl.clientHeight))
  }

  function scrollToUserMessage(targetIndex, token) {
    if (!listEl || token !== messageScrollRequest.token) return
    const target = listEl.querySelector(`[data-user-message-index="${targetIndex}"]`)
    if (!target) return

    // scrollIntoView may choose an outer scroll container and its final position
    // is easily invalidated while restored message cards finish laying out. Scroll
    // the actual message list explicitly, then correct once after the animation.
    isAtBottom = false
    listEl.scrollTo({ top: targetScrollTop(target), behavior: 'smooth' })

    if (scrollCorrectionTimer) clearTimeout(scrollCorrectionTimer)
    scrollCorrectionTimer = setTimeout(() => {
      if (!listEl || token !== messageScrollRequest.token) return
      const settledTarget = listEl.querySelector(`[data-user-message-index="${targetIndex}"]`)
      if (settledTarget) listEl.scrollTop = targetScrollTop(settledTarget)
      scrollCorrectionTimer = null
    }, 550)
  }

  $effect(() => {
    const token = messageScrollRequest.token
    const targetSessionId = messageScrollRequest.sessionId
    const targetIndex = messageScrollRequest.messageIndex
    // A cross-session request may arrive before restoration has populated this
    // keyed list. Tracking the message count retries after the target DOM exists.
    messages.length
    if (!token || token === handledScrollToken || targetSessionId !== currentSession.sessionId || !Number.isInteger(targetIndex)) return
    if (messages[targetIndex]?.role !== 'user') return

    handledScrollToken = token
    requestAnimationFrame(() => {
      requestAnimationFrame(() => scrollToUserMessage(targetIndex, token))
    })
  })

  onDestroy(() => {
    if (scrollCorrectionTimer) clearTimeout(scrollCorrectionTimer)
  })

  // 计算消息分组：每个 user 消息及其后的 assistant/tool 消息为一组
  const groups = $derived(() => {
    const result = []
    let i = 0
    while (i < messages.length) {
      if (messages[i].role === 'user') {
        const startIndex = i
        let endIndex = i
        let lastAssistantIndex = -1
        for (let j = i + 1; j < messages.length; j++) {
          if (messages[j].role === 'user') break
          endIndex = j
          if (messages[j].role === 'assistant') {
            lastAssistantIndex = j
          }
        }
        const isCollapsed = collapsedGroups.has(startIndex)
        // 是否存在中间消息（user 和 lastAssistant 之间的 tool/assistant）
        const hasIntermediate = lastAssistantIndex >= 0 && lastAssistantIndex > startIndex + 1

        // Compact blocks: same-agent assistant/tool loops share one outer card.
        const compactAgentBlocks = []
        let compactStart = startIndex + 1
        let compactAgentId = null
        for (let j = startIndex + 1; j <= endIndex; j++) {
          const m = messages[j]
          const msgAgentId = m.agent_id || m.assistant_id || null
          if (msgAgentId && compactAgentId && msgAgentId !== compactAgentId) {
            compactAgentBlocks.push({ start: compactStart, end: j - 1, agentId: compactAgentId })
            compactStart = j
            compactAgentId = msgAgentId
          } else if (!compactAgentId && msgAgentId) {
            compactAgentId = msgAgentId
          }
        }
        if (compactStart <= endIndex) {
          compactAgentBlocks.push({ start: compactStart, end: endIndex, agentId: compactAgentId })
        }

        // Detailed blocks preserve the old behavior: each assistant message starts
        // a new visual block and its following tool messages stay with it.
        const detailedAgentBlocks = []
        let detailedStart = startIndex + 1
        let detailedAgentId = null
        for (let j = startIndex + 1; j <= endIndex; j++) {
          const m = messages[j]
          const msgAgentId = m.agent_id || m.assistant_id || null
          if (m.role === 'assistant') {
            if (detailedStart < j) detailedAgentBlocks.push({ start: detailedStart, end: j - 1, agentId: detailedAgentId })
            detailedStart = j
            detailedAgentId = msgAgentId
          } else if (m.role === 'tool') {
            if (msgAgentId && detailedAgentId && msgAgentId !== detailedAgentId) {
              detailedAgentBlocks.push({ start: detailedStart, end: j - 1, agentId: detailedAgentId })
              detailedStart = j
              detailedAgentId = msgAgentId
            } else if (!detailedAgentId && msgAgentId) {
              detailedAgentId = msgAgentId
            }
          }
        }
        if (detailedStart <= endIndex) {
          detailedAgentBlocks.push({ start: detailedStart, end: endIndex, agentId: detailedAgentId })
        }

        result.push({ startIndex, endIndex, lastAssistantIndex, isCollapsed, hasIntermediate, compactAgentBlocks, detailedAgentBlocks })
        i = endIndex + 1
      } else {
        // 非 user 消息（如 system 消息）单独处理，不折叠
        result.push({ startIndex: i, endIndex: i, lastAssistantIndex: -1, isCollapsed: false, hasIntermediate: false, compactAgentBlocks: [], detailedAgentBlocks: [], ungrouped: true })
        i++
      }
    }
    return result
  })

  function isGroupDetailed(startIndex) {
    return replyModeOverrides.has(startIndex) ? !displayMessageDetails : displayMessageDetails
  }

  function toggleReplyMode(startIndex) {
    const targetDetailed = !isGroupDetailed(startIndex)
    const next = new Set(replyModeOverrides)
    if (next.has(startIndex)) next.delete(startIndex)
    else next.add(startIndex)
    replyModeOverrides = next
    // Switching to detailed view should immediately reveal the whole turn,
    // even if it had previously been auto-collapsed.
    if (targetDetailed && collapsedGroups.has(startIndex) && onToggleCollapse) {
      onToggleCollapse(startIndex)
    }
  }

  function getBlockAgent(block) {
    if (!block?.agentId) return null
    return agentList.find(a => a.agent_id === block.agentId) || null
  }

  function getBlockName(block) {
    const agent = getBlockAgent(block)
    if (agent?.nickname) return agent.nickname
    const firstAssistant = messages.slice(block.start, block.end + 1).find(m => m.role === 'assistant')
    return firstAssistant?.agent_nickname || firstAssistant?.name || t('roleAssistant')
  }

  function getBlockStat(block) {
    for (let i = block.end; i >= block.start; i--) {
      if (messages[i]?.role === 'assistant' && messages[i]?.stat) return messages[i].stat
    }
    return null
  }

  function formatTokenCount(n) {
    return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : `${n ?? 0}`
  }

  function formatTimestamp(timestamp) {
    if (!timestamp) return ''
    const date = new Date(timestamp)
    if (Number.isNaN(date.getTime())) return ''
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    return `${month}/${day} ${hours}:${minutes}:${seconds}`
  }

  function getBlockInferenceTimes(block) {
    let outputStartedAt = ''
    let loopCompletedAt = ''
    for (let i = block.start; i <= block.end; i++) {
      const msg = messages[i]
      if (msg?.role !== 'assistant') continue
      if (!outputStartedAt && msg.stat?.first_token_timestamp) {
        outputStartedAt = msg.stat.first_token_timestamp
      }
      if (msg.stat) {
        loopCompletedAt = msg.stat.completed_at || msg.timestamp || loopCompletedAt
      }
    }
    return { outputStartedAt, loopCompletedAt }
  }

  function buildBlockStatTooltip(s, block) {
    const fmtMs = (n) => n == null ? 'N/A' : n >= 10000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`
    const lines = [
      `${t('tokenIn')} ${formatTokenCount(s.prompt_tokens)}   ${t('tokenOut')} ${formatTokenCount(s.completion_tokens)}   ${t('tokenTotal')} ${formatTokenCount(s.total_tokens)}`
    ]
    if (s.total_prompt_tokens !== s.prompt_tokens || s.total_completion_tokens !== s.completion_tokens) {
      lines.push(`${t('tokenCumIn')} ${formatTokenCount(s.total_prompt_tokens)}   ${t('tokenCumOut')} ${formatTokenCount(s.total_completion_tokens)}   ${t('tokenCumTotal')} ${formatTokenCount(s.total_all_tokens)}`)
    }
    if (s.ttft_ms != null) lines.push(`${t('statTtft')} ${fmtMs(s.ttft_ms)}`)
    if (s.net_ms != null) lines.push(`${t('statNet')} ${fmtMs(s.net_ms)}`)
    if (s.total_ms != null) lines.push(`${t('statRound')} ${fmtMs(s.total_ms)}`)
    if (s.overall_ms != null) lines.push(`${t('statOverall')} ${fmtMs(s.overall_ms)}`)
    const { outputStartedAt, loopCompletedAt } = getBlockInferenceTimes(block)
    const outputStartedTime = formatTimestamp(outputStartedAt)
    const loopCompletedTime = formatTimestamp(loopCompletedAt)
    if (outputStartedTime) lines.push(`${t('statOutputStartedTime')} ${outputStartedTime}`)
    if (loopCompletedTime) lines.push(`${t('statLoopCompletedTime')} ${loopCompletedTime}`)
    return lines.join('\n')
  }

  function getToolResultId(msg) {
    return msg?.tool_use_id || null
  }

  function getCompactToolResults(block) {
    const callIds = new Set()
    for (let i = block.start; i <= block.end; i++) {
      for (const tc of messages[i]?.tool_calls || []) {
        const id = tc.id || tc.tool_use_id
        if (id) callIds.add(id)
      }
    }
    const results = {}
    for (let i = block.start; i <= block.end; i++) {
      const msg = messages[i]
      // Match every result frame to its originating call, including temporary
      // streaming results.  The call card owns the status icon and detail area;
      // standalone tool bubbles are only a fallback for unmatched results.
      const resultId = getToolResultId(msg)
      if (msg?.role === 'tool' && resultId && callIds.has(resultId)) {
        results[resultId] = msg
      }
    }
    return results
  }

  function isMatchedToolResult(msg, toolResults) {
    const resultId = getToolResultId(msg)
    return msg?.role === 'tool' && resultId && toolResults[resultId]
  }

  function isToolError(content) {
    return isToolErrorContent(content)
  }

  function getBlockCopyText(block) {
    const parts = []
    for (let i = block.start; i <= block.end; i++) {
      const msg = messages[i]
      if (!msg) continue
      if (msg.role === 'assistant') {
        if (msg.content) parts.push(msg.content)
        for (const tc of msg.tool_calls || []) parts.push(`🛠️${tc.name ?? t('unknownTool')}`)
      } else if (msg.role === 'tool') {
        parts.push(isToolError(msg.content) ? '✖️' : '✔️')
      }
    }
    return parts.join(' ')
  }

  function toggleCollapse(startIndex) {
    if (!listEl) {
      if (onToggleCollapse) onToggleCollapse(startIndex)
      return
    }

    const isCurrentlyCollapsed = collapsedGroups.has(startIndex)

    // 展开前记录状态
    const wasAtBottom = isAtBottom
    let anchorTopBefore = 0
    if (isCurrentlyCollapsed) {
      const anchorEl = listEl.querySelector(`[data-anchor="${startIndex}"]`)
      if (anchorEl) {
        anchorTopBefore = anchorEl.getBoundingClientRect().top
      }
    }

    // 执行状态切换
    if (onToggleCollapse) onToggleCollapse(startIndex)

    if (isCurrentlyCollapsed) {
      // 展开：slide 动画结束后再修正滚动位置
      setTimeout(() => {
        if (wasAtBottom) {
          // 展开前在底部 → 动画结束后继续滚到底，anchor 保持在底部
          scrollToBottom()
        } else {
          // 展开前不在底部 → 测量 anchor 位移并补偿
          const anchorEl = listEl.querySelector(`[data-anchor="${startIndex}"]`)
          if (!anchorEl) return
          const anchorTopAfter = anchorEl.getBoundingClientRect().top
          const delta = anchorTopAfter - anchorTopBefore
          if (Math.abs(delta) > 1) {
            listEl.scrollTop += delta
          }
        }
      }, SLIDE_DURATION + 20)
    }
    // 折叠时 anchor 本身不动，无需调整
  }
</script>

<div class="message-list" bind:this={listEl} onscroll={onScroll}>
  {#if messages.length === 0}
    <div class="empty">
      <div class="logo-container">
        {#if logoConfig && logoConfig.trim() !== ''}
          <AppLogo class="empty-custom-logo" />
        {/if}
        <img src="/logo.svg" alt="Logo" class="default-logo" />
      </div>
      <div class="empty-text">{t('startChat')}</div>
    </div>
  {:else}
    {#each groups() as group (group.startIndex)}
      {#if group.ungrouped}
        <!-- 非 user 消息（如 system 消息）单独显示，不折叠 -->
        <MessageBubble msg={messages[group.startIndex]} {agentList} {onRevoke} />
      {:else}
        {@const userMsg = messages[group.startIndex]}
        {@const fileJournalTurnKey = resolveFileJournalTurnKey(fileJournalTurnKeyMap, userMsg.timestamp)}
        {@const groupDetailed = isGroupDetailed(group.startIndex)}
        <div class="group" class:collapsed={group.isCollapsed} data-start-index={group.startIndex}>
          <!-- user 消息始终显示 -->
          <MessageBubble
            msg={userMsg}
            {agentList}
            {onRevoke}
            scrollTargetIndex={group.startIndex}
            hasFileChanges={fileJournalTurnKey !== null}
            fileDiffData={fileJournalTurnKey ? (fileDiffCache[fileJournalTurnKey] || null) : null}
            fileDiffVisible={fileJournalTurnKey ? fileDiffVisible.has(fileJournalTurnKey) : false}
            onToggleFileDiff={fileJournalTurnKey && onToggleFileDiff ? () => onToggleFileDiff(fileJournalTurnKey) : undefined}
          />

          {#if groupDetailed && !group.isCollapsed}
            <!-- Detailed mode: preserve the original per-message cards and execution folding. -->
            {#each group.detailedAgentBlocks as block (block.start)}
              <div class="agent-block detailed">
                {#each Array.from({ length: block.end - block.start + 1 }) as _, offset}
                  {@const msgIndex = block.start + offset}
                  {@const msg = messages[msgIndex]}
                  {@const isLastAssistant = msgIndex === group.lastAssistantIndex && group.lastAssistantIndex >= 0}
                  {#if isLastAssistant}
                    <div data-anchor={group.startIndex}>
                      <MessageBubble
                        {msg}
                        {agentList}
                        {onRevoke}
                        replyDetailed={true}
                        onToggleReplyMode={() => toggleReplyMode(group.startIndex)}
                        collapseButton={group.hasIntermediate ? 'collapse' : null}
                        onCollapse={group.hasIntermediate ? () => toggleCollapse(group.startIndex) : undefined}
                        showRetry={msgIndex === retryAssistantIndex}
                        onRetry={msgIndex === retryAssistantIndex ? onRetryLastInference : undefined}
                        {retryDisabled}
                      />
                    </div>
                  {:else}
                    <div transition:slide={{ duration: SLIDE_DURATION }} data-intermediate={group.startIndex}>
                      <MessageBubble {msg} {agentList} {onRevoke} />
                    </div>
                  {/if}
                {/each}
              </div>
            {/each}
          {:else if !groupDetailed}
            <!-- Compact mode: one assistant-style card per continuous agent block. -->
            {#each group.compactAgentBlocks as block (block.start)}
              {@const blockAgent = getBlockAgent(block)}
              {@const blockStat = getBlockStat(block)}
              {@const blockToolResults = getCompactToolResults(block)}
              <div class="agent-block compact" data-anchor={group.startIndex}>
                <div class="agent-block-label">
                  {#if blockAgent}
                    <IconDisplay value={blockAgent.avatar || '🤖'} size={18} />
                  {/if}
                  <span>{getBlockName(block)}</span>
                  <div class="agent-block-actions">
                    {#if blockStat}
                      <span class="agent-block-tokens" title={buildBlockStatTooltip(blockStat, block)}>
                        {formatTokenCount(blockStat.prompt_tokens)}/{formatTokenCount(blockStat.completion_tokens)} tokens
                      </span>
                    {/if}
                    {#if retryAssistantIndex >= block.start && retryAssistantIndex <= block.end && onRetryLastInference}
                      <button class="agent-block-retry-btn" onclick={onRetryLastInference} disabled={retryDisabled}>{t('retryLastInference')}</button>
                    {/if}
                    {#if block.end === group.endIndex}
                      <button class="agent-block-mode-btn" onclick={() => toggleReplyMode(group.startIndex)}>{t('detailedReply')}</button>
                    {/if}
                    <CopyButton getText={() => getBlockCopyText(block)} />
                  </div>
                </div>
                <div class="agent-block-content">
                  {#each Array.from({ length: block.end - block.start + 1 }) as _, offset}
                    {@const msg = messages[block.start + offset]}
                    {#if !isMatchedToolResult(msg, blockToolResults)}
                      <MessageBubble {msg} {agentList} {onRevoke} compact={true} toolResultsById={blockToolResults} />
                    {/if}
                  {/each}
                </div>
              </div>
            {/each}
          {:else}
            <!-- 折叠状态：只显示最后一条 assistant 消息 -->
            {#if group.lastAssistantIndex >= 0}
              {@const msg = messages[group.lastAssistantIndex]}
              <div data-anchor={group.startIndex}>
                <MessageBubble
                  {msg}
                  {agentList}
                  {onRevoke}
                  replyDetailed={true}
                  onToggleReplyMode={() => toggleReplyMode(group.startIndex)}
                  collapseButton={group.hasIntermediate ? 'expand' : null}
                  onCollapse={group.hasIntermediate ? () => toggleCollapse(group.startIndex) : undefined}
                  showRetry={group.lastAssistantIndex === retryAssistantIndex}
                  onRetry={group.lastAssistantIndex === retryAssistantIndex ? onRetryLastInference : undefined}
                  {retryDisabled}
                />
              </div>
            {/if}
          {/if}
        </div>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .message-list {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    color: var(--text-secondary);
    padding: 40px 0;
  }
  .empty :global(.logo-container) {
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .empty-custom-logo {
    flex-shrink: 0;
  }
  .default-logo {
    width: auto;
    height: 64px;
  }
  .empty-text {
    font-size: 1rem;
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .group.collapsed {
    gap: 12px;
  }

  /* Detailed mode keeps the original, visually invisible grouping container. */
  .agent-block.detailed {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* Compact mode: one assistant-style card for the complete continuous
     assistant/tool loop of the same agent. */
  .agent-block.compact {
    align-self: flex-start;
    box-sizing: border-box;
    width: min(85%, 100%);
    padding: 10px 14px;
    border-radius: 8px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text);
  }
  .agent-block-label {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-bottom: 4px;
    font-size: 0.9rem;
    font-weight: 600;
    opacity: 0.8;
  }
  .agent-block-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: auto;
  }
  .agent-block-mode-btn {
    padding: 2px 8px;
    font-family: inherit;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border: none;
    border-radius: 4px;
    cursor: pointer;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .agent-block-mode-btn:hover {
    background: var(--bg-secondary, rgba(0,0,0,0.2));
    color: var(--text, #333);
  }
  .agent-block-retry-btn {
    padding: 2px 8px;
    font-family: inherit;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border: none;
    border-radius: 4px;
    cursor: pointer;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .agent-block-retry-btn:hover:not(:disabled) {
    background: var(--bg-secondary, rgba(0,0,0,0.2));
    color: var(--text, #333);
  }
  .agent-block-retry-btn:active:not(:disabled) {
    background: var(--primary, #4a9eff);
    color: #fff;
  }
  .agent-block-retry-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .agent-block-mode-btn:active {
    background: var(--primary, #4a9eff);
    color: #fff;
  }
  .agent-block-tokens {
    color: var(--text-secondary, #888);
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
    letter-spacing: 0.02em;
    opacity: 0.75;
  }
  .agent-block-content {
    display: block;
    min-width: 0;
    line-height: 1.6;
  }
</style>
