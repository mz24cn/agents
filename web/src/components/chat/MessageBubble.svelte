<script>
  import MarkdownRenderer from './MarkdownRenderer.svelte'
  import ThinkingBlock from './ThinkingBlock.svelte'
  import ToolCallCard from './ToolCallCard.svelte'
  import ImageViewer from './ImageViewer.svelte'
  import AudioPlayer from './AudioPlayer.svelte'
  import CopyButton from './CopyButton.svelte'
  import FileDiffViewer from './FileDiffViewer.svelte'
  import IconDisplay from '../../lib/components/IconDisplay.svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'
  import { isToolErrorContent } from '../../lib/tool-result.js'

  let { msg, agentList = [], onRevoke, collapseButton = null, onCollapse, hasFileChanges = false, onToggleFileDiff, fileDiffData = null, fileDiffVisible = false, compact = false, replyDetailed = null, onToggleReplyMode, toolResultsById = {}, scrollTargetIndex = null, showRetry = false, onRetry, retryDisabled = false } = $props()

  // ── Compact mode helpers ──
  /** Determine icon for tool result in compact mode */
  let toolResultIcon = $derived.by(() => {
    if (msg.role !== 'tool') return ''
    if (msg.streaming === true || (msg.sub_messages && Object.values(msg.sub_messages).some(sm => sm.streaming))) return '⏳'
    const tc = toolContentSource
    if (!tc) return '\u2714\uFE0F'  // empty = check mark
    if (isToolErrorContent(tc)) return '\u2716\uFE0F'
    return '\u2714\uFE0F'
  })

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
    let displayContent = content

    if (type === 'json' || type === 'script') {
      // Pretty-print JSON to match tool call argument display
      if (type === 'json') {
        try {
          displayContent = JSON.stringify(JSON.parse(content), null, 2)
        } catch {
          // Invalid JSON — fall back to raw content
        }
      }
      return { html: highlight(displayContent, lang), lang, displayContent }
    }
    return { html: null, lang: null, displayContent }
  }

  function renderContentParts(content) {
    const source = String(content ?? '')
    const re = /<file>\s*([^<]+?)\s*<\/file>/g
    const parts = []
    let index = 0
    let match
    while ((match = re.exec(source)) !== null) {
      if (match.index > index) parts.push({ type: 'text', value: source.slice(index, match.index) })
      parts.push({ type: 'file', value: match[1].trim() })
      index = re.lastIndex
    }
    if (index < source.length) parts.push({ type: 'text', value: source.slice(index) })
    return parts
  }

  // 获取AI代理信息
  const matchedAgent = $derived((msg.agent_id || msg.assistant_id) ? agentList.find(a => a.agent_id === (msg.agent_id || msg.assistant_id)) : null)
  const agentNickname = $derived(matchedAgent?.nickname)
  const displayName = $derived(agentNickname || msg.name || t('roleAssistant'))

 // 推理输出速度：输出 token 数 / 耗时，按 tok/s 显示；时间缺失或非法时显示 N/A
  function formatOutputSpeed(tokens, startAt, endAt) {
    if (tokens == null || !Number.isFinite(tokens)) return 'N/A'
    if (!startAt || !endAt) return 'N/A'
    const startMs = new Date(startAt).getTime()
    const endMs = new Date(endAt).getTime()
    if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs <= startMs) return 'N/A'
    const seconds = (endMs - startMs) / 1000
    return `${(tokens / seconds).toFixed(1)} tok/s`
  }

  function buildStatTooltip(s) {
    const fmtTokens = (n) => n >= 10000 ? `${(n / 1000).toFixed(1)}k` : `${n}`
    const fmtMs = (n) => n == null ? 'N/A' : n >= 10000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`

    const lines = []
    lines.push(`${t('tokenIn')} ${fmtTokens(s.prompt_tokens)}   ${t('tokenOut')} ${fmtTokens(s.completion_tokens)}   ${t('tokenTotal')} ${fmtTokens(s.total_tokens)}`)
    lines.push(`${t('tokenCached')} ${s.cached_input_tokens == null ? 'N/A' : fmtTokens(s.cached_input_tokens)}`)
    // 详细模式：本轮输出 token 数 / 模型请求实际开始到本轮推理输出完成。
    // completed_at 在后端执行工具前生成，因此该区间不包含后续工具时间。
    lines.push(`${t('statOutputSpeed')} ${formatOutputSpeed(s.completion_tokens, msg.started_at || s.request_started_at, s.completed_at || msg.timestamp)}`)
    if (s.total_prompt_tokens !== s.prompt_tokens || s.total_completion_tokens !== s.completion_tokens) {
      lines.push(`${t('tokenCumIn')} ${fmtTokens(s.total_prompt_tokens)}   ${t('tokenCumOut')} ${fmtTokens(s.total_completion_tokens)}   ${t('tokenCumTotal')} ${fmtTokens(s.total_all_tokens)}`)
    }
    if (s.ttft_ms != null) lines.push(`${t('statTtft')} ${fmtMs(s.ttft_ms)}`)
    if (s.net_ms != null)  lines.push(`${t('statNet')} ${fmtMs(s.net_ms)}`)
    if (s.total_ms != null) lines.push(`${t('statRound')} ${fmtMs(s.total_ms)}`)
    if (s.overall_ms != null) lines.push(`${t('statOverall')} ${fmtMs(s.overall_ms)}`)
    // Add this inference request's wall-clock timeline if available.
    const requestStartedTime = formatTimestamp(s.request_started_at)
    const firstTokenTime = formatTimestamp(s.first_token_timestamp)
    const completedTime = formatTimestamp(s.completed_at || msg.timestamp)
    if (requestStartedTime) lines.push(`${t('statRequestStartedTime')} ${requestStartedTime}`)
    if (firstTokenTime) lines.push(`${t('statFirstTokenTime')} ${firstTokenTime}`)
    if (completedTime) lines.push(`${t('statCompletedTime')} ${completedTime}`)
    return lines.join('\n')
  }

  // 工具结果：按实际显示内容计算行数，超过5行默认收缩，否则默认展开。
  // JSON 会先 pretty-print，因此即使原始 JSON 是单行，也能按格式化后的行数收缩。
  // talk_to 子消息渲染时用 sub_messages[] 各自的 content；传统 tool 消息用 msg.content。
  const toolContentSource = $derived(
    msg.sub_messages
      ? Object.values(msg.sub_messages).map(sm => sm.content || '').join('\n')
      : (msg.content ?? '')
  )
  const toolResultRender = $derived(msg.role === 'tool' && toolContentSource ? renderToolResult(toolContentSource) : { html: null, lang: null, displayContent: toolContentSource })
  const toolResultPreviewContent = $derived((toolResultRender.displayContent ?? '').split('\n').slice(0, 5).join('\n'))
  const toolResultPreviewHtml = $derived(toolResultRender.html ? highlight(toolResultPreviewContent, toolResultRender.lang) : null)
  const toolResultLines = $derived((toolResultRender.displayContent ?? '').split('\n').length)
  const toolResultOverLimit = $derived(msg.role === 'tool' && toolResultLines > 5)
  // 流式期间始终展开；结束后根据行数决定
  // 在 compact 模式下，工具结果默认折叠（只显示 ✔/✖）
  const isToolStreaming = $derived(msg.streaming === true)
  let toolResultExpanded = $state(false)
  let toolResultHoverExpanded = $state(false)
  let toolResultHoverTimer = null

  function startToolResultHover() {
    if (toolResultExpanded || toolResultHoverTimer) return
    toolResultHoverTimer = setTimeout(() => {
      toolResultHoverTimer = null
      toolResultHoverExpanded = true
    }, 1000)
  }

  function stopToolResultHover() {
    if (toolResultHoverTimer) clearTimeout(toolResultHoverTimer)
    toolResultHoverTimer = null
    toolResultHoverExpanded = false
  }

  const showToolResult = $derived(toolResultExpanded || toolResultHoverExpanded)

  $effect(() => {
    if (compact) {
      toolResultExpanded = false
    } else if (!isToolStreaming) {
      toolResultExpanded = !toolResultOverLimit
    }
  })

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

  // System messages default to collapsed (they're context, not conversational)
  let systemExpanded = $state(false)
  function toggleSystem() { systemExpanded = !systemExpanded }

  // 格式化时间戳为 MM/DD HH:mm:ss 格式
  function formatTimestamp(timestamp) {
    if (!timestamp) return ''
    try {
      const date = new Date(timestamp)
      if (isNaN(date.getTime())) return ''
      const month = String(date.getMonth() + 1).padStart(2, '0')
      const day = String(date.getDate()).padStart(2, '0')
      const hours = String(date.getHours()).padStart(2, '0')
      const minutes = String(date.getMinutes()).padStart(2, '0')
      const seconds = String(date.getSeconds()).padStart(2, '0')
      return `${month}/${day} ${hours}:${minutes}:${seconds}`
    } catch {
      return ''
    }
  }
</script>

<div class="message {msg.role}" class:compact data-user-message-index={scrollTargetIndex ?? undefined}>
{#if compact}
  <!-- ── Compact mode: lean display for reasoning-loop messages ── -->
  {#if msg.role === 'assistant'}
    {#if msg.thinking && typeof msg.thinking === 'string' && msg.thinking.trim().length > 0}
      <ThinkingBlock thinking={msg.thinking} expanded={thinkingExpanded} />
    {/if}
    {#if msg.content}
      <MarkdownRenderer content={msg.content} />
    {/if}
    {#if msg.tool_calls}
      <ToolCallCard toolCalls={msg.tool_calls} compact={true} {toolResultsById} />
    {/if}
  {:else if msg.role === 'tool'}
    {#if msg.sub_messages}
      <!-- talk_to sub-agent messages: same rendering as normal mode -->
      <div class="talk-to-subs">
        {#each Object.values(msg.sub_messages) as sm}
          <div class="talk-to-sub" class:streaming={sm.streaming}>
            {#if sm.streaming}
              <span class="talk-to-sub-badge">⏳</span>
            {/if}
            {#if sm.content}
              {@const prefix = '**' + (sm.agent_nickname || sm.agent_id) + '** (' + sm.agent_id + '): '}
              <MarkdownRenderer content={prefix + (sm.content || '')} />
            {/if}
          </div>
        {/each}
      </div>
    {:else}
    <!-- Tool result compact: show ✔/✖ click to expand -->
    <div class="compact-tool-result-wrap" role="group" onmouseenter={startToolResultHover} onmouseleave={stopToolResultHover}>
      <button class="compact-tool-result" title={toolResultExpanded ? t('collapseResult') : t('expandResult')} aria-label={toolResultExpanded ? t('collapseResult') : t('expandResult')} onclick={() => toolResultExpanded = !toolResultExpanded}>
        <span class="compact-result-icon">{toolResultIcon}</span>
      </button>
      {#if showToolResult}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="tool-result-block" ondblclick={() => toolResultExpanded = !toolResultExpanded}>
        {#if toolResultRender.html}
          <div class="code-block tool-result-code">
            {#if toolResultRender.lang}<span class="code-lang">{toolResultRender.lang.toUpperCase()}</span>{/if}
            <pre><code class="language-{toolResultRender.lang}">{@html toolResultRender.html}</code></pre>
          </div>
        {:else}
          <div class="tool-result-markdown"><MarkdownRenderer content={toolResultRender.displayContent} /></div>
        {/if}
        </div>
      {/if}
    </div>
    {/if}
  {:else}
    <!-- fallback for other roles in compact mode -->
    <div class="role-label">
      <span>{msg.role}</span>
    </div>
    {#if msg.content}
      <MarkdownRenderer content={msg.content} />
    {/if}
  {/if}
{:else}
  <!-- ── Normal (non-compact) display ── -->
  <div class="role-label">
    {#if msg.role === 'user'}
      <span>{t('roleUser')}</span>
      <div class="role-actions">
        {#if msg.timestamp}
          <span class="timestamp">{formatTimestamp(msg.timestamp)}</span>
        {/if}
        {#if onRevoke && msg.timestamp}
          <button class="revoke-btn" onclick={() => onRevoke(msg.timestamp, hasFileChanges)}>
            {t('revoke')}
          </button>
        {/if}
        {#if hasFileChanges}
          <button class="file-changes-btn" onclick={onToggleFileDiff}>
            {t('fileChanges')}
          </button>
        {/if}
        {#if collapseButton === 'expand'}
          <button class="toggle-btn" onclick={onCollapse}>
            {t('expandExecution')}
          </button>
        {/if}
        <CopyButton getText={() => msg.content ?? ''} />
      </div>
    {:else if msg.role === 'assistant'}
     {#if matchedAgent}
        <!-- 与紧凑模式（MessageList 的 agent-block-label）保持一致：size=18，图片固定 18x18，emoji 随字号 -->
        <IconDisplay value={matchedAgent.avatar || '🤖'} size={18} />
      {/if}
      <span class="agent-name">{displayName}</span>
      <div class="role-actions">
        {#if msg.stat}
          <span class="token-stats" title={buildStatTooltip(msg.stat)}>
            {msg.stat.prompt_tokens >= 10000 ? `${(msg.stat.prompt_tokens/1000).toFixed(1)}k` : msg.stat.prompt_tokens}/{msg.stat.completion_tokens >= 10000 ? `${(msg.stat.completion_tokens/1000).toFixed(1)}k` : msg.stat.completion_tokens} tokens
          </span>
        {/if}
        {#if showRetry && onRetry}
          <button class="retry-btn" onclick={onRetry} disabled={retryDisabled}>{t('retryInferenceRequest')}</button>
        {/if}
        {#if onToggleReplyMode && replyDetailed !== null}
          <button class="toggle-btn" onclick={onToggleReplyMode}>
            {replyDetailed ? t('compactReply') : t('detailedReply')}
          </button>
        {/if}
        {#if collapseButton === 'collapse' || collapseButton === 'expand'}
          <button class="toggle-btn" onclick={onCollapse}>
            {collapseButton === 'collapse' ? t('collapseExecution') : t('expandExecution')}
          </button>
        {/if}
        {#if msg.thinking && typeof msg.thinking === 'string' && msg.thinking.trim().length > 0}
          <button class="toggle-btn" onclick={toggleThinking}>
            {thinkingExpanded ? t('collapseThinking') : t('expandThinking')}
          </button>
        {/if}
        <CopyButton getText={() => {
          const parts = []
          if (msg.content) parts.push(msg.content)
          if (msg.tool_calls?.length) {
            for (const tc of msg.tool_calls) {
              const name = tc.name ?? t('unknownTool')
              const args = typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments, null, 2)
              parts.push(`[Tool Call: ${name}]\n${args}`)
            }
          }
          return parts.join('\n\n') || ''
        }} />
      </div>
    {:else if msg.role === 'system'}
      <span>{t('roleSystem')}</span>
      <div class="role-actions">
        {#if msg.timestamp}
          <span class="timestamp">{formatTimestamp(msg.timestamp)}</span>
        {/if}
        <button class="toggle-btn" onclick={toggleSystem}>
          {systemExpanded ? t('collapseExecution') : t('expandExecution')}
        </button>
        <CopyButton getText={() => msg.content ?? ''} />
      </div>
    {:else if msg.role === 'tool'}
      <span>{t('roleFunction')}{#if msg.name}: {msg.name}{/if}</span>
      <div class="role-actions">
        {#if msg.timestamp}
          <span class="timestamp">{formatTimestamp(msg.timestamp)}</span>
        {/if}
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

  {#if msg.thinking && typeof msg.thinking === 'string' && msg.thinking.trim().length > 0}
    <ThinkingBlock thinking={msg.thinking} expanded={thinkingExpanded} />
  {/if}

  {#if msg.sub_messages && msg.role === 'tool'}
    <!-- talk_to sub-agent messages: inline markdown matching historical
         "**nickname** (agent_id): content" format -->
    <div class="talk-to-subs">
      {#each Object.values(msg.sub_messages) as sm}
        <div class="talk-to-sub" class:streaming={sm.streaming}>
          {#if sm.streaming}
            <span class="talk-to-sub-badge">⏳</span>
          {/if}
          {#if sm.content}
            {@const prefix = '**' + (sm.agent_nickname || sm.agent_id) + '** (' + sm.agent_id + '): '}
            <MarkdownRenderer content={prefix + (sm.content || '')} />
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  {#if msg.content}
    {#if msg.role === 'assistant'}
      <MarkdownRenderer content={msg.content} />
    {:else if msg.role === 'system'}
      {#if systemExpanded}
        <MarkdownRenderer content={msg.content} />
      {/if}
    {:else if msg.role === 'tool'}
      {#if !msg.sub_messages}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="tool-result-block" ondblclick={() => toolResultExpanded = !toolResultExpanded}>
        {#if toolResultRender.html}
          {#if toolResultExpanded}
            <div class="code-block tool-result-code">
              {#if toolResultRender.lang}<span class="code-lang">{toolResultRender.lang.toUpperCase()}</span>{/if}
              <pre><code class="language-{toolResultRender.lang}">{@html toolResultRender.html}</code></pre>
            </div>
          {:else}
            <div class="code-block tool-result-code">
              {#if toolResultRender.lang}<span class="code-lang">{toolResultRender.lang.toUpperCase()}</span>{/if}
              <pre class="preview"><code class="language-{toolResultRender.lang}">{@html toolResultPreviewHtml}</code></pre>
            </div>
          {/if}
        {:else if toolResultExpanded}
          <div class="tool-result-markdown"><MarkdownRenderer content={toolResultRender.displayContent} /></div>
        {:else}
          <div class="tool-result-markdown preview-fade"><MarkdownRenderer content={toolResultPreviewContent} /></div>
        {/if}
      </div>
      {/if}
    {:else}
      <div class="content">
        {#each renderContentParts(msg.content) as part}
          {#if part.type === 'file'}
            <span class="file-ref-chip">{part.value}</span>
          {:else}
            {part.value}
          {/if}
        {/each}
      </div>
    {/if}
  {:else if msg.prompt_template}
    <div class="content template-ref">
      <span class="template-ref-id">{msg.prompt_template}</span>
      {#if msg.arguments && Object.keys(msg.arguments).length > 0}
        {#each Object.entries(msg.arguments) as [k, v]}
          <span class="template-ref-arg"><span class="arg-key">{k}</span><span class="arg-val"><MarkdownRenderer content={v} /></span></span>
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
{/if}
</div>

{#if msg.role === 'user' && fileDiffVisible && fileDiffData?.files}
  <FileDiffViewer files={fileDiffData.files} turnKey={fileDiffData.turn_key || ''} />
{/if}

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
  .message.assistant.compact {
    display: contents;
    background: none;
    border: none;
    padding: 0;
    max-width: 100%;
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
  .message.tool.compact {
    display: contents;
    background: none;
    border: none;
    padding: 0;
    width: auto;
    font-size: 0.85rem;
  }

  /* Compact assistant messages participate in one inline text flow. Ordinary
     paragraphs stay inline so a following tool badge/result can share the
     same line; explicit paragraph boundaries still produce a line break. */
  .message.compact :global(.markdown-content) {
    display: inline;
  }
  .message.compact :global(.markdown-content p) {
    display: inline;
    margin: 0;
  }
  .message.compact :global(.markdown-content p + p)::before {
    content: '\A';
    white-space: pre;
  }
  .message.compact :global(.markdown-content ul),
  .message.compact :global(.markdown-content ol),
  .message.compact :global(.markdown-content blockquote),
  .message.compact :global(.markdown-content table),
  .message.compact :global(.markdown-content .code-block) {
    display: block;
  }
  .role-label {
    display: flex;
    align-items: center;
    gap: 4px;
    justify-content: space-between;
    font-size: 0.75rem;
    font-weight: 600;
    margin-bottom: 4px;
    opacity: 0.8;
  }
  .agent-name {
    margin-right: auto;
    font-size: 0.9rem;
  }
 .role-actions {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .timestamp {
    font-size: 0.75rem;
    color: rgba(255, 255, 255, 0.6);
    white-space: nowrap;
    margin-right: 4px;
  }
  .message.tool .timestamp {
    color: var(--text-secondary, #888);
    opacity: 0.75;
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
  .retry-btn {
    padding: 2px 8px;
    font-family: inherit;
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
  .retry-btn:hover:not(:disabled) {
    background: var(--bg-secondary, rgba(0,0,0,0.2));
    color: var(--text, #333);
  }
  .retry-btn:active:not(:disabled) {
    background: var(--primary, #4a9eff);
    color: #fff;
  }
  .retry-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .content {
    white-space: pre-wrap;
    line-height: 1.5;
    font-size: 0.9rem;
  }
  .file-ref-chip {
    display: inline-flex;
    align-items: center;
    vertical-align: baseline;
    max-width: min(520px, 100%);
    margin: 0 2px;
    padding: 1px 7px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.18);
    border: 1px solid rgba(255, 255, 255, 0.35);
    color: inherit;
    font-size: 0.78rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    line-height: 1.6;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .file-ref-chip::before {
    content: '📎';
    margin-right: 4px;
    font-family: system-ui, sans-serif;
  }
  .message.assistant .file-ref-chip,
  .message.system .file-ref-chip,
  .message.tool .file-ref-chip {
    background: color-mix(in srgb, var(--primary) 14%, var(--bg-secondary));
    border-color: color-mix(in srgb, var(--primary) 35%, var(--border));
    color: var(--primary);
  }
  .template-ref { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px; white-space: normal; }
  .template-ref-id { font-family: monospace; font-weight: 600; font-size: 0.9rem; }
  .template-ref-arg { display: flex; align-items: flex-start; gap: 2px; font-size: 0.82rem; background: rgba(255,255,255,0.15); border-radius: 4px; padding: 1px 6px; }
  .arg-key { opacity: 0.75; flex-shrink: 0; }
  .arg-key::after { content: ':'; margin-right: 3px; }
  .arg-val { font-weight: 500; min-width: 0; }
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
  .reply-mode-btn,
  .revoke-btn {
    padding: 2px 8px;
    font-size: 0.75rem;
    color: rgba(255,255,255,0.7);
    background: rgba(255,255,255,0.15);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .reply-mode-btn:hover {
    color: #fff;
    background: rgba(255,255,255,0.25);
  }
  .revoke-btn:hover {
    color: #fff;
    background: rgba(239, 68, 68, 0.6);
  }
  .message.user .revoke-btn {
    color: rgba(255,255,255,0.7);
    background: rgba(255,255,255,0.15);
  }
  .message.user .revoke-btn:hover {
    color: #fff;
    background: rgba(239, 68, 68, 0.6);
  }
  .file-changes-btn {
    padding: 2px 8px;
    font-size: 0.75rem;
    color: rgba(255,255,255,0.7);
    background: rgba(255,255,255,0.15);
    border: none;
    border-radius: 4px;
    cursor: pointer;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .file-changes-btn:hover {
    color: #fff;
    background: rgba(59, 130, 246, 0.5);
  }
  .tool-result-block {
    margin-top: 4px;
    cursor: pointer;
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

  /* Compact mode tool result */
  .compact-tool-result-wrap {
    display: inline;
  }
  .compact-tool-result {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 1px 8px;
    font-size: 0.78rem;
    background: var(--bg-tertiary, rgba(0,0,0,0.08));
    border: 1px solid transparent;
    border-radius: 12px;
    cursor: pointer;
    color: var(--text-secondary, #888);
    transition: background 0.15s, border-color 0.15s;
    line-height: 1.5;
    font-family: inherit;
    margin: 2px 4px 2px 0;
  }
  .compact-tool-result:hover {
    background: var(--bg-secondary, rgba(0,0,0,0.12));
    border-color: var(--border, rgba(128,128,128,0.3));
    color: var(--text, #333);
  }
  .compact-result-icon {
    font-size: 0.8rem;
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

  /* talk_to sub-agent messages — inline markdown, visually matching
     the historical "**nickname** (agent_id): content" format */
  .talk-to-subs {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 4px;
  }
  .talk-to-sub {
    /* no border / background — same look as a regular markdown paragraph */
    line-height: 1.5;
  }
  .talk-to-sub.streaming {
    /* subtle left-border pulse to hint "still arriving" */
    border-left: 2px solid var(--primary, #7c3aed);
    padding-left: 8px;
  }
  .talk-to-sub-badge {
    display: inline-block;
    vertical-align: middle;
    font-size: 0.65rem;
    padding: 1px 6px;
    margin-right: 4px;
    border-radius: 999px;
    background: var(--primary, #7c3aed);
    color: #fff;
    animation: talk-to-pulse 1.5s ease-in-out infinite;
  }
  @keyframes talk-to-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
</style>
