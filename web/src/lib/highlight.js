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

/**
 * Applies syntax highlighting to code after HTML-escaping it.
 * Falls back to plain escapeHtml for unsupported languages.
 * @param {string} code  Raw code text (unescaped)
 * @param {string} lang  Language identifier (python/json/bash/sh etc.)
 * @returns {string}     Safe HTML string suitable for insertion into <code>
 */
export function highlight(code, lang) {
  const rules = RULES[lang.toLowerCase()]
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
      result += `<span class="${match.className}">${escapeHtml(match.text)}</span>`
      pos = match.end
    }
    if (pos < code.length) result += escapeHtml(code.slice(pos))

    return result
  } catch {
    return escaped
  }
}
