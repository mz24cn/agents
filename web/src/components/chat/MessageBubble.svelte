<script>
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import ThinkingBlock from './ThinkingBlock.svelte'
  import ToolCallCard from './ToolCallCard.svelte'
  import ImageViewer from './ImageViewer.svelte'
  import AudioPlayer from './AudioPlayer.svelte'
  import CopyButton from './CopyButton.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { msg } = $props()

  // 工具结果：超过5行默认收缩，否则默认展开
  const toolResultLines = (msg.content ?? '').split('\n').length
  const toolResultOverLimit = msg.role === 'function' && toolResultLines > 5
  let toolResultExpanded = $state(!toolResultOverLimit)

  // 思考过程：有正文或工具调用时自动收起；用户手动操作后不再自动跟随
  let thinkingUserToggled = $state(false)
  let thinkingExpanded = $state(true)
  $effect(() => {
    if (!thinkingUserToggled) {
      thinkingExpanded = !(msg.content || msg.tool_calls)
    }
  })
  function toggleThinking() {
    thinkingUserToggled = true
    thinkingExpanded = !thinkingExpanded
  }
</script>

<div class="message {msg.role}">
  <div class="role-label">
    {#if msg.role === 'user'}
      <span>{t('roleUser')}</span>
      <CopyButton getText={() => msg.content ?? ''} />
    {:else if msg.role === 'assistant'}
      <span>{t('roleAssistant')}</span>
      <div class="role-actions">
        {#if msg.thinking}
          <button class="toggle-btn" onclick={toggleThinking}>
            {thinkingExpanded ? t('collapseThinking') : t('expandThinking')}
          </button>
        {/if}
        <CopyButton getText={() => msg.content ?? ''} />
      </div>
    {:else if msg.role === 'system'}
      {t('roleSystem')}
    {:else if msg.role === 'function'}
      <span>{t('roleFunction')}</span>
      <div class="role-actions">
        {#if toolResultOverLimit}
          <button class="toggle-btn" onclick={() => toolResultExpanded = !toolResultExpanded}>
            {toolResultExpanded ? t('collapseResult') : t('expandResult')}
          </button>
        {/if}
        <CopyButton getText={() => msg.content ?? ''} />
      </div>
    {:else}
      {msg.role}
    {/if}
  </div>

  {#if msg.thinking}
    <ThinkingBlock thinking={msg.thinking} expanded={thinkingExpanded} />
  {/if}

  {#if msg.content}
    {#if msg.role === 'assistant'}
      <MarkdownRenderer content={msg.content} />
    {:else if msg.role === 'function'}
      <div class="tool-result-block">
        {#if toolResultExpanded}
          <pre class="tool-result-content">{msg.content}</pre>
        {:else}
          <pre class="tool-result-content preview">{msg.content.split('\n').slice(0, 5).join('\n')}</pre>
        {/if}
      </div>
    {:else}
      <div class="content">{msg.content}</div>
    {/if}
  {/if}

  {#if msg.tool_calls}
    <ToolCallCard toolCalls={msg.tool_calls} />
  {/if}

  {#if msg.images && msg.images.length > 0}
    <ImageViewer images={msg.images} />
  {/if}

  {#if msg.audio}
    <AudioPlayer audio={msg.audio} />
  {/if}
</div>

<style>
  .message {
    padding: 10px 14px;
    border-radius: 8px;
    max-width: 85%;
    word-break: break-word;
  }
  .message.user {
    align-self: flex-end;
    background: var(--primary);
    color: #fff;
  }
  .message.assistant {
    align-self: flex-start;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text);
  }
  .message.system {
    align-self: center;
    background: var(--bg-secondary);
    border: 1px dashed var(--border);
    color: var(--text-secondary);
    font-size: 0.85rem;
    max-width: 90%;
  }
  .message.function {
    align-self: flex-start;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.85rem;
    width: 85%;
  }
  .role-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.75rem;
    font-weight: 600;
    margin-bottom: 4px;
    opacity: 0.8;
  }
  .role-actions {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .toggle-btn {
    padding: 2px 8px;
    font-size: 0.7em;
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
  .toggle-btn:hover {
    background: var(--bg-secondary, rgba(0,0,0,0.2));
    color: var(--text, #333);
  }
  .content {
    white-space: pre-wrap;
    line-height: 1.5;
    font-size: 0.9rem;
  }
  .message.user :global(.copy-btn) {
    color: rgba(255,255,255,0.7);
    background: rgba(255,255,255,0.15);
  }
  .message.user :global(.copy-btn:hover) {
    color: #fff;
    background: rgba(255,255,255,0.25);
  }
  .message.user :global(.copy-btn:active) {
    background: rgba(255,255,255,0.4);
    color: #fff;
  }
  .tool-result-block { margin-top: 4px; }
  .tool-result-content {
    margin: 0;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    padding: 6px 8px;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
    color: var(--text-secondary);
  }
  .tool-result-content.preview {
    -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
    mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  }
</style>
