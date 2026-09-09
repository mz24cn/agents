import { describe, expect, it } from 'vitest'
import { compactToolDisplay, parseToolArgs } from './compact-tool.js'

describe('parseToolArgs', () => {
  it('parses JSON string arguments', () => {
    expect(parseToolArgs({ arguments: '{"path":"a/b.py"}' })).toEqual({ path: 'a/b.py' })
  })

  it('passes object arguments through', () => {
    expect(parseToolArgs({ arguments: { path: 'a/b.py' } })).toEqual({ path: 'a/b.py' })
  })

  it('returns an empty object for missing or invalid arguments', () => {
    expect(parseToolArgs(null)).toEqual({})
    expect(parseToolArgs({})).toEqual({})
    expect(parseToolArgs({ arguments: '' })).toEqual({})
    expect(parseToolArgs({ arguments: '{"path":' })).toEqual({})
    expect(parseToolArgs({ arguments: '"just a string"' })).toEqual({})
  })
})

describe('compactToolDisplay: file tools', () => {
  it('shows the file name instead of the tool name for read_file', () => {
    const display = compactToolDisplay({
      name: 'read_file',
      arguments: '{"path":"web/src/lib/compact-tool.js"}',
    })
    expect(display).toEqual({ icon: '👀', label: 'compact-tool.js' })
  })

  it('maps write_file and edit_file to their own icons', () => {
    expect(compactToolDisplay({ name: 'write_file', arguments: { path: 'a/b.txt' } })).toEqual({
      icon: '💾',
      label: 'b.txt',
    })
    expect(compactToolDisplay({ name: 'edit_file', arguments: { path: '/abs/c.txt' } })).toEqual({
      icon: '✍️',
      label: 'c.txt',
    })
  })

  it('handles Windows-style paths', () => {
    expect(compactToolDisplay({ name: 'read_file', arguments: { path: 'C:\\tmp\\notes.md' } })).toEqual({
      icon: '👀',
      label: 'notes.md',
    })
    expect(compactToolDisplay({ name: 'write_file', arguments: { path: 'C:/scratch/tmp/x.log' } })).toEqual({
      icon: '💾',
      label: 'x.log',
    })
  })

  it('keeps a path without a directory separator as-is', () => {
    expect(compactToolDisplay({ name: 'edit_file', arguments: { path: 'Makefile' } })).toEqual({
      icon: '✍️',
      label: 'Makefile',
    })
  })

  it('falls back to the tool name when path is missing (e.g. while streaming)', () => {
    expect(compactToolDisplay({ name: 'read_file', arguments: '{"path":' })).toEqual({
      icon: '👀',
      label: 'read_file',
    })
    expect(compactToolDisplay({ name: 'write_file' })).toEqual({ icon: '💾', label: 'write_file' })
  })
})

describe('compactToolDisplay: search_code', () => {
  it('shows the query for a plain pattern', () => {
    expect(compactToolDisplay({ name: 'search_code', arguments: { query: 'ToolCallCard' } })).toEqual({
      icon: '🔎',
      label: 'ToolCallCard',
    })
  })

  it('takes only the first keyword of a pipe alternation', () => {
    const display = compactToolDisplay({
      name: 'search_code',
      arguments: { query: 'read_file|write_file|edit_file' },
    })
    expect(display).toEqual({ icon: '🔎', label: 'read_file' })
  })

  it('takes the first keyword of a multi-word pattern', () => {
    expect(compactToolDisplay({ name: 'search_code', arguments: { query: 'def main' } })).toEqual({
      icon: '🔎',
      label: 'def',
    })
  })

  it('skips an empty leading alternative', () => {
    expect(compactToolDisplay({ name: 'search_code', arguments: { query: '|foo|bar' } })).toEqual({
      icon: '🔎',
      label: 'foo',
    })
  })

  it('falls back to the tool name when query is missing', () => {
    expect(compactToolDisplay({ name: 'search_code' })).toEqual({ icon: '🔎', label: 'search_code' })
  })
})

