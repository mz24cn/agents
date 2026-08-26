<script>
  import { t } from '../../lib/i18n.svelte.js'

  let { files = [], turnKey = '' } = $props()

  // Track which files are expanded
  let expandedFiles = $state({})

  // View mode: 'unified' | 'split', persisted to localStorage
  const VIEW_MODE_KEY = 'coco:diffViewMode'
  let viewMode = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem(VIEW_MODE_KEY)) || 'unified'
  )

  function toggleViewMode() {
    viewMode = viewMode === 'unified' ? 'split' : 'unified'
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(VIEW_MODE_KEY, viewMode)
    }
  }

  function toggleFile(path) {
    expandedFiles = { ...expandedFiles, [path]: !expandedFiles[path] }
  }

  function collapseAll() {
    expandedFiles = {}
  }

  function expandAll() {
    const all = {}
    for (const f of files) all[f.path] = true
    expandedFiles = all
  }

  // Parse diff lines into segments
  function parseDiffHunks(diffText) {
    if (!diffText) return []
    const lines = diffText.split('\n')
    // Drop trailing empty line from split if present
    if (lines.length > 0 && lines[lines.length - 1] === '') {
      lines.pop()
    }
    const hunks = []
    let currentHunk = null

    for (const line of lines) {
      if (line.startsWith('@@')) {
        if (currentHunk) hunks.push(currentHunk)
        // Extract line numbers from hunk header
        const match = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/)
        const oldStart = match ? parseInt(match[1]) : 0
        const newStart = match ? parseInt(match[3]) : 0
        currentHunk = {
          header: line,
          oldStart,
          newStart,
          oldCount: match && match[2] ? parseInt(match[2]) : 1,
          newCount: match && match[4] ? parseInt(match[4]) : 1,
          context: match ? match[5] || '' : '',
          rows: [],
          // running counters during row iteration
          _oldLine: oldStart,
          _newLine: newStart,
        }
      } else if (currentHunk) {
        let type, oldNum, newNum
        if (line.startsWith('+')) {
          type = 'add'
          oldNum = null
          newNum = currentHunk._newLine++
        } else if (line.startsWith('-')) {
          type = 'del'
          oldNum = currentHunk._oldLine++
          newNum = null
        } else {
          type = 'ctx'
          oldNum = currentHunk._oldLine++
          newNum = currentHunk._newLine++
        }
        currentHunk.rows.push({ type, text: line, oldNum, newNum })
      }
    }
    if (currentHunk) hunks.push(currentHunk)
    return hunks
  }

  // Convert unified rows to split (side-by-side) row pairs
  // Each split row: { left: row|null, right: row|null }
  function buildSplitRows(rows) {
    const result = []
    let i = 0
    while (i < rows.length) {
      const row = rows[i]
      // When del is followed by add, pair them as a modification
      if (row.type === 'del' && i + 1 < rows.length && rows[i + 1].type === 'add') {
        result.push({ left: row, right: rows[i + 1] })
        i += 2
      } else if (row.type === 'del') {
        result.push({ left: row, right: null })
        i += 1
      } else if (row.type === 'add') {
        result.push({ left: null, right: row })
        i += 1
      } else {
        // ctx — shows on both sides
        result.push({ left: row, right: row })
        i += 1
      }
    }
    return result
  }

  function getChangeIcon(changeType) {
    if (changeType === 'added') return '+'
    if (changeType === 'deleted') return '-'
    return '~'
  }

  function getChangeLabel(changeType) {
    if (changeType === 'added') return t('fileAdded') || 'added'
    if (changeType === 'deleted') return t('fileDeleted') || 'deleted'
    return t('fileModified') || 'modified'
  }

  function formatModified(value) {
    if (value == null) return '\u2014'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return '\u2014'
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    return `${year}/${month}/${day} ${hours}:${minutes}:${seconds}`
  }

  function formatSize(value) {
    const size = Number(value)
    return Number.isFinite(size) && size >= 0
      ? `${Math.trunc(size).toLocaleString('en-US')} B`
      : '\u2014'
  }

  let allExpanded = $derived(Object.keys(expandedFiles).length === 0 ? false :
    files.length > 0 && files.every(f => expandedFiles[f.path]))
</script>

