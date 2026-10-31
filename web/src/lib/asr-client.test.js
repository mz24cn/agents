/**
 * Tests for the voice-input client helpers (web/src/lib/asr-client.js).
 *
 * Focus: the parts that decide what the ASR service actually receives and what
 * lands in the chat box —
 *   - same-origin WebSocket URL (the runtime bridge, not a direct ws://host:7996)
 *   - the secure-context rule that decides whether the mic is usable at all
 *   - resampling + PCM16LE framing (48 kHz mic blocks -> 20 ms / 16 kHz frames)
 *   - transcript folding: partial replaces, final commits once, cancel drops
 */
import { describe, it, expect } from 'vitest'
import {
  AntiAliasResampler,
  ASR_WS_PATH,
  PcmStreamEncoder,
  applyAsrEvent,
  buildAsrWsUrl,
  createAsrTranscript,
  diagnoseEmptyResult,
  AsrVoiceSession,
  floatToInt16,
  liveText,
  micBlockReason,
  pcm16ToBytes,
  peakLevel,
  resampleLinear,
} from './asr-client.js'

describe('buildAsrWsUrl', () => {
  it('uses the same-origin bridge path with the page protocol', () => {
    const url = buildAsrWsUrl(ASR_WS_PATH, { sample_rate: 16000 }, { protocol: 'https:', host: 'agents.example.com' })
    expect(url).toBe('wss://agents.example.com/v1/asr/ws?sample_rate=16000')
  })

  it('uses ws: on http (localhost development)', () => {
    expect(buildAsrWsUrl(ASR_WS_PATH, {}, { protocol: 'http:', host: 'localhost:5173' }))
      .toBe('ws://localhost:5173/v1/asr/ws')
  })

  it('skips empty params', () => {
    expect(buildAsrWsUrl(ASR_WS_PATH, { sample_rate: 16000, last_seg_id: null }, { protocol: 'http:', host: 'h' }))
      .toBe('ws://h/v1/asr/ws?sample_rate=16000')
  })
})

describe('micBlockReason — the localhost/https rule', () => {
  const md = { mediaDevices: { getUserMedia: () => {} } }

  it('allows https', () => {
    expect(micBlockReason(md, { isSecureContext: true, location: { protocol: 'https:', hostname: 'x.example.com' } })).toBeNull()
  })

  it('allows http on localhost and 127.0.0.1', () => {
    for (const host of ['localhost', '127.0.0.1']) {
      expect(micBlockReason(md, { isSecureContext: true, location: { protocol: 'http:', hostname: host } })).toBeNull()
    }
  })

  it('blocks a plain http origin reached by IP or LAN name', () => {
    for (const host of ['10.0.0.5', 'agents.internal']) {
      expect(micBlockReason(md, { isSecureContext: false, location: { protocol: 'http:', hostname: host } }))
        .toBe('insecureContext')
    }
  })

  it('blocks when the browser has no mediaDevices (even on https)', () => {
    expect(micBlockReason({}, { isSecureContext: true, location: { protocol: 'https:', hostname: 'x' } })).toBe('unsupported')
  })
})

describe('resampleLinear', () => {
  it('is a no-op when the rate already matches', () => {
    const input = new Float32Array([0.5, -0.5])
    expect(resampleLinear(input, 16000, 16000)).toBe(input)
  })

  it('halves the sample count for 32k -> 16k', () => {
    const out = resampleLinear(new Float32Array([0, 1, 1, 0]), 32000, 16000)
    expect(out.length).toBe(2)
  })

  it('interpolates between samples when upsampling', () => {
    const out = resampleLinear(new Float32Array([0, 1]), 8000, 16000)
    expect(Array.from(out)).toEqual([0, 0.5, 1, 1])
  })
})

const tone = (freq, rate, n) => Float32Array.from({ length: n }, (_, i) => Math.sin(2 * Math.PI * freq * i / rate))
/** Peak amplitude, ignoring the first samples while the FIR window fills up. */
const steadyPeak = (arr, skip = 64) => {
  let peak = 0
  for (let i = skip; i < arr.length; i++) peak = Math.max(peak, Math.abs(arr[i]))
  return peak
}

