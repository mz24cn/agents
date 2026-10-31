/**
 * Streaming voice input for the chat box (accessories/ASR backend).
 *
 * Flow: the browser captures the microphone, resamples it to 16 kHz mono
 * PCM16 and pushes it over a WebSocket; the ASR service answers with
 * `partial` (in-flight text of the current VAD segment) and `final` (the
 * committed text of a finished segment) events.
 *
 * The socket is **same-origin** (`/v1/asr/ws`): the runtime bridges it to the
 * ASR service.  The page therefore never learns the service host/port, an
 * https page is not blocked from opening a plain `ws://` socket (mixed
 * content), and the service token stays on the server.
 *
 * Everything that can be exercised without a browser — resampling, PCM
 * encoding, transcript accumulation, capability detection — lives in the pure
 * helpers below so it is unit-testable in node.  `AsrVoiceSession` is the thin
 * browser-only wrapper around them.
 */

export const ASR_WS_PATH = '/v1/asr/ws'
export const ASR_HEALTH_PATH = '/v1/asr/health'

/** Engine working rate; the service resamples anything else, 16k avoids that. */
export const ASR_TARGET_RATE = 16000
/** Binary frames are emitted in 20 ms slices (same as the reference client). */
export const ASR_FRAME_MS = 20

// Capture watchdog thresholds (see AsrVoiceSession._watchdog).
const MIC_STALL_MS = 2500          // no mic block delivered for this long
const LOUD_MS_FOR_SEGMENT = 4000   // audible mic audio with no segment_start
const VOICE_LEVEL = 0.05           // peak amplitude treated as "someone talks"

/**
 * Same-origin WebSocket URL for the ASR bridge.
 * @param {string} path
 * @param {Record<string, string|number>} params
 * @param {{protocol: string, host: string}} [loc] injected for tests
 */
export function buildAsrWsUrl(path = ASR_WS_PATH, params = {}, loc = (typeof location !== 'undefined' ? location : null)) {
  if (!loc) throw new Error('buildAsrWsUrl needs a location')
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${proto}//${loc.host}${path}`
  const pairs = []
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === '') continue
    pairs.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  }
  return pairs.length ? `${url}?${pairs.join('&')}` : url
}

/**
 * Why the microphone cannot be used, or null when it can.
 *
 * `getUserMedia` only exists in a secure context: https, or http on
 * localhost / 127.0.0.1 (browsers also treat file: as secure).  This is a
 * browser rule, not something the app can work around — over a plain
 * http://10.x.x.x origin the mic stays locked no matter how the ASR service is
 * reachable.
 *
 * @param {{mediaDevices?: {getUserMedia?: any}}} [nav]
 * @param {{isSecureContext?: boolean, location?: {protocol?: string, hostname?: string}}} [win]
 */
export function micBlockReason(
  nav = (typeof navigator !== 'undefined' ? navigator : {}),
  win = (typeof window !== 'undefined' ? window : {}),
) {
  const loc = win.location || {}
  const host = String(loc.hostname || '')
  const secure = win.isSecureContext === true
    || loc.protocol === 'https:'
    || host === 'localhost'
    || host === '127.0.0.1'
    || host === '[::1]'
    || loc.protocol === 'file:'
  if (!secure) return 'insecureContext'
  if (!nav.mediaDevices?.getUserMedia) return 'unsupported'
  return null
}

/**
 * Streaming band-limited resampler (float32 mono), used for mic -> engine.
 *
 * Plain linear interpolation is *not* safe when downsampling: a 48 kHz mic
 * block downsampled straight to 16 kHz folds every component above 8 kHz back
 * into the speech band at full amplitude (a 12 kHz tone reappears as 4 kHz).
 * That is in-band noise the ASR model cannot distinguish from speech, and it
 * costs accuracy on fricatives and tones.  So each output sample is a
 * windowed-sinc interpolation of the input, with the sinc narrowed by
 * `to/from` when downsampling — i.e. a proper anti-alias low-pass.
 *
 * Stateful: history is kept across blocks, so block boundaries do not click.
 */
