import { describe, expect, it } from 'vitest'
import {
  buildFileJournalTurnKeyMap,
  resolveFileJournalTurnKey,
  timestampAliases,
} from './file-journals.js'

describe('file journal timestamp matching', () => {
  it('keeps the exact backend timestamp for diff requests', () => {
    const map = buildFileJournalTurnKeyMap(['2026-08-19T23:02:25'])

    expect(resolveFileJournalTurnKey(map, '2026-08-19T23:02:25'))
      .toBe('2026-08-19T23:02:25')
  })

  it('matches restored messages that use a space instead of T', () => {
    const map = buildFileJournalTurnKeyMap(['2026-08-19T23:02:25'])

    expect(resolveFileJournalTurnKey(map, '2026-08-19 23:02:25'))
      .toBe('2026-08-19T23:02:25')
  })

  it('matches historical fractional-second timestamps', () => {
    const map = buildFileJournalTurnKeyMap(['2026-08-19T23:02:25'])

    expect(resolveFileJournalTurnKey(map, '2026-08-19T23:02:25.734'))
      .toBe('2026-08-19T23:02:25')
  })

  it('does not create aliases for empty values', () => {
    expect(timestampAliases(null)).toEqual([])
    expect(timestampAliases('')).toEqual([])
  })
})