describe('AntiAliasResampler — 48 kHz mic to 16 kHz engine', () => {
  // Downsampling without a low-pass is not "slightly lossy", it is wrong: every
  // component above 8 kHz reappears inside the speech band at full amplitude,
  // and the model cannot tell that junk from a fricative.  These numbers are
  // what plain linear interpolation used to measure (0 dB = full level).
  it('rejects out-of-band energy instead of folding it into the speech band', () => {
    for (const freq of [10000, 12000, 18000]) {
      const r = new AntiAliasResampler(48000, 16000)
      const out = r.process(tone(freq, 48000, 9600))
      expect(steadyPeak(out)).toBeLessThan(0.02)
    }
  })

  it('keeps in-band speech content at unity', () => {
    for (const freq of [200, 1000, 3000, 6000]) {
      const r = new AntiAliasResampler(48000, 16000)
      const out = r.process(tone(freq, 48000, 9600))
      expect(steadyPeak(out)).toBeGreaterThan(0.95)
      expect(steadyPeak(out)).toBeLessThan(1.05)
    }
  })

  it('carries filter state across blocks, so 4096-sample blocks do not click', () => {
    const input = tone(1200, 48000, 12000)
    const r1 = new AntiAliasResampler(48000, 16000)
    const oneShot = [...r1.process(input), ...r1.flush()]
    const r = new AntiAliasResampler(48000, 16000)
    const chunked = []
    for (let i = 0; i < input.length; i += 4096) chunked.push(...r.process(input.subarray(i, i + 4096)))
    chunked.push(...r.flush())
    expect(chunked.length).toBe(oneShot.length)
    for (let i = 0; i < oneShot.length; i++) expect(chunked[i]).toBeCloseTo(oneShot[i], 6)
  })

  it('handles non-integer ratios (44.1 kHz mics) and DC', () => {
    const r = new AntiAliasResampler(44100, 16000)
    expect(steadyPeak(r.process(tone(14000, 44100, 8820)))).toBeLessThan(0.02)
    const dc = new AntiAliasResampler(48000, 16000)
    const out = dc.process(Float32Array.from({ length: 4800 }, () => 0.5))
    expect(out[out.length - 1]).toBeCloseTo(0.5, 3)
  })

  it('is a no-op when the context already runs at the engine rate', () => {
    const r = new AntiAliasResampler(16000, 16000)
    const input = tone(3000, 16000, 1600)
    expect(Array.from(r.process(input))).toEqual(Array.from(input))
  })

  it('PcmStreamEncoder filters before framing, so the engine never sees the junk', () => {
    const encoder = new PcmStreamEncoder()
    const frames = encoder.push(tone(12000, 48000, 4800), 48000)
    expect(frames.length).toBe(4)          // 100 ms at 48 kHz, minus the filter's first window
    const bytes = frames[2]                        // steady-state frame, not the settling edge
    const pcm = new Int16Array(bytes.buffer)       // PCM16LE, same byte order as the wire
    let peak = 0
    for (const sample of pcm) peak = Math.max(peak, Math.abs(sample))
    expect(peak).toBeLessThan(700)         // full-scale tone -> ~-45 dBFS
  })
})

describe('PCM16 encoding', () => {
  it('scales to full scale and clamps out-of-range input', () => {
    // Symmetric scaling: +/-1 maps to +/-32767, so nothing can overflow.
    const out = floatToInt16(new Float32Array([2, -2, 0.5]))
    expect(Array.from(out)).toEqual([32767, -32767, 16384])
  })

  it('writes little-endian bytes', () => {
    expect(Array.from(pcm16ToBytes(new Int16Array([1, -1])))).toEqual([1, 0, 0xff, 0xff])
  })

  it('reports the peak amplitude for the level meter', () => {
    expect(peakLevel(new Float32Array([0.1, -0.7, 0.3]))).toBeCloseTo(0.7)
  })
})

describe('PcmStreamEncoder — mic blocks to 20 ms frames', () => {
  it('splits a 48 kHz block into 16 kHz frames of 320 samples (640 bytes)', () => {
    const enc = new PcmStreamEncoder({ targetRate: 16000, frameMs: 20 })
    const frames = enc.push(new Float32Array(480).fill(0.25), 48000)  // 10 ms of 48k audio
    expect(frames.length).toBe(0)  // 160 samples at 16k: not a full frame yet
    // The anti-alias filter holds back its first window (~24 input samples), so
    // frames lag the raw sample count slightly; finish() hands out the remainder.
    const more = enc.push(new Float32Array(960).fill(0.25), 48000)    // 20 ms more
    expect(more.length).toBe(1)
    expect(more[0].length).toBe(640)
    // 0.25 -> 8192 -> little-endian 0x2000; checked at the end of the frame
    // because the frame's first samples are still the filter's settling ramp.
    expect(Array.from(more[0].slice(-2))).toEqual([0x00, 0x20])
  })

  it('keeps the sub-frame remainder and hands it out on finish()', () => {
    const enc = new PcmStreamEncoder({ targetRate: 16000, frameMs: 20 })
    enc.push(new Float32Array(100), 16000)
    expect(enc.push(new Float32Array(100), 16000)).toEqual([])
    const tail = enc.finish()
    expect(tail.length).toBe(1)
    expect(tail[0].length).toBe(200 * 2)
    expect(enc.finish()).toEqual([])
  })

  it('produces exactly one frame per 20 ms at the native rate', () => {
    const enc = new PcmStreamEncoder({ targetRate: 16000, frameMs: 20 })
    const frames = enc.push(new Float32Array(1600), 16000)  // 100 ms
    expect(frames.length).toBe(5)
  })
})

