<script>
  import { t } from '../../lib/i18n.svelte.js'

  let { onAudio = () => {} } = $props()

  let isRecording = $state(false)
  let duration = $state(0)
  let unsupported = $state(false)
  let permissionDenied = $state(false)

  let mediaRecorder = null
  let chunks = []
  let timer = null

  function checkSupport() {
    return typeof MediaRecorder !== 'undefined' && navigator.mediaDevices?.getUserMedia
  }

  async function startRecording() {
    if (!checkSupport()) {
      unsupported = true
      return
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      mediaRecorder = new MediaRecorder(stream)
      chunks = []

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data)
      }

      mediaRecorder.onstop = () => {
        const blob = new Blob(chunks, { type: mediaRecorder.mimeType || 'audio/webm' })
        const reader = new FileReader()
        reader.onloadend = () => {
          const base64 = reader.result.split(',')[1] || ''
          onAudio(base64)
        }
        reader.readAsDataURL(blob)
        stream.getTracks().forEach(t => t.stop())
      }

      mediaRecorder.start()
      isRecording = true
      duration = 0
      permissionDenied = false
      timer = setInterval(() => { duration++ }, 1000)
    } catch (err) {
      permissionDenied = true
    }
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop()
    }
    isRecording = false
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  function formatDuration(s) {
    const m = Math.floor(s / 60)
    const sec = s % 60
    return `${m}:${sec.toString().padStart(2, '0')}`
  }
</script>

<div class="audio-recorder">
  {#if unsupported || permissionDenied}
    <span class="unavailable-msg">{t('audioUnavailable')}</span>
  {:else if isRecording}
    <span class="recording-indicator">
      <span class="rec-dot"></span>
      {formatDuration(duration)}
    </span>
    <button
      class="stop-btn"
      onclick={stopRecording}
      type="button"
      title={t('stopRecording')}
    >⏹</button>
  {:else}
    <button
      class="mic-btn"
      onclick={startRecording}
      type="button"
      title={t('startRecording')}
    >🎙️</button>
  {/if}
</div>

<style>
  .audio-recorder {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .mic-btn, .stop-btn {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .mic-btn:hover {
    background: var(--border);
  }
  .stop-btn {
    background: var(--danger, #e74c3c);
    color: #fff;
    border-color: var(--danger, #e74c3c);
  }
  .stop-btn:hover {
    opacity: 0.85;
  }
  .recording-indicator {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 0.85rem;
    color: var(--danger, #e74c3c);
    font-variant-numeric: tabular-nums;
  }
  .rec-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--danger, #e74c3c);
    animation: blink 1s ease-in-out infinite;
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  .unavailable-msg {
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
</style>
