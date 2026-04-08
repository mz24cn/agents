<script>
  import { highlight } from '../lib/highlight.js'

  let { value = $bindable(''), rows = 8, placeholder = '', id = '', disabled = false } = $props()

  let textareaEl = $state(null)
  let scrollTop = $state(0)
  let scrollLeft = $state(0)

  // JSON validity state
  let isValid = $derived.by(() => {
    if (!value.trim()) return null // empty = neutral
    try { JSON.parse(value); return true } catch { return false }
  })

  let highlighted = $derived(value ? highlight(value, 'json') : '')

  function onScroll(e) {
    scrollTop = e.target.scrollTop
    scrollLeft = e.target.scrollLeft
  }
</script>

<div class="json-editor" class:valid={isValid === true} class:invalid={isValid === false}>
  <pre
    class="json-highlight"
    aria-hidden="true"
    style="transform: translate({-scrollLeft}px, {-scrollTop}px)"
  ><code>{@html highlighted || ''}<br /></code></pre>
  <textarea
    {id}
    {disabled}
    {placeholder}
    {rows}
    bind:value
    bind:this={textareaEl}
    onscroll={onScroll}
    spellcheck="false"
    autocomplete="off"
    autocorrect="off"
    autocapitalize="off"
  ></textarea>
  {#if isValid === false}
    <span class="json-status invalid-icon" title="JSON invalid">✕</span>
  {:else if isValid === true}
    <span class="json-status valid-icon" title="JSON valid">✓</span>
  {/if}
</div>

<style>
  .json-editor {
    position: relative;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
    overflow: hidden;
    transition: border-color 0.15s;
  }
  .json-editor.valid   { border-color: var(--success); }
  .json-editor.invalid { border-color: var(--danger); }

  .json-highlight {
    position: absolute;
    top: 0; left: 0;
    margin: 0;
    padding: 8px 10px;
    width: 100%;
    pointer-events: none;
    white-space: pre-wrap;
    word-break: break-all;
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.9rem;
    line-height: 1.5;
    color: var(--text); /* punctuation & unmatched chars use default text color */
    overflow: hidden;
    box-sizing: border-box;
  }

  .json-highlight code {
    font-family: inherit;
    font-size: inherit;
    line-height: inherit;
    background: none;
    padding: 0;
  }

  /* Syntax colors - dark theme */
  .json-highlight :global(.hl-key)     { color: #82aaff; }
  .json-highlight :global(.hl-string)  { color: #c3e88d; }
  .json-highlight :global(.hl-number)  { color: #f78c6c; }
  .json-highlight :global(.hl-boolean) { color: #ff5874; }
  .json-highlight :global(.hl-null)    { color: #ff5874; }

  /* Syntax colors - light theme */
  :root[data-theme="light"] .json-highlight :global(.hl-key)     { color: #1d4ed8; }
  :root[data-theme="light"] .json-highlight :global(.hl-string)  { color: #16a34a; }
  :root[data-theme="light"] .json-highlight :global(.hl-number)  { color: #c2410c; }
  :root[data-theme="light"] .json-highlight :global(.hl-boolean) { color: #dc2626; }
  :root[data-theme="light"] .json-highlight :global(.hl-null)    { color: #dc2626; }

  textarea {
    position: relative;
    display: block;
    width: 100%;
    padding: 8px 10px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: transparent;
    caret-color: var(--text);
    font-family: 'Fira Code', 'Consolas', monospace;
    font-size: 0.9rem;
    line-height: 1.5;
    resize: vertical;
    outline: none;
    box-sizing: border-box;
    white-space: pre-wrap;
    word-break: break-all;
    overflow: auto;
  }
  textarea::placeholder { color: var(--text-secondary); opacity: 0.6; }
  textarea:disabled { opacity: 0.6; cursor: not-allowed; }

  .json-status {
    position: absolute;
    bottom: 4px;
    right: 6px;
    font-size: 0.75rem;
    font-weight: bold;
    pointer-events: none;
    line-height: 1;
  }
  .valid-icon   { color: var(--success); }
  .invalid-icon { color: var(--danger); }
</style>