describe('transcript folding', () => {
  it('partial replaces the current segment and stays live', () => {
    const st = createAsrTranscript()
    expect(applyAsrEvent(st, { type: 'partial', seg_id: 1, text: '你好' })).toEqual({ committed: '', live: '你好' })
    expect(applyAsrEvent(st, { type: 'partial', seg_id: 1, text: '你好吗' }).live).toBe('你好吗')
  })

  it('final commits exactly once and clears the live text', () => {
    const st = createAsrTranscript()
    applyAsrEvent(st, { type: 'partial', seg_id: 1, text: '你好' })
    expect(applyAsrEvent(st, { type: 'final', seg_id: 1, text: '你好。' })).toEqual({ committed: '你好。', live: '' })
    // A late duplicate must not insert the text twice.
    expect(applyAsrEvent(st, { type: 'final', seg_id: 1, text: '你好。' }).committed).toBe('')
    expect(st.lastSegId).toBe(1)
  })

  it('segment_cancel drops an uncommitted segment (noise shorter than min-speech)', () => {
    const st = createAsrTranscript()
    applyAsrEvent(st, { type: 'partial', seg_id: 3, text: '呃' })
    expect(applyAsrEvent(st, { type: 'segment_cancel', seg_id: 3 })).toEqual({ committed: '', live: '' })
    expect(liveText(st)).toBe('')
  })

  it('keeps a committed segment when a later one is cancelled', () => {
    const st = createAsrTranscript()
    applyAsrEvent(st, { type: 'final', seg_id: 1, text: '第一句。' })
    applyAsrEvent(st, { type: 'partial', seg_id: 2, text: '第' })
    applyAsrEvent(st, { type: 'segment_cancel', seg_id: 2 })
    expect(st.order).toEqual([1])
  })

  it('replay (reconnect catch-up) commits without a preceding partial', () => {
    const st = createAsrTranscript()
    expect(applyAsrEvent(st, { type: 'replay', seg_id: 7, text: '补发的句子。' }).committed).toBe('补发的句子。')
  })

  it('ignores control events', () => {
    const st = createAsrTranscript()
    expect(applyAsrEvent(st, { type: 'ready', engine: 'paraformer-streaming' })).toEqual({ committed: '', live: '' })
    expect(applyAsrEvent(st, { type: 'pong' })).toEqual({ committed: '', live: '' })
  })
})

describe('why a recording produced no text', () => {
  it('names "no audio left the browser" when nothing was sent', () => {
    expect(diagnoseEmptyResult({ audioMs: 0, sawResult: false })).toBe('noAudio')
  })

  it('stays quiet about a blip too short to contain a syllable', () => {
    expect(diagnoseEmptyResult({ audioMs: 200, sawResult: false })).toBeNull()
  })

  it('names "no speech in the audio" when audio went up and nothing came back', () => {
    expect(diagnoseEmptyResult({ audioMs: 4000, sawResult: false })).toBe('noResult')
  })

  it('says nothing when the service did answer', () => {
    expect(diagnoseEmptyResult({ audioMs: 4000, sawResult: true })).toBeNull()
  })

  it('flush_ack with no results reports the reason once', () => {
    const errors = []
    const session = new AsrVoiceSession({ onError: (code) => errors.push(code) })
    session._audioMs = 3000
    session._handleFrame(JSON.stringify({ type: 'flush_ack' }))
    expect(errors).toEqual(['noResult'])
  })

  it('flush_ack after a partial reports nothing', () => {
    const errors = []
    const session = new AsrVoiceSession({ onError: (code) => errors.push(code) })
    session._audioMs = 3000
    session._handleFrame(JSON.stringify({ type: 'partial', seg_id: 1, text: '你好' }))
    session._handleFrame(JSON.stringify({ type: 'flush_ack' }))
    expect(errors).toEqual([])
  })
})

describe('capture watchdog — "the button is live but nothing is happening"', () => {
  function recordingSession(errors) {
    const s = new AsrVoiceSession({ onError: (code) => errors.push(code) })
    s.state = 'recording'
    s._lastBlockAt = Date.now()
    return s
  }

  it('reports when the browser stopped delivering mic blocks', () => {
    const errors = []
    const s = recordingSession(errors)
    s._lastBlockAt = Date.now() - 4000
    s._watchdog()
    expect(errors).toEqual(['micStalled'])
  })

  it('says nothing while blocks keep arriving', () => {
    const errors = []
    recordingSession(errors)._watchdog()
    expect(errors).toEqual([])
  })

  it('reports loud mic audio that never opened a segment', () => {
    const errors = []
    const s = recordingSession(errors)
    s._loudMs = 4500
    s._watchdog()
    expect(errors).toEqual(['noSpeechDetected'])
  })

  it('stays quiet once the service has opened a segment', () => {
    const errors = []
    const s = recordingSession(errors)
    s._handleFrame(JSON.stringify({ type: 'segment_start', seg_id: 1, start_ms: 0 }))
    s._loudMs = 9000
    s._watchdog()
    expect(errors).toEqual([])
  })

  it('reports each problem at most once per recording', () => {
    const errors = []
    const s = recordingSession(errors)
    s._lastBlockAt = Date.now() - 4000
    s._watchdog()
    s._watchdog()
    expect(errors).toEqual(['micStalled'])
  })

  it('does not fire outside the recording state', () => {
    const errors = []
    const s = new AsrVoiceSession({ onError: (code) => errors.push(code) })
    s.state = 'finalizing'
    s._lastBlockAt = Date.now() - 9000
    s._watchdog()
    expect(errors).toEqual([])
  })
})
