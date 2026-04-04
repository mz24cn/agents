<script>
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import ThinkingBlock from './ThinkingBlock.svelte'
  import ToolCallCard from './ToolCallCard.svelte'
  import ImageViewer from './ImageViewer.svelte'
  import AudioPlayer from './AudioPlayer.svelte'
  import CopyButton from './CopyButton.svelte'
  import { t } from '../../lib/i18n.svelte.js'

  let { msg } = $props()
  let toolResultExpanded = $state(false)
</script>

<div class="message {msg.role}">
  <div class="role-label">
    {#if msg.role === 'user'}
      <span>{t('roleUser')}</span>
      <CopyButton getText={() => msg.content ?? ''} />
    {:else if msg.role === 'assistant'}
      {t('roleAssistant')}
    {:else if msg.role === 'system'}
      {t('roleSystem')}
    {:else if msg.role === 'function'}
      {t('roleFunction')}
    {:else}
      {msg.role}
    {/if}
  </div>

  {#if msg.thinking}
    <ThinkingBlock thinking={msg.thinking} />
  {/if}

  {#if msg.content}
    {#if msg.role === 'assistant'}
      <MarkdownRenderer content={msg.content} />
    {:else if msg.role === 'function'}
      <div class="tool-result-block">
        <button class="toggle" onclick={() => toolResultExpanded = !toolResultExpanded}>
          {toolResultExpanded ? t('collapseResult') : t('expandResult')}
        </button>
        {#if toolResultExpanded}
          <pre class="tool-result-content">{msg.content}</pre>
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
  .toggle {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
  }
  .toggle:hover { background: var(--bg-secondary); }
  .tool-result-content {
    margin: 4px 0 0;
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-all;
    padding: 6px 8px;
    background: rgba(0,0,0,0.05);
    border-radius: 4px;
    color: var(--text-secondary);
  }
</style>
