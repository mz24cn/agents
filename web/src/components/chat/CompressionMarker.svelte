<script>
  // Context-compression marker: one attempt, rendered the way a tool call is.
  //
  // A compression is invisible work inside a turn -- it pauses the inference for
  // seconds, produces no token, and rewrites the summary the next request will
  // be given.  Showing it as a capsule pair (subject + outcome, click for the
  // details) puts it where it happened: inside the assistant message it made
  // room for, next to the tool calls of that same turn.
  //
  // The entries come from either feed and look identical: the live SSE pair
  // (ChatPage attaches them to the message) or conversation.json's own audit
  // trail (messages[i].meta.compressions), which is what keeps the marker here
  // after the session is reopened and no stream is involved.
  import { t } from '../../lib/i18n.svelte.js'
  import {
    compressionFailed,
    compressionDetailLines,
    compressionIcon,
    compressionIsPending,
    compressionLabel,
    compressionResultIcon,
    compressionStats,
    compressionTooltip,
  } from '../../lib/compression-status.js'

  let { entries = [], compact = false } = $props()

  let expandedIndexes = $state(new Set())

  function toggle(index) {
    const next = new Set(expandedIndexes)
    if (next.has(index)) next.delete(index)
    else next.add(index)
    expandedIndexes = next
  }
</script>

{#if entries.length}
  {#if compact}
    {#each entries as entry, index}
      {@const pending = compressionIsPending(entry)}
      {@const failed = compressionFailed(entry)}
      {@const stats = compressionStats(entry, t)}
      {@const tooltip = compressionTooltip(entry, t)}
      <span class="compression-pair" role="group">
        <span class="compression-call-zone" role="group">
          <button
            class="compression-call has-result"
            class:pending={pending}
            aria-expanded={expandedIndexes.has(index)}
            title={tooltip}
            onclick={() => toggle(index)}
          >
            <span class="compression-icon" aria-hidden="true">{compressionIcon()}</span>
            <span class="compression-name">{compressionLabel(entry, t)}</span>
            {#if stats}<span class="compression-stats">{stats}</span>{/if}
          </button>
          {#if expandedIndexes.has(index)}
            <span class="compression-detail" role="group">
              <pre>{compressionDetailLines(entry).join('\n')}</pre>
            </span>
          {/if}
        </span>
        <span class="compression-result-zone" role="group">
          <button
            class="compression-result"
            class:pending={pending}
            class:failed={failed}
            aria-expanded={expandedIndexes.has(index)}
            title={tooltip}
            onclick={() => toggle(index)}
          >{compressionResultIcon(entry)}</button>
        </span>
      </span>
    {/each}
  {:else}
    {#each entries as entry, index}
      {@const pending = compressionIsPending(entry)}
      {@const failed = compressionFailed(entry)}
      <div class="compression-block">
        <button
          class="compression-head"
          class:pending={pending}
          class:failed={failed}
          aria-expanded={expandedIndexes.has(index)}
          title={compressionTooltip(entry, t)}
          onclick={() => toggle(index)}
        >
          <span class="compression-icon" aria-hidden="true">{compressionIcon()}</span>
          <span>{compressionLabel(entry, t)}</span>
          {#if compressionStats(entry, t)}<span class="compression-stats">{compressionStats(entry, t)}</span>{/if}
          <span class="compression-result-icon" class:failed={failed} aria-hidden="true">{compressionResultIcon(entry)}</span>
        </button>
        {#if expandedIndexes.has(index)}
          <pre>{compressionDetailLines(entry).join('\n')}</pre>
        {/if}
      </div>
    {/each}
  {/if}
{/if}

<style>
  /* Same visual language as a compact tool call: a capsule for the work, a
     second half for its outcome, both opening the same detail block. */
  .compression-pair {
    /* Geometry copied from .compact-tool-pair: the marker has to sit on the
       same line as the tool capsules of its turn, so it uses their box model
       (inline, same 6px nudge, same baseline) instead of its own.  As an
       inline-block it floated above the text baseline and read as too tall. */
    display: inline;
    position: relative;
    top: 6px;
    margin: 0 4px 0 0;
    vertical-align: baseline;
    /* Remove the HTML inline whitespace between the two clickable halves. */
    font-size: 0;
  }
  .compression-call-zone,
  .compression-result-zone {
    display: inline;
    vertical-align: top;
  }
  .compression-call,
  .compression-result {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-sizing: border-box;
    height: 1.3em;
    padding: 0 7px;
    vertical-align: top;
    font-family: inherit;
    font-size: 0.78rem;
    line-height: 1;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0, 0, 0, 0.08));
    border: 1px solid transparent;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .compression-call {
    gap: 5px;
    border-radius: 12px 0 0 12px;
    border-right: none;
  }
  .compression-call.has-result {
    padding-right: 3px;
  }
  .compression-result {
    padding-left: 2px;
    padding-right: 6px;
    border-radius: 0 12px 12px 0;
    border-left: none;
  }
  .compression-call:hover,
  .compression-result:hover {
    color: var(--text, #333);
    background: var(--bg-secondary, rgba(0, 0, 0, 0.12));
    border-color: var(--border, rgba(128, 128, 128, 0.3));
  }
  .compression-call.has-result:hover {
    border-right: none;
  }
  .compression-result:hover {
    border-left: none;
  }
  .compression-call.pending,
  .compression-result.pending {
    animation: compression-pending-pulse 1.2s ease-in-out infinite;
  }
  .compression-result.failed {
    color: var(--danger, #d33);
  }
  @keyframes compression-pending-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.45; }
  }
  .compression-icon {
    display: inline-flex;
    align-items: center;
    font-size: 0.85rem;
    line-height: 1;
  }
  .compression-name { font-weight: 500; }
  .compression-stats { color: var(--text-secondary, #888); }
  .compression-detail,
  .compression-block pre {
    display: block;
    box-sizing: border-box;
    width: 100%;
    margin: 4px 0 6px;
    padding: 6px 8px;
    font-size: 0.8rem;
    line-height: 1.5;
    white-space: normal;
    background: rgba(0, 0, 0, 0.05);
    border-radius: 4px;
  }
  .compression-detail::before { content: ''; display: block; }
  .compression-detail::after { content: ''; display: block; clear: both; }
  .compression-detail pre,
  .compression-block pre {
    margin: 4px 0 0;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* Detailed (non-compact) mode: same content as the "Calling tool" block. */
  .compression-block {
    font-size: 0.8rem;
    margin-top: 6px;
    padding: 6px 8px;
    background: rgba(0, 0, 0, 0.05);
    border-radius: 4px;
  }
  .compression-head {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 0;
    border: none;
    background: none;
    font: inherit;
    color: inherit;
    cursor: pointer;
  }
  /* The outcome glyph, same ✔️/✖️/⏳ as a tool result, and the whole line opens
     the full list. */
  .compression-result-icon {
    font-size: 0.85rem;
    line-height: 1;
  }
  .compression-result-icon.failed {
    color: var(--danger, #d33);
  }
  .compression-head.pending .compression-result-icon {
    animation: compression-pending-pulse 1.2s ease-in-out infinite;
  }
</style>
