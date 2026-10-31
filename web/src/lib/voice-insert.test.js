/**
 * Where spoken text is written into the chat editor (voice-insert.js).
 *
 * The behaviour that matters to the user:
 *   - text lands at the caret, and a selection is replaced by what is said;
 *   - a partial is rewritten in place, never appended after itself;
 *   - once a segment is finalized the ASR stops touching it;
 *   - editing inside the live run takes it over — the ASR will not overwrite
 *     what was typed;
 *   - a cancelled segment disappears again, unless it was edited.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { VoiceRunWriter, needsSpaceBeforeRun, charBeforeRange } from './voice-insert.js'

let editor

function setEditor(html = '') {
  document.body.innerHTML = `<div id="ed" contenteditable="true">${html}</div>`
  editor = document.getElementById('ed')
}

/** Put the caret at a character offset in the editor's first text node. */
function caretAt(offset, node) {
  const selection = window.getSelection()
  const range = document.createRange()
  const target = node || editor.firstChild
  range.setStart(target, offset)
  range.collapse(true)
  selection.removeAllRanges()
  selection.addRange(range)
}

function selectInEditor(from, to) {
  const selection = window.getSelection()
  const range = document.createRange()
  const node = editor.firstChild
  range.setStart(node, from)
  range.setEnd(node, to)
  selection.removeAllRanges()
  selection.addRange(range)
}

const serialized = () => editor.textContent

describe('needsSpaceBeforeRun', () => {
  it('joins CJK bare and Latin words with a space', () => {
    expect(needsSpaceBeforeRun('错', '不错')).toBe(false)
    expect(needsSpaceBeforeRun('。', '明天')).toBe(false)
    expect(needsSpaceBeforeRun('o', 'world')).toBe(true)
    expect(needsSpaceBeforeRun('', 'world')).toBe(false)
    expect(needsSpaceBeforeRun('o', '。')).toBe(false)
  })

  it('reads the character left of a range', () => {
    setEditor('hello')
    const range = document.createRange()
    range.setStart(editor.firstChild, 5)
    expect(charBeforeRange(range)).toBe('o')
    range.setStart(editor.firstChild, 0)
    expect(charBeforeRange(range)).toBe('')
  })
})

describe('VoiceRunWriter', () => {
  beforeEach(() => setEditor(''))
  afterEach(() => { document.body.innerHTML = '' })

  it('inserts at the caret, replacing the selection', () => {
    setEditor('把这句话改掉')
    selectInEditor(1, 3)          // "这句" selected
    const writer = new VoiceRunWriter(editor)
    writer.update('那段')
    expect(serialized()).toBe('把那段话改掉')
    // The caret sits after the run, ready for the next words.
    const range = window.getSelection().getRangeAt(0)
    const caretAfterRun = range.startContainer === writer.node
      ? range.startOffset === writer.node.length
      : range.startContainer.childNodes[range.startOffset - 1] === writer.node
    expect(caretAfterRun).toBe(true)
  })

  it('rewrites a partial in place instead of appending it', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('今天')
    writer.update('今天天气')
    writer.update('今天天气不错')
    expect(serialized()).toBe('今天天气不错')
  })

  it('keeps typing around the live run', () => {
    setEditor('前面 ')
    caretAt(3)
    const writer = new VoiceRunWriter(editor)
    writer.update('说话')
    // A second text node appears where the user types; the run stays intact.
    editor.appendChild(document.createTextNode(' 后面'))
    writer.update('说话了')
    expect(serialized()).toBe('前面 说话了 后面')
  })

  it('releases a finalized segment and starts the next one after it', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('第一段')
    writer.commit('第一段。')
    writer.update('第二段')
    expect(serialized()).toBe('第一段。第二段')
    expect(writer.node).not.toBe(null)   // a new run, not the committed one
    writer.commit('第二段。')
    expect(serialized()).toBe('第一段。第二段。')
    expect(writer.node).toBe(null)
  })

  it('hands a run over to the user instead of overwriting it', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('今天天气')
    // The user fixes a word inside the live run.
    writer.node.textContent = '今天晴天'
    // Stale partials and the final of that same segment must neither clobber
    // the fix nor append the same words next to it.
    expect(writer.update('今天天气不错')).toBe(false)
    expect(serialized()).toBe('今天晴天')
    expect(writer.commit('今天天气不错')).toBe(false)
    expect(serialized()).toBe('今天晴天')
    // The next segment dictates normally.
    writer.update('明天')
    expect(serialized()).toBe('今天晴天明天')
  })

  it('keeps dictating after the live run is deleted', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('说错')
    writer.node.remove()               // the user backspaced the run away
    writer.update('说对了')
    expect(serialized()).toBe('说对了')
  })

  it('drops an untouched run when the segment is cancelled', () => {
    setEditor('保留')
    caretAt(2)
    const writer = new VoiceRunWriter(editor)
    writer.update('咳嗽')
    writer.update('')                  // segment_cancel
    expect(serialized()).toBe('保留')
    expect(writer.node).toBe(null)
  })

  it('keeps an edited run when the segment is cancelled', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('咳嗽')
    writer.node.textContent = '嗯'
    writer.clear()
    expect(serialized()).toBe('嗯')
  })

  it('separates Latin words but not CJK ones', () => {
    setEditor('hello')
    caretAt(5)
    const latin = new VoiceRunWriter(editor)
    latin.update('world')
    expect(serialized()).toBe('hello world')

    setEditor('你好')
    caretAt(2)
    const cjk = new VoiceRunWriter(editor)
    cjk.update('世界')
    expect(serialized()).toBe('你好世界')
  })

  it('appends at the end when the caret is not in the editor', () => {
    setEditor('已有内容')
    window.getSelection().removeAllRanges()
    const writer = new VoiceRunWriter(editor)
    writer.update('补上的话')
    expect(serialized()).toBe('已有内容补上的话')
  })

  it('starts a fresh run after the editor was rebuilt from text', () => {
    const writer = new VoiceRunWriter(editor)
    writer.update('旧')
    // ChatInput re-rendered the editor (file chips / cleared / restored).
    editor.replaceChildren(document.createTextNode('重排的文本 '))
    writer.reset()
    caretAt(6)
    writer.update('新')
    expect(serialized()).toBe('重排的文本 新')
  })

  it('final text replaces the partial where it already sits', () => {
    setEditor('开头 ')
    caretAt(3)
    const writer = new VoiceRunWriter(editor)
    writer.update('中间')
    editor.appendChild(document.createTextNode(' 结尾'))
    writer.commit('中间部分。')
    expect(serialized()).toBe('开头 中间部分。 结尾')
  })
})
