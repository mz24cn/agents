/**
 * Where ASR text lands in the chat editor.
 *
 * The in-flight (partial) text of the current speech segment is kept as its
 * own text node so it can be rewritten in place while the service refines it,
 * and the user can keep typing around it — nothing is sent until they press
 * send.  A finalized segment releases the node: those words become ordinary
 * text the ASR will not touch again.
 *
 * Insertion follows the caret: at the caret normally, replacing the selection
 * when the user has one — which is what makes dictation usable for fixing a
 * word (select it, say the replacement).
 *
 * The one rule that cannot be delegated to the caller: a partial is *cumulative*
 * ("今天" → "今天天气" → "今天天气不错"), so it must replace the previous partial
 * instead of being appended after it.  That is why the run is tracked; and when
 * the user edits inside it, the run is released rather than overwritten — user
 * text is never destroyed.
 */

const LATIN_WORD = /[A-Za-z0-9]/

/**
 * A space is only needed between two Latin words; CJK runs are joined bare.
 * @param {string} beforeChar character already in the box, left of the insert
 * @param {string} value incoming run
 */
export function needsSpaceBeforeRun(beforeChar, value) {
  if (!value) return false
  return LATIN_WORD.test(String(beforeChar || '')) && LATIN_WORD.test(value[0])
}

/** Character immediately to the left of a Range's start position. */
export function charBeforeRange(range) {
  if (!range) return ''
  const container = range.startContainer
  const offset = range.startOffset
  if (!container) return ''
  if (container.nodeType === 3 /* TEXT_NODE */) {
    const text = container.textContent || ''
    return offset > 0 ? text[offset - 1] : ''
  }
  const prev = container.childNodes[offset - 1]
  if (!prev) return ''
  const text = prev.textContent || ''
  return text ? text[text.length - 1] : ''
}

/**
 * Owns the live ASR run inside a contenteditable editor.
 *
 * `update()`  — a partial arrived: rewrite the run in place, or start a new
 *               one at the caret when there is nothing intact to rewrite.
 * `commit()`  — the segment finalized: settle its text and release the run.
 * `clear()`   — the segment was cancelled: drop the run (only if untouched).
 * `reset()`   — the editor was rebuilt from text (chips, cleared, restored):
 *               the node is gone, forget it.
 */
export class VoiceRunWriter {
  /**
   * @param {HTMLElement} editorEl contenteditable holding the message
   * @param {() => Selection|null} [selectionOf] injected for tests
   */
  constructor(editorEl, selectionOf = () => (typeof window !== 'undefined' ? window.getSelection() : null)) {
    this.editor = editorEl
    this.selectionOf = selectionOf
    /** @type {Text|null} the live run's text node, null when there is none */
    this.node = null
    /** exactly what we last wrote into `node` (intactness check) */
    this.text = ''
    /** separator kept in front of the run, preserved across updates */
    this.prefix = ''
    /** the user edited the run: stop rewriting this segment (their text wins) */
    this.suppressed = false
  }

  /** True while the node still holds exactly what we wrote (user untouched). */
  get intact() {
    return !!this.node && !!this.node.isConnected && this.node.textContent === this.text
  }

  reset() {
    this.node = null
    this.text = ''
    this.prefix = ''
    this.suppressed = false
  }

  update(value) {
    const next = String(value ?? '')
    if (this.suppressed) {
      // Wait for this segment to end rather than fight the user over it.
      if (!next) this.suppressed = false
      return false
    }
    if (!this.node) return next ? this._insertFresh(next) : true
    if (!this.node.isConnected) {
      // The run was deleted (backspaced away): dictating continues at the caret.
      this.reset()
      return next ? this._insertFresh(next) : true
    }
    if (this.node.textContent !== this.text) {
      // The user edited inside the run: keep their text, and do not append the
      // cumulative partial next to it either — that would duplicate the words.
      this.suppressed = true
      return false
    }
    if (!next) {
      this.node.remove()
      this.reset()
      return true
    }
    const caretWasAtEnd = this._caretAtRunEnd()
    this._write(next)
    if (caretWasAtEnd) this._placeCaretAfterRun()
    return true
  }

  commit(value) {
    const next = String(value ?? '')
    if (this.suppressed) {
      // Same segment: the user's version of these words stays as they left it.
      this.reset()
      return false
    }
    if (this.node && this.node.isConnected && this.node.textContent !== this.text) {
      this.reset()
      return false
    }
    if (this.intact) {
      if (next) {
        const caretWasAtEnd = this._caretAtRunEnd()
        this._write(next)
        if (caretWasAtEnd) this._placeCaretAfterRun()
      } else {
        this.node.remove()
      }
      this.reset()
      return true
    }
    this.reset()
    if (!next) return true
    return this._insertFresh(next)
  }

  clear() {
    if (this.intact) this.node.remove()
    this.reset()
    return true
  }

  // ------------------------------------------------------------------
  // internals
  // ------------------------------------------------------------------

  _write(value) {
    this.text = this.prefix + value
    this.node.textContent = this.text
    return this.text
  }

  /** Start a new run at the caret (replacing any selection) or at the end. */
  _insertFresh(value) {
    const selection = this.selectionOf()
    const range = selection && selection.rangeCount ? selection.getRangeAt(0) : null
    const caretInEditor = !!range && !!this.editor && this.editor.contains(range.startContainer)

    let node
    if (caretInEditor) {
      const before = charBeforeRange(range)
      this.prefix = needsSpaceBeforeRun(before, value) ? ' ' : ''
      range.deleteContents()
      node = document.createTextNode(this.prefix + value)
      range.insertNode(node)
      this.node = node
      this.text = node.textContent
      this._placeCaretAfterRun()
      return true
    }

    // No caret inside the editor (the user clicked away): append at the end,
    // which is what speaking into an unfocused box should do.
    const last = this.editor?.lastChild
    const before = last ? (last.textContent || '').slice(-1) : ''
    this.prefix = needsSpaceBeforeRun(before, value) ? ' ' : ''
    node = document.createTextNode(this.prefix + value)
    this.editor?.appendChild(node)
    this.node = node
    this.text = node.textContent
    return true
  }

  /** Caret collapsed right after the run (typing position for the next word). */
  _caretAtRunEnd() {
    const selection = this.selectionOf()
    if (!selection || !selection.rangeCount || !selection.isCollapsed || !this.node) return false
    const range = selection.getRangeAt(0)
    if (range.startContainer === this.node) {
      return range.startOffset >= (this.node.textContent || '').length
    }
    const parent = this.node.parentNode
    if (!parent || range.startContainer !== parent) return false
    const index = Array.prototype.indexOf.call(parent.childNodes, this.node)
    return range.startOffset === index + 1
  }

  _placeCaretAfterRun() {
    const selection = this.selectionOf()
    if (!selection || !this.node || !this.node.parentNode) return
    const range = document.createRange()
    range.setStartAfter(this.node)
    range.collapse(true)
    selection.removeAllRanges()
    selection.addRange(range)
  }
}
