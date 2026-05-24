/**
 * Lightweight syntax highlighting module using regex rules.
 * No third-party runtime dependencies.
 */

const PYTHON_RULES = [
  // Triple-quoted strings (highest priority)
  { pattern: /"""[\s\S]*?"""|'''[\s\S]*?'''/g, className: 'hl-string' },
  // Single-line strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /#[^\n]*/g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(def|class|if|else|elif|for|while|return|import|from|with|as|try|except|finally|pass|break|continue|True|False|None)\b/g,
    className: 'hl-keyword',
  },
  // Numbers
  { pattern: /\b\d+(\.\d+)?\b/g, className: 'hl-number' },
]

const JSON_RULES = [
  // Key names ("key":)
  { pattern: /"(?:[^"\\]|\\.)*"(?=\s*:)/g, className: 'hl-key' },
  // String values
  { pattern: /"(?:[^"\\]|\\.)*"/g, className: 'hl-string' },
  // Numbers
  { pattern: /-?\b\d+(\.\d+)?([eE][+-]?\d+)?\b/g, className: 'hl-number' },
  // Booleans
  { pattern: /\b(true|false)\b/g, className: 'hl-boolean' },
  // null
  { pattern: /\bnull\b/g, className: 'hl-null' },
]

const BASH_RULES = [
  // Single-quoted strings
  { pattern: /'[^']*'/g, className: 'hl-string' },
  // Double-quoted strings
  { pattern: /"(?:[^"\\]|\\.)*"/g, className: 'hl-string' },
  // Comments
  { pattern: /#[^\n]*/g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(if|then|fi|else|elif|for|while|do|done|case|esac|function|return|export|local)\b/g,
    className: 'hl-keyword',
  },
  // Variable references: ${var} and $var
  { pattern: /\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*/g, className: 'hl-variable' },
]

const RULES = {
  python: PYTHON_RULES,
  json: JSON_RULES,
  bash: BASH_RULES,
  sh: BASH_RULES,
}

const JSON_LONG_STRING_FALLBACK_LIMIT = 90
const JSON_PREVIEW_RESERVED_CHARS = 28
const JSON_PREVIEW_MIN_CHARS = 24

/**
 * Escapes HTML special characters to prevent XSS.
 * @param {string} code
 * @returns {string}
 */