describe('compactToolDisplay: exec tools', () => {
  it('skips cd/echo and summarizes the first substantive command', () => {
    const display = compactToolDisplay({
      name: 'exec_shell',
      arguments: { command: 'cd /tmp && echo hi && python run.py --x' },
    })
    expect(display).toEqual({ icon: '🔧', label: 'python run.py' })
  })

  it('maps exec_cli to its own icon', () => {
    expect(compactToolDisplay({ name: 'exec_cli', arguments: { command: 'pytest -x' } })).toEqual({
      icon: '💻',
      label: 'pytest',
    })
  })

  it('drops switch arguments and keeps only the first substantive one', () => {
    // `grep -n "name=\|\"name\"\|'name'" runtime/x.py` -> `grep name=\|\"name\"\|'name'`
    const display = compactToolDisplay({
      name: 'exec_shell',
      arguments: { command: `grep -n "name=\\|\\"name\\"\\|'name'" runtime/builtin_tools_coding.py` },
    })
    expect(display).toEqual({ icon: '🔧', label: `grep name=\\|\\"name\\"\\|'name'` })
  })

  it('keeps a quoted argument containing spaces as one word', () => {
    const display = compactToolDisplay({
      name: 'exec_shell',
      arguments: { command: `grep -n "op=push\\|op = \\"push\\"\\|\\"Unsu\\\"" runtime/handler_api.py` },
    })
    expect(display.label.startsWith('grep op=push\\|op = \\"push\\"\\|')).toBe(true)
    expect(display.label.endsWith('runtime/handler_api.py')).toBe(false)
  })

  it('strips one pair of surrounding quotes from the kept argument', () => {
    expect(
      compactToolDisplay({ name: 'exec_shell', arguments: { command: "sed -i '1520,1600p' runtime/handler_api.py" } })
    ).toEqual({ icon: '🔧', label: 'sed 1520,1600p' })
  })

  it('keeps a plain positional argument and drops trailing switches', () => {
    expect(
      compactToolDisplay({ name: 'exec_shell', arguments: { command: 'git log --oneline -n 5' } })
    ).toEqual({ icon: '🔧', label: 'git log' })
  })

  it('keeps a noise-only command visible instead of blank', () => {
    expect(compactToolDisplay({ name: 'exec_shell', arguments: { command: 'cd /var/log' } })).toEqual({
      icon: '🔧',
      label: 'cd /var/log',
    })
  })

  it('stops at shell operators within a statement', () => {
    expect(
      compactToolDisplay({ name: 'exec_shell', arguments: { command: 'cd src\nls -la | grep py' } })
    ).toEqual({ icon: '🔧', label: 'ls' })
  })

  it('skips bare assignments but keeps prefixed real commands', () => {
    expect(
      compactToolDisplay({ name: 'exec_shell', arguments: { command: 'export FOO=bar && FOO=1 npm test' } })
    ).toEqual({ icon: '🔧', label: 'npm test' })
  })

  it('falls back to the tool name for an empty command', () => {
    expect(compactToolDisplay({ name: 'exec_cli', arguments: { command: '' } })).toEqual({
      icon: '💻',
      label: 'exec_cli',
    })
  })
})

describe('compactToolDisplay: fallback behavior', () => {
  it('leaves other tools untouched with the default icon', () => {
    expect(compactToolDisplay({ name: 'fetch', arguments: { url: 'https://example.com' } })).toEqual({
      icon: '🛠️',
      label: 'fetch',
    })
  })

  it('uses the fallback name for unknown tools without a name', () => {
    expect(compactToolDisplay(null, 'unknown')).toEqual({ icon: '🛠️', label: 'unknown' })
    expect(compactToolDisplay({}, 'unknown')).toEqual({ icon: '🛠️', label: 'unknown' })
  })

  it('truncates overly long labels with an ellipsis', () => {
    const display = compactToolDisplay({
      name: 'exec_shell',
      arguments: { command: 'python ' + 'a'.repeat(80) },
    })
    expect(display.label.length).toBe(40)
    expect(display.label.endsWith('…')).toBe(true)
  })
})
