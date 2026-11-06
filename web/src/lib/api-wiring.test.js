/**
 * Wiring guard: every api.js helper a component *calls* must be imported there.
 *
 * A component that calls ``getPauseStatus(sid)`` without importing it compiles
 * (Vite/rollup resolve nothing for a bare identifier -- it becomes a global
 * lookup) and fails only in the browser, usually inside a ``try { } catch { }``
 * that swallows the ReferenceError. The feature then looks present and is inert:
 * the cross-restart soft-pause lookup shipped exactly that way.
 *
 * There is no linter or svelte-check in this project, so this is the cheap
 * stand-in: scan the Svelte sources for api.js exports used as bare calls and
 * require each one to be imported or declared locally in the same file.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (entry.endsWith('.svelte')) out.push(full)
  }
  return out
}

/** Named exports of src/lib/api.js (functions, consts, and re-export lists). */
function apiExportNames() {
  const source = readFileSync(join(SRC, 'lib', 'api.js'), 'utf8')
  const names = new Set()
  for (const match of source.matchAll(/export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/g)) names.add(match[1])
  for (const match of source.matchAll(/export\s+(?:const|let|class)\s+([A-Za-z0-9_$]+)/g)) names.add(match[1])
  for (const match of source.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const clause of match[1].split(',')) {
      const name = clause.trim().split(/\s+as\s+/).pop()?.trim()
      if (name) names.add(name)
    }
  }
  return names
}

/** Names a file already has in scope: any import specifier or local declaration. */
function namesInScope(source) {
  const scoped = new Set()
  for (const match of source.matchAll(/import\s*(?:\{([^}]*)\}|(\*\s+as\s+[A-Za-z0-9_$]+)|([A-Za-z0-9_$]+))/g)) {
    const clause = match[1] ?? match[2] ?? match[3] ?? ''
    for (const part of clause.split(',')) {
      const name = part.trim().split(/\s+as\s+/).pop()?.trim()
      if (name) scoped.add(name)
    }
  }
  for (const match of source.matchAll(/(?:function|const|let|var|class)\s+([A-Za-z0-9_$]+)/g)) scoped.add(match[1])
  return scoped
}

describe('api.js wiring in components', () => {
  const exports = apiExportNames()

  it('finds the api.js exports to check', () => {
    // Guards the scanner itself: a parser regression would silently pass.
    expect(exports.has('getPauseStatus')).toBe(true)
    expect(exports.size).toBeGreaterThan(20)
  })

  it('every api.js helper a component calls is imported or declared locally', () => {
    const offenders = []
    for (const file of walk(SRC)) {
      const source = readFileSync(file, 'utf8')
      const scope = namesInScope(source)
      for (const name of exports) {
        if (scope.has(name)) continue
        // A bare ``name(`` call: not a property access, not a member of a word.
        if (new RegExp(`(?<![.\\w$])${name}\\s*\\(`).test(source)) offenders.push(`${file.slice(SRC.length + 1)}: ${name}()`)
      }
    }
    expect(offenders).toEqual([])
  })
})