export class AntiAliasResampler {
  /** @param {number} taps half-width of the FIR window, in input samples */
  constructor(fromRate, toRate, { taps = 24 } = {}) {
    this.from = Number(fromRate) || toRate
    this.to = Number(toRate) || fromRate
    this.half = Math.max(2, Math.round(taps))
    this.step = this.from / this.to          // input samples per output sample
    this.beta = Math.min(1, this.to / this.from)
    this._kernels = []                       // kernel cache, indexed by quantised phase
    // Seed the window with `half` silent samples so the very first output
    // samples have a full window to work with (leading edge is zero-padded,
    // flush() zero-pads the trailing edge).
    this._buf = new Float32Array(this.half)
    this._base = -this.half                  // absolute index of _buf[0]
    this._out = 0                            // index of the next output sample
  }

  _kernel(phase) {
    const slot = Math.round(phase * 256) % 257
    const cached = this._kernels[slot]
    if (cached) return cached
    const b = this.beta, half = this.half
    // Odd tap count, symmetric about x=0: an asymmetric tap set destroys the
    // linear-phase property and the stopband collapses (measured ~15 dB instead
    // of ~45 dB), which is exactly the leakage this filter exists to prevent.
    const k = new Float32Array(2 * half + 1)
    for (let j = 0; j <= 2 * half; j++) {
      const x = j - half + phase
      const ax = Math.abs(x)
      if (ax > half) { k[j] = 0; continue }
      const y = b * x
      const sinc = y === 0 ? 1 : Math.sin(Math.PI * y) / (Math.PI * y)
      k[j] = b * sinc * (0.54 + 0.46 * Math.cos(Math.PI * ax / half))
    }
    // A windowed sinc sums to ~1 but not exactly; dividing by the tap sum pins
    // the passband (and DC) at unity so levels are not silently shaved.
    let total = 0
    for (const v of k) total += v
    if (total) for (let j = 0; j < k.length; j++) k[j] /= total
    this._kernels[slot] = k
    return k
  }

  /** Resample one block; returns whole output samples only, the rest stays buffered. */
  process(input) {
    if (this.from === this.to) return input
    const merged = new Float32Array(this._buf.length + input.length)
    merged.set(this._buf); merged.set(input, this._buf.length)
    this._buf = merged
    const base = this._base, half = this.half, step = this.step
    const last = base + merged.length - 1

    const out = []
    while (true) {
      const pos = this._out * step
      const centre = Math.floor(pos)
      if (centre - half < base || centre + half > last) break
      const k = this._kernel(pos - centre)
      let acc = 0
      for (let j = 0; j <= 2 * half; j++) acc += k[j] * merged[centre - half + j - base]
      out.push(acc)
      this._out++
    }
    // keep enough history for the next block's first windows
    const keep = 2 * half
    if (merged.length > keep) { this._base = base + merged.length - keep; this._buf = merged.slice(-keep) }
    else { this._base = base }
    return Float32Array.from(out)
  }

  /** Emit the tail, zero-padding the input window (call once, at stop). */
  flush() {
    if (this.from === this.to) return new Float32Array(0)
    const half = this.half, step = this.step
    const merged = new Float32Array(this._buf.length + half)
    merged.set(this._buf)
    const base = this._base, last = base + merged.length - 1
    const out = []
    while (true) {
      const pos = this._out * step
      const centre = Math.floor(pos)
      if (centre - half < base || centre + half > last) break
      const k = this._kernel(pos - centre)
      let acc = 0
      for (let j = 0; j <= 2 * half; j++) acc += k[j] * merged[centre - half + j - base]
      out.push(acc)
      this._out++
    }
    this._buf = new Float32Array(0); this._base = base + merged.length
    return Float32Array.from(out)
  }
}

/** Linear-interpolation resample (float32 mono). Returns a new Float32Array. */
export function resampleLinear(input, fromRate, toRate) {
  const from = Number(fromRate) || toRate
  if (!input?.length || from === toRate) return input
  const ratio = toRate / from
  const out = new Float32Array(Math.floor(input.length * ratio))
  for (let i = 0; i < out.length; i++) {
    const idx = i / ratio
    const i0 = Math.floor(idx)
    const i1 = Math.min(i0 + 1, input.length - 1)
    out[i] = input[i0] + (input[i1] - input[i0]) * (idx - i0)
  }
  return out
}

