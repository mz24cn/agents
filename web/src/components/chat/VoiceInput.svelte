<script>
  /**
   * Microphone button for the chat input: streams speech to the ASR service
   * (accessories/ASR) and inserts the recognized text into the message box.
   *
   * It is deliberately a *transcription* control, not a recorder: nothing is
   * attached to the message, the words land in the text box where the user can
   * still edit them, add file references, or cancel.
   *
   * Two independent preconditions are reported separately, because they send
   * the user to different fixes:
   *   - browser side: getUserMedia needs a secure context (https, or
   *     http://localhost) — see micBlockReason();
   *   - server side: the ASR service must be up (GET /v1/asr/health).
   */
  import { onDestroy } from 'svelte'
  import { t } from '../../lib/i18n.svelte.js'
  import { ASR_HEALTH_PATH, AsrVoiceSession, micBlockReason } from '../../lib/asr-client.js'

  let { disabled = false, onCommit = () => {}, onPartial = () => {}, onError = () => {} } = $props()

  /** idle | connecting | recording | finalizing */
  let phase = $state('idle')
  /** null = not probed yet, else the /v1/asr/health payload */
  let service = $state(null)
  /** null | 'insecureContext' | 'unsupported' */
  let blockReason = $state(micBlockReason())
  let level = $state(0)
  let engine = $state('')

  /**
   * Voice input is a deployment option (AGENTS_ASR_URL): with no ASR service
   * configured there is nothing to say about it, so the button is not shown at
   * all. It appears once the probe answers, which keeps it from appearing and
   * then vanishing.
   */
  const hidden = $derived(service !== null && service.configured === false)

  /** Live instance exports (reached through bind:this from ChatInput). */
  export function stop({ discard = false } = {}) {
    // `discard` is what sending a message asks for: the flush is still sent,
    // but a trailing segment must not repopulate the box that was just cleared.
    if (discard) discardTranscript = true
    if (phase === 'recording' || phase === 'connecting') session?.stop()
  }

  export function recording() {
    return phase === 'recording' || phase === 'connecting'
  }

  let session = null
  let discardTranscript = false

  /** Error codes we translate; anything else is shown in the service's words. */
  const ERROR_KEYS = {
    asr_unreachable: 'voiceServiceDown',
    not_configured: 'voiceNotConfigured',
    asrDisconnected: 'asrDisconnected',
    asrUnauthorized: 'asrUnauthorized',
    // "the button worked but nothing appeared" -- named, because otherwise the
    // user blames the model while the microphone is muted on that machine.
    noAudio: 'voiceNoAudio',
    noResult: 'voiceNoResult',
    // mid-recording diagnostics (AsrVoiceSession._watchdog)
    micStalled: 'voiceMicStalled',
    noSpeechDetected: 'voiceNoSpeechDetected',
  }

  async function probeService() {
    try {
      const resp = await fetch(ASR_HEALTH_PATH, { credentials: 'same-origin' })
      const data = await resp.json()
      service = data && typeof data === 'object' ? data : { available: false }
      engine = service.engine || ''
    } catch {
      service = { available: false }
    }
  }
  probeService()

  const serviceDown = $derived(service !== null && service.available === false)
  const blocked = $derived(blockReason !== null)

  const hint = $derived.by(() => {
    if (blockReason === 'insecureContext') {
      const origin = typeof location !== 'undefined' ? location.origin : ''
      return t('voiceInsecureContext', { origin })
    }
    if (blockReason === 'unsupported') return t('voiceUnsupported')
    if (phase === 'connecting') return t('voiceConnecting')
    if (phase === 'finalizing') return t('voiceFinalizing')
    if (phase === 'recording') return t('voiceListening')
    if (serviceDown) return t('voiceServiceDown')
    if (engine) return t('voiceEngineReady', { engine })
    return t('voiceInput')
  })

  function reportError(message) {
    onError?.(message)
  }

  async function start() {
    blockReason = micBlockReason()
    if (blockReason) {
      // Do not call getUserMedia: the browser would answer with a generic
      // permission error and the user would chase the wrong fix.
      reportError(hint)
      return
    }
    discardTranscript = false
    session = new AsrVoiceSession({
      onStatus: (next) => { phase = next },
      onTranscript: ({ committed, live }) => {
        if (discardTranscript) return
        // A finalized segment settles where its partial already sits; the live
        // run then follows the caret for the next one.
        if (committed) onCommit(committed)
        onPartial(live)
      },
      onLevel: (value) => { level = value },
      onEvent: (evt) => {
        if (evt?.type === 'ready' && evt.engine) engine = evt.engine
      },
      onError: (code, message) => {
        if (code === 'asr_unreachable') {
          service = { available: false }
          reportError(t('voiceServiceDown'))
          return
        }
        const key = ERROR_KEYS[code]
        reportError(key ? t(key) : (message || t(code)))
      },
    })
    try {
      await session.start()
    } catch (err) {
      const name = err?.name
      if (name === 'NotAllowedError' || name === 'SecurityError') reportError(t('voiceMicDenied'))
      else if (name === 'NotFoundError' || name === 'OverconstrainedError') reportError(t('voiceMicMissing'))
      else if (name === 'AudioContextSuspended') {
        // Mic granted, socket open, but the browser refuses to run the audio
        // graph: no sample is ever captured.  Nothing to do with the service.
        reportError(t('voiceAudioBlocked'))
      }
      else {
        // Socket could not be opened: the service is almost certainly down.
        service = { available: false }
        reportError(t('voiceServiceDown'))
      }
      session = null
      phase = 'idle'
      onPartial('')
    }
  }

  function toggle() {
    if (phase === 'idle') {
      start()
    } else if (phase === 'recording') {
      session?.stop()
    } else if (phase === 'connecting') {
      session?.abort()
      session = null
      phase = 'idle'
      onPartial('')
    }
    // 'finalizing' is a ~1s tail; clicking during it does nothing.
  }

  onDestroy(() => {
    try { session?.abort() } catch { /* already torn down */ }
    session = null
  })
