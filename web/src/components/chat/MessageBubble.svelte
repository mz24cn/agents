<script>
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import ThinkingBlock from './ThinkingBlock.svelte'
  import ToolCallCard from './ToolCallCard.svelte'
  import ImageViewer from './ImageViewer.svelte'
  import AudioPlayer from './AudioPlayer.svelte'
  import CopyButton from './CopyButton.svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'

  /**
   * Detect the content type of a tool result.
   * Returns { type: 'json'|'script'|'markdown', lang?: string }
   */
  function detectToolResultType(content) {
    const trimmed = content.trimStart()
    // JSON: must start with { or [
    if (trimmed[0] === '{' || trimmed[0] === '[') {
      try {
        JSON.parse(content)
        return { type: 'json', lang: 'json' }
      } catch {
        // not valid JSON, fall through
      }
    }
    // Shebang detection
    const firstLine = content.split('\n')[0]
    if (firstLine.startsWith('#!')) {
      if (/\b(bash|sh)\b/.test(firstLine)) return { type: 'script', lang: 'bash' }
      if (/\bpython\b/.test(firstLine))    return { type: 'script', lang: 'python' }
    }
    return { type: 'markdown' }
  }

  function renderToolResult(content) {
    const { type, lang } = detectToolResultType(content)
    if (type === 'json' || type === 'script') {
      return { html: highlight(content, lang), lang }
    }
    return { html: null, lang: null }
  }

  let { msg } = $props()

  function buildStatTooltip(s) {
    const fmtTokens = (n) => n >= 10000 ? `${(n / 1000).toFixed(1)}k` : `${n}`
    const fmtMs = (n) => n == null ? 'N/A' : n >= 10000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`

    const lines = []
    lines.push(`${t('tokenIn')} ${fmtTokens(s.prompt_tokens)}   ${t('tokenOut')} ${fmtTokens(s.completion_tokens)}   ${t('tokenTotal')} ${fmtTokens(s.total_tokens)}`)
    if (s.total_prompt_tokens !== s.prompt_tokens || s.total_completion_tokens !== s.completion_tokens) {
      lines.push(`${t('tokenCumIn')} ${fmtTokens(s.total_prompt_tokens)}   ${t('tokenCumOut')} ${fmtTokens(s.total_completion_tokens)}   ${t('tokenCumTotal')} ${fmtTokens(s.total_all_tokens)}`)
    }
    if (s.ttft_ms != null) lines.push(`${t('statTtft')} ${fmtMs(s.ttft_ms)}`)
    if (s.net_ms != null)  lines.push(`${t('statNet')} ${fmtMs(s.net_ms)}`)
    if (s.total_ms != null) lines.push(`${t('statRound')} ${fmtMs(s.total_ms)}`)
    if (s.overall_ms != null) lines.push(`${t('statOverall')} ${fmtMs(s.overall_ms)}`)
    return lines.join('\n')
  }

  // 工具结果：超过5行默认收缩，否则默认展开
  const toolResultLines = (msg.content ?? '').split('\n').length
  const toolResultOverLimit = msg.role === 'tool' && toolResultLines > 5
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
        {#if msg.stat}
          <span class="token-stats" title={buildStatTooltip(msg.stat)}>
            {msg.stat.prompt_tokens >= 10000 ? `${(msg.stat.prompt_tokens/1000).toFixed(1)}k` : msg.stat.prompt_tokens}/{msg.stat.completion_tokens >= 10000 ? `${(msg.stat.completion_tokens/1000).toFixed(1)}k` : msg.stat.completion_tokens} tokens
          </span>
        {/if}
        {#if msg.thinking}
          <button class="toggle-btn" onclick={toggleThinking}>
            {thinkingExpanded ? t('collapseThinking') : t('expandThinking')}
          </button>
        {/if}
        <CopyButton getText={() => msg.content ?? ''} />
      </div>
    {:else if msg.role === 'system'}
      {t('roleSystem')}
    {:else if msg.role === 'tool'}
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
    {:else if msg.role === 'tool'}
      {@const detected = renderToolResult(msg.content)}
      <div class="tool-result-block" ondblclick={() => toolResultExpanded = !toolResultExpanded}>
        {#if detected.html}
          {#if toolResultExpanded}
            <div class="code-block tool-result-code">
              {#if detected.lang}<span class="code-lang">{detected.lang.toUpperCase()}</span>{/if}
              <pre><code class="language-{detected.lang}">{@html detected.html}</code></pre>
            </div>
          {:else}
            <div class="code-block tool-result-code">
              {#if detected.lang}<span class="code-lang">{detected.lang.toUpperCase()}</span>{/if}
              <pre class="preview"><code class="language-{detected.lang}">{@html detected.html.split('\n').slice(0, 5).join('\n')}</code></pre>
            </div>
          {/if}
        {:else if toolResultExpanded}
          <div class="tool-result-markdown"><MarkdownRenderer content={msg.content} /></div>
        {:else}
          <div class="tool-result-markdown preview-fade"><MarkdownRenderer content={msg.content.split('\n').slice(0, 5).join('\n')} /></div>
        {/if}
      </div>
    {:else}
      <div class="content">{msg.content}</div>
    {/if}
  {:else if msg.prompt_template}
    <div class="content template-ref">
      <span class="template-ref-id">{msg.prompt_template}</span>
      {#if msg.arguments && Object.keys(msg.arguments).length > 0}
        {#each Object.entries(msg.arguments) as [k, v]}
          <span class="template-ref-arg"><span class="arg-key">{k}</span><span class="arg-val">{v}</span></span>
        {/each}
      {/if}
    </div>
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
  .message.tool {
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
    font-size: 0.75rem;
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
  .token-stats {
    font-size: 0.75rem;
    color: var(--text-secondary, #888);
    opacity: 0.75;
    white-space: nowrap;
    letter-spacing: 0.02em;
  }
  .content {
    white-space: pre-wrap;
    line-height: 1.5;
    font-size: 0.9rem;
  }
  .template-ref { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; white-space: normal; }
  .template-ref-id { font-family: monospace; font-weight: 600; font-size: 0.9rem; }
  .template-ref-arg { display: inline-flex; align-items: center; gap: 2px; font-size: 0.82rem; background: rgba(255,255,255,0.15); border-radius: 4px; padding: 1px 6px; }
  .arg-key { opacity: 0.75; }
  .arg-key::after { content: ':'; margin-right: 3px; }
  .arg-val { font-weight: 500; }
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
  .tool-result-block {
    margin-top: 4px;
    cursor: pointer;
    user-select: none;
  }
  .tool-result-code {
    position: relative;
    margin: 0;
  }
  .tool-result-code :global(pre) {
    font-size: 0.8rem;
    padding: 6px 8px;
    margin: 0;
    border-radius: 4px;
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
  }
  .tool-result-code :global(pre.preview) {
    -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
    mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  }
  .tool-result-code :global(code) {
    background: none;
    padding: 0;
    font-size: inherit;
    line-height: 1.5;
  }
  .tool-result-code :global(.code-lang) {
    position: absolute;
    bottom: 0;
    right: 0;
    padding: 2px 8px;
    font-size: 0.7em;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border-radius: 4px 0 4px 0;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    z-index: 1;
  }
  .tool-result-markdown {
    font-size: 0.85rem;
  }
  .tool-result-markdown.preview-fade {
    -webkit-mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
    mask-image: linear-gradient(to bottom, black 60%, transparent 100%);
  }

  /* Syntax highlighting for injected @html spans */
  .tool-result-code :global(.hl-keyword) { color: #c792ea; }
  .tool-result-code :global(.hl-string)  { color: #c3e88d; }
  .tool-result-code :global(.hl-comment) { color: #546e7a; font-style: italic; }
  .tool-result-code :global(.hl-number)  { color: #f78c6c; }
  .tool-result-code :global(.hl-boolean) { color: #ff5874; }
  .tool-result-code :global(.hl-null)    { color: #ff5874; }
  .tool-result-code :global(.hl-key)     { color: #82aaff; }
  .tool-result-code :global(.hl-variable){ color: #f07178; }

  :root[data-theme="light"] .tool-result-code :global(.hl-keyword) { color: #7c3aed; }
  :root[data-theme="light"] .tool-result-code :global(.hl-string)  { color: #16a34a; }
  :root[data-theme="light"] .tool-result-code :global(.hl-comment) { color: #6b7280; font-style: italic; }
  :root[data-theme="light"] .tool-result-code :global(.hl-number)  { color: #c2410c; }
  :root[data-theme="light"] .tool-result-code :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .tool-result-code :global(.hl-null)    { color: #dc2626; }
  :root[data-theme="light"] .tool-result-code :global(.hl-key)     { color: #1d4ed8; }
  :root[data-theme="light"] .tool-result-code :global(.hl-variable){ color: #b45309; }
</style>