export function escapeHtml(code) {
  return code
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function findPreviousNonWhitespaceChar(code, index) {
  for (let i = index - 1; i >= 0; i--) {
    if (!/\s/.test(code[i])) return code[i]
  }
  return ''
}

function findNextNonWhitespaceChar(code, index) {
  for (let i = index; i < code.length; i++) {
    if (!/\s/.test(code[i])) return code[i]
  }
  return ''
}

function readInlineTrailingComma(code, index) {
  let i = index
  while (code[i] === ' ' || code[i] === '\t') i++
  return code[i] === ',' ? code.slice(index, i + 1) : ''
}

function readFollowingIndentAfterComma(code, index) {
  if (!code.startsWith('\n', index)) return ''
  let i = index + 1
  while (code[i] === ' ' || code[i] === '\t') i++
  const next = code[i]
  // Swallow only the newline that would otherwise become an extra visual row after
  // the inline <details>. Keep the original indentation spaces for the next key.
  return next === '"' || next === '}' || next === ']' ? '\n' : ''
}

function readFollowingIndentBeforeClose(code, index) {
  if (!code.startsWith('\n', index)) return ''
  let i = index + 1
  while (code[i] === ' ' || code[i] === '\t') i++
  return code[i] === ']' || code[i] === '}' ? code.slice(index, i) : ''
}

function getLinePrefixLength(code, index) {
  const lineStart = code.lastIndexOf('\n', index - 1) + 1
  return index - lineStart
}

function renderJsonStringValue(rawToken, collapseLongStrings, trailingComma = '', previewLimit = JSON_LONG_STRING_FALLBACK_LIMIT) {
  if (!collapseLongStrings) return null

  let value
  try {
    value = JSON.parse(rawToken)
  } catch {
    return null
  }

  if (typeof value !== 'string') return null

  const rawContent = rawToken.slice(1, -1)
  const rawChars = Array.from(rawContent)
  if (rawChars.length <= previewLimit) return null

  const rawPreview = rawChars.slice(0, previewLimit).join('')
  const previewHtml = escapeHtml(rawPreview)
  const rawContentHtml = escapeHtml(rawContent).replace(/\\/g, '&#92;')
  const fullHtml = escapeHtml(value)
  const trailingCommaHtml = escapeHtml(trailingComma)

  return `<details class="hl-json-expand"><summary title="展开/收起完整文本" aria-label="展开/收起完整文本"><span class="hl-json-expand-icon"><span class="hl-json-expand-open">▸</span><span class="hl-json-expand-close">▾</span></span><span class="hl-string hl-json-string-preview" data-raw-json-preview="${rawContentHtml}">&quot;${previewHtml}…&quot;</span>${trailingCommaHtml}</summary><span class="hl-json-expand-full">${fullHtml}</span></details>`
}

function renderHighlighted(code, matches) {
  matches.sort((a, b) => a.start - b.start || a.end - b.end)

  const filtered = []
  let cursor = 0
  for (const match of matches) {
    if (match.start >= cursor) {
      filtered.push(match)
      cursor = match.end
    }
  }

  let result = ''
  let pos = 0
  for (const match of filtered) {
    if (match.start > pos) result += escapeHtml(code.slice(pos, match.start))
    result += match.html ?? `<span class="${match.className}">${escapeHtml(match.text)}</span>`
    pos = match.end
  }
  if (pos < code.length) result += escapeHtml(code.slice(pos))

  return result
}

function highlightJson(code, options = {}) {
  const escaped = escapeHtml(code)
  const collapseLongStrings = options.collapseLongStrings !== false
  const previewColumns = Number.isFinite(options.jsonPreviewColumns) ? options.jsonPreviewColumns : null

  try {
    const matches = []
    const stringPattern = /"(?:[^"\\]|\\.)*"/g
    let m

    while ((m = stringPattern.exec(code)) !== null) {
      const rawToken = m[0]
      const start = m.index
      const end = start + rawToken.length
      const isKey = findNextNonWhitespaceChar(code, end) === ':'
      const previousNonWhitespaceChar = findPreviousNonWhitespaceChar(code, start)
      const isJsonStringValue = !isKey && (previousNonWhitespaceChar === ':' || previousNonWhitespaceChar === '[' || previousNonWhitespaceChar === ',')
      const className = isKey ? 'hl-key' : 'hl-string'
      const trailingComma = isJsonStringValue ? readInlineTrailingComma(code, end) : ''
      const linePrefixLength = getLinePrefixLength(code, start)
      const previewLimit = previewColumns == null
        ? JSON_LONG_STRING_FALLBACK_LIMIT
        : Math.max(JSON_PREVIEW_MIN_CHARS, Math.floor(previewColumns - linePrefixLength - JSON_PREVIEW_RESERVED_CHARS))
      const html = isJsonStringValue
        ? renderJsonStringValue(rawToken, collapseLongStrings, trailingComma, previewLimit)
        : null
      const afterComma = end + trailingComma.length
      const followingIndent = html
        ? (trailingComma ? readFollowingIndentAfterComma(code, afterComma) : readFollowingIndentBeforeClose(code, end))
        : ''
      const matchEnd = html ? afterComma + followingIndent.length : end

      matches.push({ start, end: matchEnd, text: code.slice(start, matchEnd), className, html })
    }

    for (const rule of JSON_RULES.slice(2)) {
      rule.pattern.lastIndex = 0
      while ((m = rule.pattern.exec(code)) !== null) {
        matches.push({ start: m.index, end: m.index + m[0].length, text: m[0], className: rule.className })
      }
    }

    if (matches.length === 0) return escaped
    return renderHighlighted(code, matches)
  } catch {
    return escaped
  }
}

/**
 * Applies syntax highlighting to code after HTML-escaping it.
 * Falls back to plain escapeHtml for unsupported languages.
 * @param {string} code  Raw code text (unescaped)
 * @param {string} lang  Language identifier (python/json/bash/sh etc.)
 * @param {object} options Optional rendering options
 * @returns {string}     Safe HTML string suitable for insertion into <code>
 */
export function highlight(code, lang, options = {}) {
  const normalizedLang = String(lang || '').toLowerCase()
  if (normalizedLang === 'json') return highlightJson(code, options)

  const rules = RULES[normalizedLang]
  const escaped = escapeHtml(code)

  if (!rules) return escaped

  try {
    const matches = []

    for (const rule of rules) {
      rule.pattern.lastIndex = 0
      let m
      while ((m = rule.pattern.exec(code)) !== null) {
        matches.push({ start: m.index, end: m.index + m[0].length, text: m[0], className: rule.className })
      }
    }

    if (matches.length === 0) return escaped
    return renderHighlighted(code, matches)
  } catch {
    return escaped
  }
}