/** float32 [-1,1] -> int16, clamped. */
export function floatToInt16(input) {
  const out = new Int16Array(input.length)
  for (let i = 0; i < input.length; i++) {
    const v = Math.max(-1, Math.min(1, input[i]))
    out[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)))
  }
  return out
}

/** int16 -> little-endian PCM16LE bytes (the wire format the service expects). */
export function pcm16ToBytes(int16) {
  const bytes = new Uint8Array(int16.length * 2)
  const view = new DataView(bytes.buffer)
  for (let i = 0; i < int16.length; i++) view.setInt16(i * 2, int16[i], true)
  return bytes
}

function concatFloat(a, b) {
  const out = new Float32Array(a.length + b.length)
  out.set(a); out.set(b, a.length)
  return out
}

/** Peak absolute amplitude of a float32 block, for the recording level meter. */
export function peakLevel(input) {
  let peak = 0
  for (let i = 0; i < input.length; i++) {
    const v = Math.abs(input[i])
    if (v > peak) peak = v
  }
  return peak
}

/**
 * Why a recording that ended cleanly produced no text.
 *
 * The service answers a flush with `flush_ack` even when it never opened a
 * speech segment, so "no text" is otherwise silent: the socket worked, the
 * button worked, nothing appeared.  The two cases send the user to different
 * fixes, so they are named separately.
 *
 * @param {{audioMs: number, sawResult: boolean}} sent/seen counters
 * @returns {null | 'noAudio' | 'noResult'} noAudio: nothing was ever sent;
 *          noResult: audio went up but the service found no speech in it.
 */
export function diagnoseEmptyResult({ audioMs = 0, sawResult = false } = {}) {
  if (sawResult) return null
  if (audioMs < 100) return 'noAudio'
  // Under ~0.3 s there is barely a syllable; not worth a warning.
  if (audioMs < 300) return null
  return 'noResult'
}

/**
 * Turns arbitrary-sized mic blocks into whole PCM frames at the target rate.
 *
 * The AudioContext delivers blocks sized by the audio graph (e.g. 4096 samples
 * at 48 kHz), while the ASR service is fed 20 ms slices at 16 kHz.  Leftover
 * samples that do not fill a frame stay buffered until the next block (or are
 * handed out by finish()).
 */
export class PcmStreamEncoder {
  constructor({ targetRate = ASR_TARGET_RATE, frameMs = ASR_FRAME_MS } = {}) {
    this.targetRate = targetRate
    this.frameSamples = Math.max(1, Math.round(targetRate * frameMs / 1000))
    this._pending = new Float32Array(0)
    this._resamplers = new Map()
  }

  /** One stateful resampler per distinct source rate (the AudioContext rate can change). */
  _resampler(sourceRate) {
    const rate = Number(sourceRate) || this.targetRate
    let r = this._resamplers.get(rate)
    if (!r) {
      r = new AntiAliasResampler(rate, this.targetRate)
      this._resamplers.set(rate, r)
    }
    return r
  }

  /**
   * @param {Float32Array} block one audio block, at `sourceRate`
   * @param {number} sourceRate sample rate of that block
   * @returns {Uint8Array[]} complete frames, ready to send as binary frames
   */
  push(block, sourceRate) {
    const resampled = this._resampler(sourceRate).process(block)
    const merged = new Float32Array(this._pending.length + resampled.length)
    merged.set(this._pending)
    merged.set(resampled, this._pending.length)

    const frames = []
    let offset = 0
    for (; offset + this.frameSamples <= merged.length; offset += this.frameSamples) {
      frames.push(pcm16ToBytes(floatToInt16(merged.subarray(offset, offset + this.frameSamples))))
    }
    this._pending = merged.slice(offset)
    return frames
  }

