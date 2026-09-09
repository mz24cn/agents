/**
 * Compact tool badge display helpers.
 *
 * In compact chat mode every tool call renders as a small pill holding an
 * icon plus a short label.  Instead of always showing the raw tool name,
 * these helpers derive the most informative part of the call for the
 * typical built-in tools:
 *
 *   read_file / write_file / edit_file -> file name (path without directories)
 *   search_code                        -> first keyword of the query
 *   exec_shell / exec_cli              -> first substantive command statement
 *
 * All other tools keep the default icon and their plain tool name.
 */

const DEFAULT_ICON = '🛠️'

const TOOL_ICONS = {
  read_file: '👀',
  write_file: '💾',
  edit_file: '✍️',
  search_code: '🔎',
  exec_shell: '🔧',
  exec_cli: '💻',
}

const FILE_TOOLS = new Set(['read_file', 'write_file', 'edit_file'])

// Statements that only adjust shell state or print literals carry no
// intuitive meaning on their own; they are skipped while hunting for the
// first substantive statement of a compound command.
const NOISE_COMMANDS = new Set([
  'cd',
  'echo',
  'pwd',
  'set',
  'export',
  'unset',
  'source',
  'true',
  'clear',
  ':',
])

const MAX_LABEL_LENGTH = 40

/**
 * Tool call arguments arrive either as a JSON string (OpenAI-style stream
 * deltas) or as a plain object (persisted conversations).
 */
export function parseToolArgs(tc) {
  const raw = tc?.arguments
  if (raw == null) return {}
  if (typeof raw === 'object') return raw
  if (typeof raw !== 'string') return {}
  const text = raw.trim()
  if (!text) return {}
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    // Partial arguments are common while a call is still streaming.
    return {}
  }
}

function collapseWhitespace(text) {
  return String(text).replace(/\s+/g, ' ').trim()
}

function truncateLabel(text) {
  const value = collapseWhitespace(text)
  if (value.length <= MAX_LABEL_LENGTH) return value
  return `${value.slice(0, MAX_LABEL_LENGTH - 1)}…`
}

/** File name of a POSIX or Windows path, without its directory part. */
function basenameOfPath(path) {
  if (path == null) return ''
  const text = String(path).trim()
  if (!text) return ''
  // Split on both separators so Windows and POSIX paths behave the same.
  const segments = text.split(/[\\/]+/)
  const last = segments[segments.length - 1]
  return last.trim() || text
}

/**
 * First keyword of a search query: the first non-empty `|` alternative, then
 * the first whitespace-delimited token, which keeps alternation expressions
 * such as `read_file|write_file` short while still identifying the intent.
 */
function firstQueryKeyword(query) {
  if (query == null) return ''
  const text = String(query).trim()
  if (!text) return ''
  const alternatives = text.split('|').map((part) => part.trim())
  const first = alternatives.find((part) => part) || ''
  const keyword = first.replace(/^\^+/, '').trim().split(/\s+/)[0].trim()
  return keyword || first || text
}

/**
 * First statement of a compound command that does real work.  Noise
 * statements (cd, echo, bare assignments, ...) are skipped so the badge
 * shows e.g. `python run.py` for `cd /tmp && echo hi && python run.py`,
 * summarized to the command name plus its first substantive argument.
 */
function firstSubstantiveCommand(command) {
  if (command == null) return ''
  const text = String(command).trim()
  if (!text) return ''
  const segments = text
    .split(/&&|\|\||;|\n/)
    .map((segment) => segment.trim())
    .filter(Boolean)
  for (const segment of segments) {
    if (!isNoiseStatement(segment)) return summarizeCommand(segment)
  }
  // Every statement is noise (e.g. a bare `cd x`); still surface something.
  return segments.length ? summarizeCommand(segments[0]) : ''
}

/**
 * Split a statement into shell words, honoring single/double quotes so a
 * quoted argument containing spaces (e.g. a grep pattern) stays one token.
 * Quote characters are kept on the token; `unquote` strips them for display.
 */
function tokenize(statement) {
  const tokens = []
  let current = ''
  let quote = null
  for (let i = 0; i < statement.length; i += 1) {
    const ch = statement[i]
    if (quote) {
      if (ch === '\\' && quote === '"' && i + 1 < statement.length) {
        // An escaped char inside double quotes (e.g. \") is literal text.
        current += ch + statement[i + 1]
        i += 1
      } else {
        if (ch === quote) quote = null
        current += ch
      }
    } else if (ch === '"' || ch === "'") {
      quote = ch
      current += ch
    } else if (/\s/.test(ch)) {
      if (current) {
        tokens.push(current)
        current = ''
      }
    } else {
      current += ch
    }
  }
  if (current) tokens.push(current)
  return tokens
}

function isNoiseStatement(statement) {
  const tokens = tokenize(statement)
  let index = 0
  while (index < tokens.length && ASSIGNMENT_TOKEN.test(tokens[index])) {
    index += 1
  }
  const head = tokens[index]
  if (!head) return true
  return NOISE_COMMANDS.has(head.toLowerCase())
}

/** A `VAR=value` token, possibly prefixed before the real command. */
const ASSIGNMENT_TOKEN = /^[A-Za-z_][A-Za-z0-9_]*=/

/** Strip one pair of matching surrounding quotes for display. */
function unquote(token) {
  if (token.length >= 2) {
    const first = token[0]
    if ((first === '"' || first === "'") && token.endsWith(first)) {
      return token.slice(1, -1)
    }
  }
  return token
}

/**
 * Shorten a statement to its command name plus the first substantive
 * argument.  Switches (-n, --verbose), shell punctuation (|, >, ...) and
 * leading `VAR=value` prefixes are dropped, and one pair of surrounding
 * quotes is stripped from the kept argument: `sed -i '1520,1600p' x.py`
 * summarizes to `sed 1520,1600p`.
 */
function summarizeCommand(statement) {
  const tokens = tokenize(statement)
  let start = 0
  while (start < tokens.length && ASSIGNMENT_TOKEN.test(tokens[start])) {
    start += 1
  }
  const command = tokens[start]
  if (!command) return ''
  for (let i = start + 1; i < tokens.length; i += 1) {
    const arg = tokens[i]
    if (arg.startsWith('-')) continue // switches: -n, -la, --verbose
    // A bare shell operator (|, >, <) starts a new command or redirection:
    // stop here so `ls -la | grep py` summarizes to `ls`, not `ls grep`.
    if (!/\w/u.test(arg)) break
    return `${command} ${unquote(arg)}`
  }
  return command
}

/**
 * Derive the icon and label for a tool call's compact badge.
 *
 * @param {object|null} tc tool call ({ name, arguments })
 * @param {string} fallbackName shown when neither arguments nor name yield a label
 * @returns {{ icon: string, label: string }}
 */
export function compactToolDisplay(tc, fallbackName = '') {
  const name = tc?.name
  const icon = (name && TOOL_ICONS[name]) || DEFAULT_ICON
  const args = parseToolArgs(tc)

  let label = ''
  if (name && FILE_TOOLS.has(name)) {
    label = basenameOfPath(args.path)
  } else if (name === 'search_code') {
    label = firstQueryKeyword(args.query)
  } else if (name === 'exec_shell' || name === 'exec_cli') {
    label = firstSubstantiveCommand(args.command)
  }

  if (!label) label = name || fallbackName
  return { icon, label: truncateLabel(label) }
}
