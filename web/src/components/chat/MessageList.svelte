<script>
  import MessageBubble from './MessageBubble.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { messages = [] } = $props()
  let listEl = $state(null)
  let isAtBottom = $state(true)

  function onScroll() {
    if (!listEl) return
    const threshold = 100
    isAtBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < threshold
  }

  $effect(() => {
    // 追踪整个 messages 内容变化（包括流式追加）
    JSON.stringify(messages)
    if (listEl && isAtBottom) listEl.scrollTop = listEl.scrollHeight
  })
</script>

<div class="message-list" bind:this={listEl} onscroll={onScroll}>
  {#if messages.length === 0}
    <div class="empty">{t('startChat')}</div>
  {:else}
    {#each messages as msg, i (i)}
      <MessageBubble {msg} />
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
    text-align: center;
    color: var(--text-secondary);
    padding: 60px 0;
    font-size: 1rem;
  }
</style>