  /** Emit whatever is buffered (a short tail); the buffer is then empty. */
  finish() {
    for (const rate of this._resamplers.keys()) {
      const tail = this._resamplers.get(rate).flush()
      if (tail.length) this._pending = concatFloat(this._pending, tail)
    }
    if (!this._pending.length) return []
    const frames = [pcm16ToBytes(floatToInt16(this._pending))]
    this._pending = new Float32Array(0)
    return frames
  }
}

/** Transcript accumulator: one entry per ASR segment id. */
export function createAsrTranscript() {
  return { order: [], segs: new Map(), lastSegId: 0 }
}

/** Text of the segments that have not been committed yet (the live partial). */
export function liveText(state) {
  let out = ''
  for (const id of state.order) {
    const seg = state.segs.get(id)
    if (seg && !seg.final) out += (out ? ' ' : '') + seg.text
  }
  return out.trim()
}

/**
 * Fold one server event into the transcript.
 *
 * `partial` replaces the text of its segment, `final` / `replay` commit it
 * (returned as `committed`, exactly once per segment), `segment_cancel` drops
 * an uncommitted segment (the service discards coughs / noise shorter than
 * --min-speech-ms).  `live` is what the UI shows while speaking.
 *
 * @returns {{committed: string, live: string}}
 */
export function applyAsrEvent(state, event) {
  const type = event?.type
  let committed = ''

  if (type === 'partial' || type === 'final' || type === 'replay') {
    const id = Number.isFinite(event.seg_id) ? event.seg_id : 0
    if (!state.segs.has(id)) {
      state.order.push(id)
      state.segs.set(id, { text: '', final: false })
    }
    const seg = state.segs.get(id)
    seg.text = String(event.text ?? '')
    if (type !== 'partial' && !seg.final) {
      seg.final = true
      committed = seg.text
      if (id > state.lastSegId) state.lastSegId = id
    }
  } else if (type === 'segment_cancel') {
    const id = Number.isFinite(event.seg_id) ? event.seg_id : 0
    const seg = state.segs.get(id)
    if (seg && !seg.final) {
      state.segs.delete(id)
      state.order = state.order.filter((x) => x !== id)
    }
  } else if (type === 'reset') {
    state.segs.clear()
    state.order = []
  }

  return { committed, live: liveText(state) }
}

/**
 * One recording session: microphone in, transcript out.
 *
 * Callbacks: onStatus(state), onEvent(evt), onTranscript({committed, live}),
 * onLevel(peak), onError(code, message).
 */
export class AsrVoiceSession {
  constructor({
    url = null,            // null -> same-origin bridge (ASR_WS_PATH)
    sampleRate = ASR_TARGET_RATE,
    language = 'zh',
    vad = {},
    lastSegId,
    onStatus = () => {},
    onEvent = () => {},
    onTranscript = () => {},
    onLevel = () => {},
    onError = () => {},
  } = {}) {
    this.url = url
    this.sampleRate = sampleRate
    this.language = language
    this.vad = vad
    this.lastSegId = Number.isFinite(lastSegId) ? lastSegId : null
    this.onStatus = onStatus
    this.onEvent = onEvent
    this.onTranscript = onTranscript
    this.onLevel = onLevel
    this.onError = onError

    this.state = 'idle'
    this.engine = ''
    this.transcript = createAsrTranscript()

    this._ws = null
    this._audioCtx = null
    this._stream = null
    this._node = null
    this._src = null
    this._encoder = new PcmStreamEncoder({ targetRate: sampleRate })
    this._flushTimer = null
    this._disposed = false
    // Diagnostics for "the button worked but no text appeared": how much audio
    // actually left the browser, and whether the service ever answered.
    this._audioMs = 0
    this._sawResult = false
    this._lastBlockAt = 0        // last mic block handed to us by the browser
    this._watchTimer = null
    this._sawSegmentStart = false
    this._loudMs = 0
    this._reported = {}
  }

  get recording() { return this.state === 'recording' }

