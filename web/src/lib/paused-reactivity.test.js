// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import { mount, flushSync, unmount } from 'svelte'
import Harness from '../__paused_probe__/Harness.svelte'

describe('paused state reactivity chain', () => {
  it('Set.add from a plain callback flips ChatInput to the paused control', () => {
    const target = document.createElement('div')
    document.body.appendChild(target)
    const refs = {}
    mount(Harness, { target, props: { sessionId: 's1', eventsRef: refs } })
    flushSync()
    const span = () => target.querySelector('span')
    console.log('BEFORE: ' + JSON.stringify(target.textContent.slice(0, 140)))
    expect(target.innerHTML).toContain('⏹')
    refs.events({ status: 'paused' })
    flushSync()
    console.log('AFTER: ' + JSON.stringify(target.textContent.slice(0, 140)))
    console.log('innerHTML has ⏸ :', target.innerHTML.includes('⏸'))
    unmount(target)
    expect(target.innerHTML).toContain('⏸')
  })
})
