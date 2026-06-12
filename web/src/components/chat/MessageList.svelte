<script>
  import MessageBubble from './MessageBubble.svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { slide } from 'svelte/transition'
  import { getContext } from 'svelte'
  import AppLogo from '../../lib/components/AppLogo.svelte'

  let { messages = [], agentList = [], onRevoke, onScrollAtBottom, shouldScrollToBottom = false, collapsedGroups = new Set(), onToggleCollapse } = $props()
  let listEl = $state(null)
  let isAtBottom = $state(true)
  
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
        result.push({ startIndex, endIndex, lastAssistantIndex, isCollapsed, hasIntermediate })
        i = endIndex + 1
      } else {
        // 非 user 消息（如 system 消息）单独处理，不折叠
        result.push({ startIndex: i, endIndex: i, lastAssistantIndex: -1, isCollapsed: false, hasIntermediate: false, ungrouped: true })
        i++
      }
    }
    return result
  })

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
        <div class="group" class:collapsed={group.isCollapsed} data-start-index={group.startIndex}>
          {#each Array.from({ length: group.endIndex - group.startIndex + 1 }) as _, offset}
            {@const msgIndex = group.startIndex + offset}
            {@const msg = messages[msgIndex]}
            {@const isUser = offset === 0}
            {@const isLastAssistant = msgIndex === group.lastAssistantIndex && group.lastAssistantIndex >= 0}
            {#if isUser}
              <!-- user 消息始终显示 -->
              <MessageBubble 
                {msg} 
                {agentList} 
                {onRevoke}
              />
            {:else if isLastAssistant}
              <!-- 最后一条 assistant 消息始终显示，带有折叠/展开按钮（仅当有中间消息时） -->
              <div data-anchor={group.startIndex}>
                <MessageBubble 
                  {msg} 
                  {agentList} 
                  {onRevoke}
                  collapseButton={group.hasIntermediate ? (group.isCollapsed ? 'expand' : 'collapse') : null}
                  onCollapse={group.hasIntermediate ? () => toggleCollapse(group.startIndex) : undefined}
                />
              </div>
            {:else if !group.isCollapsed}
              <!-- 中间消息（tool/assistant）：展开时用 slide 动画显示，折叠时隐藏 -->
              <div transition:slide={{ duration: SLIDE_DURATION }} data-intermediate={group.startIndex}>
                <MessageBubble 
                  {msg} 
                  {agentList} 
                  {onRevoke}
                />
              </div>
            {/if}
          {/each}
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
</style>
