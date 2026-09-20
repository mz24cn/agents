<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { highlight } from '../../lib/highlight.js'
  import { isToolErrorContent } from '../../lib/tool-result.js'
  import { tools } from '../../lib/api.js'
  import { buildCallTestSkeleton } from '../../lib/tool-call-skeleton.js'
  import JsonEditor from '../JsonEditor.svelte'
  import CopyButton from '../chat/CopyButton.svelte'

  let { tool, onCancel } = $props()

  let argsJson = $state(buildCallTestSkeleton(tool))
  let running = $state(false)
  // base64 自动编码/解码：与模型调用工具一致（路径->base64 入参，长 base64 结果->本地文件路径）
  let base64Auto = $state(false)
  // phase: 'idle'（未调用）| 'running'（调用中）| 'done'（完成，outcome 保存结果）
  let phase = $state('idle')
  // { ok, text?, error?, ms }
  let outcome = $state(null)

  let argsValue = $derived.by(() => {
    try {
      return JSON.parse(argsJson)
    } catch {
      return null
    }
  })
  let argsValid = $derived(argsValue !== null && typeof argsValue === 'object' && !Array.isArray(argsValue))

  // 结果展示：能解析成 JSON 就 pretty-print + 语法高亮，否则纯文本
  let renderedResult = $derived.by(() => {
    if (phase !== 'done' || outcome?.text == null) return null
    const content = String(outcome.text)
    try {
      const pretty = JSON.stringify(JSON.parse(content), null, 2)
      return { html: highlight(pretty, 'json'), text: pretty }
    } catch {
      return { html: null, text: content }
    }
  })

  async function submit() {
    if (running || !argsValid) return
    running = true
    phase = 'running'
    outcome = null
    const started = performance.now()
    try {
      const text = await tools.call(tool.tool_id, argsValue, { base64Auto })
      outcome = {
        ok: !isToolErrorContent(text),
        text,
        ms: Math.round(performance.now() - started),
      }
      phase = 'done'
    } catch (err) {
      outcome = {
        ok: false,
        error: err?.message || String(err),
        ms: Math.round(performance.now() - started),
      }
      phase = 'done'
    } finally {
      running = false
    }
  }
</script>

<div class="tool-call-test">
  <div class="form-header">
    <h3>{t('toolTestTitle')}</h3>
    <button type="button" class="btn btn-back" onclick={onCancel} disabled={running} title={t('cancel')}>&larr; {t('cancel')}</button>
  </div>

  <div class="meta">
    <span class="meta-id">{tool.tool_id}</span>
    {#if tool.tool_type === 'mcp' && tool.mcp_server_name}
      <span class="meta-route">{t('toolTestMcpRoute')}: {tool.mcp_server_name} → {tool.tool_name || tool.name}</span>
    {/if}
  </div>

  <div class="form-group">
    <div class="field-label-row">
      <label class="field-label">{t('toolTestArgs')}</label>
      <label class="b64-toggle" title={t('toolTestBase64AutoHint')}>
        <input type="checkbox" bind:checked={base64Auto} disabled={running} />
        {t('toolTestBase64Auto')}
      </label>
    </div>
    <JsonEditor bind:value={argsJson} autoResize maxHeightVh={40} disabled={running} />
    <span class="hint">{t('toolTestWorkspaceHint')}</span>
  </div>

  <div class="actions">
    <button class="btn-primary" onclick={submit} disabled={running || !argsValid}>
      {running ? t('toolTestRunning') : t('toolTestSubmit')}
    </button>
  </div>

  {#if phase === 'running'}
    <div class="running-status">⏳ {t('toolTestRunning')}</div>
  {:else if phase === 'done' && outcome}
    <div class="result" class:fail={!outcome.ok}>
      <div class="result-header">
        <span class="result-title">
          {#if outcome.ok}✅ {t('toolTestSuccess')}{:else}❌ {t('toolTestFailed')}{/if}
          <span class="result-ms">({outcome.ms} ms)</span>
        </span>
        {#if outcome.text != null}
          <CopyButton getText={() => outcome.text} />
        {/if}
      </div>
      <div class="result-body">
        {#if outcome.error}
          <pre class="result-error"><code>{outcome.error}</code></pre>
        {:else if renderedResult?.html}
          <pre><code>{@html renderedResult.html}</code></pre>
        {:else}
          <pre><code>{renderedResult?.text ?? ''}</code></pre>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .tool-call-test { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 20px; }
  .form-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .form-header h3 { margin: 0; color: var(--text); }
  .btn-back { background: transparent; color: var(--text-secondary); border: 1px solid var(--border); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; transition: all 0.15s; }
  .btn-back:hover:not(:disabled) { background: var(--border); color: var(--text); }
  .btn-back:disabled { opacity: 0.5; cursor: not-allowed; }
  .meta { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 16px; }
  .meta-id { font-family: 'Fira Code', 'Consolas', monospace; font-weight: 600; color: var(--text); }
  .form-group { margin-bottom: 14px; display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); }
  .field-label-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .b64-toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 0.8rem; color: var(--text-secondary); cursor: pointer; user-select: none; white-space: nowrap; }
  .b64-toggle input { margin: 0; cursor: pointer; accent-color: var(--primary, #4a9eff); }
  .b64-toggle:has(input:disabled) { opacity: 0.5; cursor: not-allowed; }
  .hint { color: var(--text-secondary); font-size: 0.8rem; margin-top: 4px; }
  .actions { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .btn-primary { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; background: var(--primary, #4a9eff); color: #fff; }
  .btn-primary:hover:not(:disabled) { opacity: 0.9; }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .running-status { font-size: 0.9rem; color: var(--text-secondary); padding: 12px 0; }
  .result { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .result:not(.fail) { border-color: var(--success, #16a34a); }
  .result.fail { border-color: var(--danger); }
  .result-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 8px 12px; background: var(--bg-secondary); font-size: 0.85rem; }
  .result-title { font-weight: 600; }
  .result:not(.fail) .result-title { color: var(--success, #16a34a); }
  .result.fail .result-title { color: var(--danger); }
  .result-ms { color: var(--text-secondary); font-weight: 400; margin-left: 6px; }
  .result-body { max-height: 60vh; overflow: auto; }
  .result-body pre { margin: 0; padding: 12px; font-size: 0.82rem; line-height: 1.5; white-space: pre-wrap; word-break: break-all; }
  .result-body code { font-family: 'Fira Code', 'Consolas', monospace; }
  .result-error code { color: var(--danger); }
  /* Syntax colors - dark theme */
  .result-body :global(.hl-key)     { color: #82aaff; }
  .result-body :global(.hl-string)  { color: #c3e88d; }
  .result-body :global(.hl-number)  { color: #f78c6c; }
  .result-body :global(.hl-boolean) { color: #ff5874; }
  .result-body :global(.hl-null)    { color: #ff5874; }
  /* Syntax colors - light theme */
  :root[data-theme="light"] .result-body :global(.hl-key)     { color: #1d4ed8; }
  :root[data-theme="light"] .result-body :global(.hl-string)  { color: #16a34a; }
  :root[data-theme="light"] .result-body :global(.hl-number)  { color: #c2410c; }
  :root[data-theme="light"] .result-body :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .result-body :global(.hl-null)    { color: #dc2626; }
</style>
