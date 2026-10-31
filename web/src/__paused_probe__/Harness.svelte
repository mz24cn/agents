<script>
  // Mirrors ChatPage's paused-state state machine. Regression invariant: in
  // this Svelte version $state Sets do NOT track in-place .add/.delete/.clear
  // (proxy.js only proxies property writes and array methods), so every
  // mutation here MUST be copy-then-reassign, exactly like ChatPage's
  // markPaused()/markPauseRequested() helpers do.
  import ChatInput from '../components/chat/ChatInput.svelte'
  let { sessionId, eventsRef } = $props()
  let backendPausedSessions = $state(new Set())
  let pauseRequestedSessions = $state(new Set())
  let isStreaming = $state(true)
  let isPaused = $derived(!!sessionId && backendPausedSessions.has(sessionId))
  let pauseRequested = $derived(!!sessionId && pauseRequestedSessions.has(sessionId))
  eventsRef.events = ({ status }) => {
    if (status === 'paused') {
      isStreaming = false
      if (!backendPausedSessions.has(sessionId)) {
        const next = new Set(backendPausedSessions)
        next.add(sessionId)
        backendPausedSessions = next
      }
    }
  }
</script>

<ChatInput isStreaming={isStreaming} isPaused={isPaused} pauseRequested={pauseRequested}
  onPause={() => {}} onStop={() => {}} onStopForce={() => {}} onResume={() => {}} onEndPause={() => {}}
  onInterject={() => {}} onSend={() => {}} canPause={true} text="" />