  /** @returns {Promise<void>} resolves once mic + socket are live */
  async start() {
    if (this._ws || this._stream) await this.abort()
    this.transcript = createAsrTranscript()
    this._encoder = new PcmStreamEncoder({ targetRate: this.sampleRate })
    this._audioMs = 0
    this._sawResult = false
    this._sawSegmentStart = false
    this._loudMs = 0
    this._reported = {}
    this._setStatus('connecting')
    await this._openSocket()
    try {
      await this._openMic()
    } catch (err) {
      await this.abort()
      this._setStatus('idle')
      throw err
    }
    this._setStatus('recording')
  }

  /** Stop capturing, ask the service to close the last segment, then hang up. */
  stop() {
    if (!this._stream && !this._ws) return
    this._stopMic()
    if (this._ws && this._ws.readyState === 1 /* OPEN */) {
      this._setStatus('finalizing')
      this._sendControl({ type: 'flush' })
      // flush_ack closes the socket; this only fires if the service stalls.
      this._flushTimer = setTimeout(() => this._closeSocket(), 5000)
    } else {
      this._closeSocket()
      this._setStatus('idle')
    }
  }

  /** Tear everything down without waiting for trailing results. */
  async abort() {
    this._stopMic()
    if (this._flushTimer) { clearTimeout(this._flushTimer); this._flushTimer = null }
    this._closeSocket()
    if (this.state !== 'idle') this._setStatus('idle')
  }

  /** Segments committed so far; a reconnect replays only what is missing. */
  get lastSegIdSeen() { return this.transcript.lastSegId }

  // -- internals ------------------------------------------------------------

  _setStatus(next) {
    if (this.state === next) return
    this.state = next
    this.onStatus(next)
  }

  _sendControl(obj) {
    try { this._ws?.send(JSON.stringify(obj)) } catch { /* socket already gone */ }
  }

  _openSocket() {
    const params = { sample_rate: this.sampleRate }
    if (this.lastSegId !== null) params.last_seg_id = this.lastSegId
    const url = this.url || buildAsrWsUrl(ASR_WS_PATH, params)

    return new Promise((resolve, reject) => {
      let ws
      try { ws = new WebSocket(url) } catch (err) { reject(err); return }
      ws.binaryType = 'arraybuffer'
      this._ws = ws

      ws.onopen = () => {
        this._sendControl({
          type: 'config',
          sample_rate: this.sampleRate,
          language: this.language,
          vad: { enabled: true, ...this.vad },
        })
        resolve()
      }
      ws.onmessage = (evt) => this._handleFrame(evt.data)
      ws.onerror = () => {
        if (this.state === 'connecting') reject(new Error('asr_connect_failed'))
      }
      ws.onclose = (evt) => {
        if (this.state === 'connecting' || this.state === 'recording' || this.state === 'finalizing') {
          if (this.state === 'recording') this.onError('asrDisconnected')
          this._stopMic()
          this._setStatus('idle')
        }
        this._ws = null
        if (this._flushTimer) { clearTimeout(this._flushTimer); this._flushTimer = null }
        if (evt?.code === 4401) this.onError('asrUnauthorized')
      }
    })
  }

  _handleFrame(raw) {
    let evt
    try { evt = JSON.parse(raw) } catch { return }
    this.onEvent(evt)

    switch (evt.type) {
      case 'ready':
        this.engine = evt.engine || ''
        break
      case 'error':
        // The bridge reports asr_unreachable here (service not started); the
        // service itself reports bad_rate / busy.  The code is translated in
        // the UI, the message is the service's own wording.
        this.onError(evt.code || 'asrError', evt.message || '')
        break
      case 'flush_ack':
        // The service closes cleanly even when it never found speech, so this
        // is the last chance to say why the box stayed empty.
        if (!this._sawResult) {
          const why = diagnoseEmptyResult({ audioMs: this._audioMs, sawResult: this._sawResult })
          if (why) this.onError(why, '')
        }
        this._closeSocket()
        this._setStatus('idle')
        break
      case 'segment_start':
        this._sawSegmentStart = true
        this._loudMs = 0
        break
      case 'partial':
      case 'final':
      case 'replay':
      case 'segment_cancel': {
        if (evt.type !== 'segment_cancel') this._sawResult = true
        const { committed, live } = applyAsrEvent(this.transcript, evt)
        if (committed) this.onTranscript({ committed, live })
        else this.onTranscript({ committed: '', live })
        break
      }
      default:
        break
    }
  }

