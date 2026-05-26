/**
 * Lightweight syntax highlighting module using regex rules.
 * No third-party runtime dependencies.
 */

const PYTHON_RULES = [
  // Triple-quoted strings (highest priority)
  { pattern: /"""[\s\S]*?"""|'''[\s\S]*?'''/g, className: 'hl-string' },
  // Single-line strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Decorators
  { pattern: /@\w+/g, className: 'hl-decorator' },
  // Comments
  { pattern: /#[^\n]*/g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(def|class|if|else|elif|for|while|return|import|from|with|as|try|except|finally|pass|break|continue|True|False|None|yield|lambda|async|await|not|and|or|is|in|del|raise|global|nonlocal|assert)\b/g,
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
    pattern: /\b(if|then|fi|else|elif|for|while|do|done|case|esac|function|return|export|local|source|alias|unset|shift|exit|echo|printf|read|test|true|false)\b/g,
    className: 'hl-keyword',
  },
  // Variable references: ${var} and $var
  { pattern: /\$\{[^}]+}|\$[A-Za-z_][A-Za-z0-9_]*/g, className: 'hl-variable' },
]

const JS_RULES = [
  // Template literals
  { pattern: /`(?:[^`\\]|\\.)*`/g, className: 'hl-string' },
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments (line and block)
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|this|class|extends|super|import|from|export|default|typeof|instanceof|void|delete|throw|try|catch|finally|async|await|yield|of|in)\b/g,
    className: 'hl-keyword',
  },
  // Built-in values
  { pattern: /\b(true|false|null|undefined|NaN|Infinity)\b/g, className: 'hl-boolean' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/g, className: 'hl-number' },
  // Arrow functions
  { pattern: /=>/g, className: 'hl-keyword' },
]

const TS_RULES = [
  // Template literals
  { pattern: /`(?:[^`\\]|\\.)*`/g, className: 'hl-string' },
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|this|class|extends|super|import|from|export|default|typeof|instanceof|void|delete|throw|try|catch|finally|async|await|yield|of|in|type|interface|enum|namespace|declare|abstract|implements|readonly|as|is|keyof|infer)\b/g,
    className: 'hl-keyword',
  },
  // Built-in values
  { pattern: /\b(true|false|null|undefined|NaN|Infinity)\b/g, className: 'hl-boolean' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/g, className: 'hl-number' },
  { pattern: /=>/g, className: 'hl-keyword' },
]