</script>

{#if !hidden}
  <button
    class="send-btn voice-btn"
    class:recording={phase === 'recording'}
    class:busy={phase === 'connecting' || phase === 'finalizing'}
    class:off={blocked || serviceDown}
    onclick={toggle}
    onmousedown={(e) => {
      // Keep the caret/selection in the message box: recognized words are
      // written where the cursor is, and a selection is what gets replaced.
      e.preventDefault()
    }}
    disabled={disabled}
    title={hint}
    aria-label={t('voiceInput')}
    aria-pressed={phase === 'recording'}
  >
    {#if phase === 'finalizing'}
      ⋯
    {:else}
      🎤
      {#if phase === 'recording'}
        <span class="level" style="transform: scaleX({Math.min(1, level * 3).toFixed(2)})"></span>
      {/if}
    {/if}
  </button>
{/if}

<style>
  .voice-btn {
    position: relative;
    overflow: hidden;
    background: var(--bg-secondary);
    color: var(--text-secondary);
    border: 1px solid var(--border);
    font-size: 0.85rem;
  }
  .voice-btn:hover:not(:disabled) {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
  /* ASR service down, or the origin cannot use the microphone: still clickable
     (the tooltip says why), but visibly not ready. */
  .voice-btn.off { opacity: 0.45; }
  .voice-btn.recording {
    background: var(--danger, #e53e3e);
    border-color: var(--danger, #e53e3e);
    color: #fff;
    animation: voice-pulse 1.4s ease-in-out infinite;
  }
  .voice-btn.busy { opacity: 0.7; }
  .level {
    position: absolute;
    left: 3px;
    right: 3px;
    bottom: 2px;
    height: 2px;
    background: #fff;
    border-radius: 1px;
    transform-origin: left center;
    transform: scaleX(0);
  }
  @keyframes voice-pulse {
    0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--danger, #e53e3e) 55%, transparent); }
    50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger, #e53e3e) 25%, transparent); }
  }
</style>