  async _openMic() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
    this._stream = stream
    const Ctor = window.AudioContext || window.webkitAudioContext
    this._audioCtx = new Ctor()
    // Chrome/Edge can hand back a *suspended* context (autoplay policy, a tab
    // that lost focus, a policy set by an extension).  A suspended context
    // never fires onaudioprocess: the socket stays open, the button looks live,
    // and not one sample is sent.  Resume it, and say so if it will not run.
    if (this._audioCtx.state === 'suspended' && typeof this._audioCtx.resume === 'function') {
      try { await this._audioCtx.resume() } catch { /* reported below */ }
    }
    if (this._audioCtx.state === 'suspended') {
      throw Object.assign(new Error('audio context suspended'), { name: 'AudioContextSuspended' })
    }
    this._src = this._audioCtx.createMediaStreamSource(stream)
    // ScriptProcessor (not AudioWorklet): no extra module to ship, works in
    // every browser, and the per-block work (resample + copy) is negligible.
    this._node = this._audioCtx.createScriptProcessor(4096, 1, 1)
    this._node.onaudioprocess = (e) => {
      const block = e.inputBuffer.getChannelData(0)
      const level = peakLevel(block)
      this.onLevel(level)
      this._lastBlockAt = Date.now()
      if (level >= VOICE_LEVEL) this._loudMs += (block.length / this._audioCtx.sampleRate) * 1000
      if (!this._ws || this._ws.readyState !== 1) return
      for (const frame of this._encoder.push(block, this._audioCtx.sampleRate)) {
        try { this._ws.send(frame) } catch { break }
        this._audioMs += ASR_FRAME_MS
      }
    }
    this._src.connect(this._node)
    this._node.connect(this._audioCtx.destination)
    // Watch the capture itself, not just the socket.  Both failure modes below
    // look identical from the button ("I clicked, I spoke, nothing appeared"):
    // the audio graph stopped delivering blocks, or it delivers plenty of sound
    // while the service never opens a segment (noise floor raised by the
    // browser's AGC, or the voice is simply under the VAD gate).
    this._lastBlockAt = Date.now()
    if (this._watchTimer) clearInterval(this._watchTimer)
    this._watchTimer = setInterval(() => this._watchdog(), 1000)
  }

  _watchdog() {
    if (this.state !== 'recording') return
    if (Date.now() - this._lastBlockAt > MIC_STALL_MS) {
      this._reportOnce('micStalled')
      return
    }
    if (!this._sawSegmentStart && this._loudMs >= LOUD_MS_FOR_SEGMENT) {
      this._loudMs = 0                 // one hint per stretch of loud audio
      this._reportOnce('noSpeechDetected')
    }
  }

  _reportOnce(code) {
    if (this._reported[code]) return
    this._reported[code] = true
    this.onError(code, '')
  }

  _stopMic() {
    if (this._watchTimer) { clearInterval(this._watchTimer); this._watchTimer = null }
    try { this._node?.disconnect() } catch { /* already gone */ }
    try { this._src?.disconnect() } catch { /* already gone */ }
    // Push the sub-frame tail before the mic goes away, otherwise the last
    // syllable of an utterance is dropped.
    if (this._ws && this._ws.readyState === 1) {
      for (const frame of this._encoder.finish()) {
        try { this._ws.send(frame) } catch { break }
        this._audioMs += ASR_FRAME_MS
      }
    }
    try { this._stream?.getTracks().forEach((t) => t.stop()) } catch { /* already gone */ }
    try { this._audioCtx?.close() } catch { /* already gone */ }
    this._node = null
    this._src = null
    this._stream = null
    this._audioCtx = null
  }

  _closeSocket() {
    try { this._ws?.close() } catch { /* already gone */ }
    this._ws = null
  }
}