<div class="file-diff-viewer">
  <div class="diff-header">
    <span class="diff-title">{t('fileChanges')} ({files.length})</span>
    <div class="diff-header-actions">
      <button class="toggle-all-btn" onclick={toggleViewMode} title={viewMode === 'unified' ? (t('splitView') || 'Split view') : (t('unifiedView') || 'Unified view')}>
        {viewMode === 'unified' ? '\u2261' : '\u25d6'}
      </button>
      <button class="toggle-all-btn" onclick={expandAll}>{t('expandAll')}</button>
      <button class="toggle-all-btn" onclick={collapseAll}>{t('collapseAll')}</button>
    </div>
  </div>

  <div class="file-list">
    {#each files as file (file.path)}
      <div class="file-item" class:expanded={expandedFiles[file.path]}>
        <button class="file-header" onclick={() => toggleFile(file.path)}>
          <span class="file-chevron">{expandedFiles[file.path] ? '\u25be' : '\u25b8'}</span>
          <span class="file-path" title={file.path}>{file.path}</span>
          <span class="file-modified">{formatModified(file.modified)}</span>
          <span class="file-size">{formatSize(file.size)}</span>
          <span class="file-change-label {file.change_type}">{getChangeLabel(file.change_type)}</span>
        </button>

        {#if expandedFiles[file.path]}
          <div class="file-diff-content">
            {#if !file.diff}
              <div class="diff-empty">{t('noChanges') || 'No changes'}</div>
            {:else if viewMode === 'unified'}
              <!-- ===== UNIFIED VIEW ===== -->
              <div class="diff-view">
                {#each parseDiffHunks(file.diff) as hunk (hunk.header)}
                  <div class="diff-hunk">
                    <div class="hunk-header">{hunk.header}</div>
                    <div class="hunk-rows">
                      {#each hunk.rows as row}
                        <div class="diff-row {row.type}">
                          <span class="diff-ln diff-ln-old">{row.oldNum != null ? row.oldNum : ''}</span>
                          <span class="diff-ln diff-ln-new">{row.newNum != null ? row.newNum : ''}</span>
                          <span class="diff-marker">{row.type === 'add' ? '+' : row.type === 'del' ? '-' : ' '}</span>
                          <span class="diff-text">{row.text.slice(1) || ''}</span>
                        </div>
                      {/each}
                    </div>
                  </div>
                {/each}
              </div>
            {:else}
              <!-- ===== SPLIT VIEW ===== -->
              <div class="diff-view split-view">
                {#each parseDiffHunks(file.diff) as hunk (hunk.header)}
                  <div class="diff-hunk">
                    <div class="hunk-header">{hunk.header}</div>
                    <div class="split-pane-header">
                      <span class="split-pane-label">OLD</span>
                      <span class="split-pane-label">NEW</span>
                    </div>
                    <div class="hunk-rows">
                      {#each buildSplitRows(hunk.rows) as pair}
                        {@const leftType = pair.left?.type ?? 'empty'}
                        {@const rightType = pair.right?.type ?? 'empty'}
                        <div class="split-row">
                          <!-- LEFT SIDE -->
                          <div class="split-side split-left {leftType}">
                            {#if pair.left}
                              <span class="diff-ln">{pair.left.oldNum != null ? pair.left.oldNum : ''}</span>
                              <span class="diff-marker">{pair.left.type === 'del' ? '-' : ' '}</span>
                              <span class="diff-text">{pair.left.text.slice(1) || ''}</span>
                            {:else}
                              <span class="diff-ln"></span>
                              <span class="diff-marker"></span>
                              <span class="diff-text"></span>
                            {/if}
                          </div>
                          <!-- RIGHT SIDE -->
                          <div class="split-side split-right {rightType}">
                            {#if pair.right}
                              <span class="diff-ln">{pair.right.newNum != null ? pair.right.newNum : ''}</span>
                              <span class="diff-marker">{pair.right.type === 'add' ? '+' : ' '}</span>
                              <span class="diff-text">{pair.right.text.slice(1) || ''}</span>
                            {:else}
                              <span class="diff-ln"></span>
                              <span class="diff-marker"></span>
                              <span class="diff-text"></span>
                            {/if}
                          </div>
                        </div>
                      {/each}
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
</div>

<style>
  .file-diff-viewer {
    margin-top: 8px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg-primary);
    overflow: hidden;
    font-size: 0.82rem;
  }

  .diff-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    background: var(--bg-tertiary, rgba(0,0,0,0.05));
    border-bottom: 1px solid var(--border);
  }

  .diff-title {
    font-weight: 600;
    font-size: 0.85rem;
  }

  .diff-header-actions {
    display: flex;
    gap: 6px;
  }

  .toggle-all-btn {
    padding: 2px 10px;
    font-size: 0.75rem;
    color: var(--text-secondary);
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
  }

  .toggle-all-btn:hover {
    color: var(--text);
    border-color: var(--primary);
  }

  .file-list {
    max-height: 480px;
    overflow-y: auto;
  }

  .file-item {
    border-bottom: 1px solid var(--border);
  }

  .file-item:last-child {
    border-bottom: none;
  }

  .file-header {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    padding: 6px 12px;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.82rem;
    text-align: left;
    color: var(--text);
    transition: background 0.1s;
  }

  .file-header:hover {
    background: var(--bg-tertiary, rgba(0,0,0,0.03));
  }

  .file-chevron {
    font-size: 0.7rem;
    width: 14px;
    flex-shrink: 0;
    color: var(--text-secondary);
  }

  .file-path {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.8rem;
    flex: 1 1 auto;
    min-width: 80px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-modified,
  .file-size {
    color: var(--text-secondary);
    font-size: 0.74rem;
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .file-modified {
    width: 142px;
  }

  .file-size {
    width: 76px;
  }

  .file-change-label {
    width: 48px;
    box-sizing: border-box;
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 10px;
    text-align: center;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .file-change-label.added {
    color: #16a34a;
    background: color-mix(in srgb, #16a34a 15%, transparent);
  }
  .file-change-label.deleted {
    color: #dc2626;
    background: color-mix(in srgb, #dc2626 15%, transparent);
  }
  .file-change-label.modified {
    color: #d97706;
    background: color-mix(in srgb, #d97706 15%, transparent);
  }

  .file-diff-content {
    background: var(--bg-secondary);
  }

  .diff-empty {
    padding: 12px;
    color: var(--text-secondary);
    font-style: italic;
  }

  .diff-view {
    overflow-x: auto;
  }

  .diff-hunk {
    /* no extra wrapper needed */
  }

  .hunk-header {
    padding: 4px 12px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.72rem;
    color: var(--text-secondary);
    background: color-mix(in srgb, var(--primary) 8%, var(--bg-tertiary, #f0f0f0));
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }

  .hunk-rows {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.78rem;
    line-height: 1.5;
  }

  /* ===== UNIFIED ROW ===== */

  .diff-row {
    display: flex;
    min-height: 1.5em;
  }

  .diff-ln {
    width: 44px;
    min-width: 44px;
    text-align: right;
    padding: 0 6px;
    user-select: none;
    color: var(--text-tertiary, #88929e);
    font-size: 0.72rem;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    white-space: nowrap;
    overflow: hidden;
  }

  .diff-row.add {
    background: color-mix(in srgb, #16a34a 12%, transparent);
  }

  .diff-row.del {
    background: color-mix(in srgb, #dc2626 12%, transparent);
  }

  .diff-row.ctx {
    background: transparent;
  }

  .diff-marker {
    width: 20px;
    min-width: 20px;
    text-align: center;
    padding: 0 4px;
    user-select: none;
    flex-shrink: 0;
  }

  .diff-row.add .diff-marker {
    color: #16a34a;
  }

  .diff-row.del .diff-marker {
    color: #dc2626;
  }

  .diff-row.ctx .diff-marker {
    color: var(--text-secondary);
  }

  .diff-text {
    white-space: pre-wrap;
    word-break: break-all;
    padding-right: 8px;
  }

  /* ===== SPLIT VIEW ===== */

  .split-pane-header {
    display: flex;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.66rem;
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-tertiary, rgba(0,0,0,0.03));
    border-bottom: 1px solid var(--border);
  }

  .split-pane-label {
    flex: 1;
    padding: 3px 12px;
  }

  .split-pane-label + .split-pane-label {
    border-left: 1px solid var(--border);
  }

  .split-row {
    display: flex;
    min-height: 1.5em;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  }

  .split-row:last-child {
    border-bottom: none;
  }

  .split-side {
    flex: 1;
    min-width: 0;
    display: flex;
  }

  .split-left {
    border-right: 1px solid var(--border);
  }

  .split-side .diff-ln {
    border-right: 1px solid var(--border);
  }

  .split-side .diff-text {
    white-space: pre-wrap;
    word-break: break-all;
    padding-right: 4px;
  }

  .split-side .diff-marker {
    text-align: center;
  }

  /* Split side backgrounds */
  .split-side.del {
    background: color-mix(in srgb, #dc2626 10%, transparent);
  }
  .split-side.del .diff-marker {
    color: #dc2626;
  }

  .split-side.add {
    background: color-mix(in srgb, #16a34a 10%, transparent);
  }
  .split-side.add .diff-marker {
    color: #16a34a;
  }

  .split-side.ctx {
    background: transparent;
  }
  .split-side.ctx .diff-marker {
    color: var(--text-secondary);
  }

  .split-side.empty {
    background: var(--bg-tertiary, rgba(0,0,0,0.02));
  }
</style>