const HTML_RULES = [
  // Comments
  { pattern: /<!--[\s\S]*?-->/g, className: 'hl-comment' },
  // Strings in attributes
  { pattern: /"[^"]*"|'[^']*'/g, className: 'hl-string' },
  // Tags
  { pattern: /<\/?[\w-]+/g, className: 'hl-tag' },
  // Attributes
  { pattern: /\s[\w-]+(?==)/g, className: 'hl-attribute' },
  // Entities
  { pattern: /&\w+;|&#\d+;|&#x[\da-fA-F]+;/g, className: 'hl-variable' },
]

const CSS_RULES = [
  // Comments
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // @rules
  { pattern: /@[\w-]+/g, className: 'hl-decorator' },
  // Properties (word before colon)
  { pattern: /[\w-]+(?=\s*:)/g, className: 'hl-key' },
  // Hex colors
  { pattern: /#[0-9a-fA-F]{3,8}\b/g, className: 'hl-number' },
  // Values with units
  { pattern: /\b\d+(\.\d+)?(px|em|rem|%|vh|vw|vmin|vmax|ch|ex|deg|rad|turn|s|ms|fr)\b/g, className: 'hl-number' },
  // Plain numbers
  { pattern: /\b\d+(\.\d+)?\b/g, className: 'hl-number' },
  // Selectors (simplified: .class, #id)
  { pattern: /[.#][\w-]+/g, className: 'hl-keyword' },
]

const JAVA_RULES = [
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"/g, className: 'hl-string' },
  // Char literals
  { pattern: /'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Annotations
  { pattern: /@\w+/g, className: 'hl-decorator' },
  // Keywords
  {
    pattern: /\b(abstract|assert|boolean|break|byte|case|catch|char|class|const|continue|default|do|double|else|enum|extends|final|finally|float|for|goto|if|implements|import|instanceof|int|interface|long|native|new|package|private|protected|public|return|short|static|strictfp|super|switch|synchronized|this|throw|throws|transient|try|void|volatile|while|var|record|sealed|permits|yield)\b/g,
    className: 'hl-keyword',
  },
  // Built-in values
  { pattern: /\b(true|false|null)\b/g, className: 'hl-boolean' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?[fFdDlL]?\b/g, className: 'hl-number' },
]

const GO_RULES = [
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"|`[^`]*`/g, className: 'hl-string' },
  // Char literals
  { pattern: /'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(break|case|chan|const|continue|default|defer|else|fallthrough|for|func|go|goto|if|import|interface|map|package|range|return|select|struct|switch|type|var)\b/g,
    className: 'hl-keyword',
  },
  // Built-in values
  { pattern: /\b(true|false|nil|iota)\b/g, className: 'hl-boolean' },
  // Built-in types
  { pattern: /\b(bool|byte|complex(64|128)|error|float(32|64)|int(8|16|32|64)?|rune|string|uint(8|16|32|64)?|uintptr|any|comparable)\b/g, className: 'hl-type' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/g, className: 'hl-number' },
]

const RUST_RULES = [
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"/g, className: 'hl-string' },
  // Raw strings
  { pattern: /r#*"[^"]*?"#*/g, className: 'hl-string' },
  // Char literals
  { pattern: /'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Attributes
  { pattern: /#\[[^\]]*\]/g, className: 'hl-decorator' },
  // Macros (word!)
  { pattern: /\b\w+!/g, className: 'hl-decorator' },
  // Keywords
  {
    pattern: /\b(as|async|await|break|const|continue|crate|dyn|else|enum|extern|fn|for|if|impl|in|let|loop|match|mod|move|mut|pub|ref|return|self|Self|static|struct|super|trait|type|unsafe|use|where|while|yield)\b/g,
    className: 'hl-keyword',
  },
  // Built-in values
  { pattern: /\b(true|false|Some|None|Ok|Err)\b/g, className: 'hl-boolean' },
  // Types
  { pattern: /\b(bool|char|f32|f64|i8|i16|i32|i64|i128|isize|str|u8|u16|u32|u64|u128|usize|String|Vec|Box|Rc|Arc|Option|Result|HashMap|HashSet)\b/g, className: 'hl-type' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?(_\d+)*\b/g, className: 'hl-number' },
]

const C_RULES = [
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"/g, className: 'hl-string' },
  // Char literals
  { pattern: /'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Preprocessor
  { pattern: /^\s*#\s*\w+[^\n]*/gm, className: 'hl-decorator' },
  // Comments
  { pattern: /\/\/[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(auto|break|case|char|const|continue|default|do|double|else|enum|extern|float|for|goto|if|inline|int|long|register|restrict|return|short|signed|sizeof|static|struct|switch|typedef|union|unsigned|void|volatile|while|_Bool|_Complex|_Imaginary)\b/g,
    className: 'hl-keyword',
  },
  // Common types
  { pattern: /\b(int8_t|int16_t|int32_t|int64_t|uint8_t|uint16_t|uint32_t|uint64_t|size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|bool|NULL|EOF|stdin|stdout|stderr)\b/g, className: 'hl-type' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?[fFlLuU]*\b/g, className: 'hl-number' },
]

const SQL_RULES = [
  // Strings
  { pattern: /'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Comments
  { pattern: /--[^\n]*/g, className: 'hl-comment' },
  { pattern: /\/\*[\s\S]*?\*\//g, className: 'hl-comment' },
  // Keywords
  {
    pattern: /\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|ALTER|DROP|INDEX|VIEW|AND|OR|NOT|IN|IS|NULL|LIKE|BETWEEN|EXISTS|HAVING|GROUP|BY|ORDER|ASC|DESC|LIMIT|OFFSET|JOIN|INNER|LEFT|RIGHT|OUTER|FULL|CROSS|ON|AS|UNION|ALL|DISTINCT|CASE|WHEN|THEN|ELSE|END|BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE|PRIMARY|KEY|FOREIGN|REFERENCES|DEFAULT|CHECK|CONSTRAINT|UNIQUE|CASCADE|TRIGGER|FUNCTION|PROCEDURE|RETURNS|DECLARE|CURSOR|FETCH|OPEN|CLOSE|IF|WHILE|LOOP|EXIT|EXECUTE|EXPLAIN|ANALYZE|VACUUM)\b/gi,
    className: 'hl-keyword',
  },
  // Numbers
  { pattern: /\b\d+(\.\d+)?\b/g, className: 'hl-number' },
]

const YAML_RULES = [
  // Comments
  { pattern: /#[^\n]*/g, className: 'hl-comment' },
  // Strings
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Keys
  { pattern: /^[\t ]*[\w][\w .-]*(?=\s*:)/gm, className: 'hl-key' },
  // Booleans & null
  { pattern: /\b(true|false|yes|no|on|off|null)\b/gi, className: 'hl-boolean' },
  // Anchors & aliases
  { pattern: /[&*][\w-]+/g, className: 'hl-variable' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?([eE][+-]?\d+)?\b/g, className: 'hl-number' },
]

const TOML_RULES = [
  // Comments
  { pattern: /#[^\n]*/g, className: 'hl-comment' },
  // Strings
  { pattern: /"""[\s\S]*?"""|'''[\s\S]*?'''/g, className: 'hl-string' },
  { pattern: /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, className: 'hl-string' },
  // Section headers
  { pattern: /^\s*\[[\w.-]+\]/gm, className: 'hl-decorator' },
  { pattern: /^\s*\[\[[\w.-]+\]\]/gm, className: 'hl-decorator' },
  // Keys
  { pattern: /^[\t ]*[\w][\w -]*(?=\s*=)/gm, className: 'hl-key' },
  // Booleans
  { pattern: /\b(true|false)\b/g, className: 'hl-boolean' },
  // Numbers
  { pattern: /\b\d+(\.\d+)?\b/g, className: 'hl-number' },
  // Dates
  { pattern: /\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/g, className: 'hl-number' },
]

const RULES = {
  python: PYTHON_RULES,
  py: PYTHON_RULES,
  json: JSON_RULES,
  bash: BASH_RULES,
  sh: BASH_RULES,
  zsh: BASH_RULES,
  javascript: JS_RULES,
  js: JS_RULES,
  jsx: JS_RULES,
  mjs: JS_RULES,
  cjs: JS_RULES,
  typescript: TS_RULES,
  ts: TS_RULES,
  tsx: TS_RULES,
  html: HTML_RULES,
  htm: HTML_RULES,
  xml: HTML_RULES,
  svg: HTML_RULES,
  vue: HTML_RULES,
  svelte: HTML_RULES,
  css: CSS_RULES,
  scss: CSS_RULES,
  less: CSS_RULES,
  java: JAVA_RULES,
  kt: JAVA_RULES,
  kotlin: JAVA_RULES,
  go: GO_RULES,
  rust: RUST_RULES,
  rs: RUST_RULES,
  c: C_RULES,
  h: C_RULES,
  cpp: C_RULES,
  cxx: C_RULES,
  cc: C_RULES,
  hpp: C_RULES,
  sql: SQL_RULES,
  yaml: YAML_RULES,
  yml: YAML_RULES,
  toml: TOML_RULES,
  dockerfile: BASH_RULES,
  makefile: BASH_RULES,
  mk: BASH_RULES,
  ini: TOML_RULES,
  cfg: TOML_RULES,
  conf: TOML_RULES,
  env: BASH_RULES,
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

/**
 * Get the language identifier from a file extension.
 * @param {string} filename
 * @returns {string} Language identifier or empty string
 */
export function getFileLang(filename) {
  if (!filename) return ''
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  if (ext === 'md' || ext === 'markdown' || ext === 'mdx') return ''
  return RULES[ext] ? ext : ''
}

/**
 * Check if a file is a markdown file.
 * @param {string} filename
 * @returns {boolean}
 */
export function isMarkdownFile(filename) {
  if (!filename) return false
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  return ext === 'md' || ext === 'markdown' || ext === 'mdx'
}
