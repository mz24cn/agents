<script>
  import { t } from '../../lib/i18n.svelte.js'
  import ConfirmDialog from '../ConfirmDialog.svelte'

  /**
   * The AskUser question, asked where the user actually is.
   *
   * The agent service registered AskUser as a client-side tool, so a call to it
   * is not executed by the service: it comes back to us and this dialog *is* the
   * tool.  `request` is `{ question, options, timeout, defaultOption }`; the
   * outcome is reported through `onChoose({ value, timedOut, auto, freeText })`
   * and the caller turns it into the `role: 'tool'` answer.
   *
   * Built on ConfirmDialog so it inherits the overlay, the card, Escape-to-dismiss
   * and the theme; the options replace the usual two buttons because they are the
   * choices, not an OK/Cancel decoration.
   */
  let { open, request = null, onChoose, onCancel, otherWaiting = 0 } = $props()

  let highlight = $state(0)
  let remaining = $state(0)
  let freeText = $state(false)
  let freeValue = $state('')
  let answered = $state(false)

  const options = $derived(request?.options ?? [])
  const defaultOption = $derived(request?.defaultOption ?? '')
  const timeout = $derived(request?.timeout ?? 0)

  function choose(value, meta = {}) {
    if (answered || !open) return
    answered = true
    onChoose?.({ value, ...meta })
  }

  function dismiss() {
    choose(null, { dismissed: true })
  }

  // A new question starts clean: the default option is pre-highlighted, and a
  // positive timeout starts its countdown (0 means "wait as long as it takes").
  $effect(() => {
    if (!open || !request) return
    answered = false
    freeText = false
    freeValue = ''
    const preset = options.findIndex((option) => option === defaultOption)
    highlight = preset >= 0 ? preset : 0
    remaining = timeout
    if (timeout <= 0) return
    const startedAt = Date.now()
    const timer = setInterval(() => {
      const left = timeout - Math.floor((Date.now() - startedAt) / 1000)
      if (left > 0) {
        remaining = left
        return
      }
      remaining = 0
      clearInterval(timer)
      // Only auto-select when the model said which option is safe without an
      // answer; otherwise let the countdown end and report "no answer".
      choose(defaultOption || null, { timedOut: true, auto: Boolean(defaultOption) })
    }, 250)
    return () => clearInterval(timer)
  })

  $effect(() => {
    if (!open) return
    const handler = (event) => {
      if (event.key === 'Tab' && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault()
        freeText = !freeText
        return
      }
      if (freeText) {
        if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault()
          choose(freeValue.trim() || null, { freeText: true })
        }
        return
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        if (!options.length) return
        const step = event.key === 'ArrowDown' ? 1 : -1
        highlight = (highlight + step + options.length) % options.length
        return
      }
      if (event.key === 'Enter') {
        event.preventDefault()
        if (options[highlight] !== undefined) choose(options[highlight])
        return
      }
      const digit = Number.parseInt(event.key, 10)
      if (Number.isFinite(digit) && digit >= 1 && digit <= options.length) {
        event.preventDefault()
        highlight = digit - 1
        choose(options[digit - 1])
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  })

  const timerText = $derived(
    timeout > 0 ? t('askUserCountdown', { seconds: remaining }) : t('askUserWaitForever'),
  )
</script>

<ConfirmDialog
  {open}
  title={t('askUserTitle')}
  hideActions={true}
  onEscape={dismiss}
  onCancel={dismiss}
>
  {#if request}
    <div class="ask">
      <p class="ask-question">{request.question}</p>
      <div class="ask-options">
        {#each options as option, index (index)}
          <button
            class="ask-option"
            class:selected={index === highlight}
            onclick={() => { highlight = index; choose(option) }}
            onmouseenter={() => { highlight = index }}
          >
            <span class="ask-key">{index + 1}</span>
            <span class="ask-text">{option}</span>
            {#if option === defaultOption && timeout > 0}
              <span class="ask-default">{t('askUserDefaultBadge')}</span>
            {/if}
          </button>
        {/each}
      </div>
      {#if freeText}
        <!-- svelte-ignore a11y_autofocus -->
        <textarea
          class="ask-free"
          rows="3"
          placeholder={t('askUserFreeTextPlaceholder')}
          bind:value={freeValue}
          autofocus
        ></textarea>
        <div class="ask-free-actions">
          <button class="btn btn-cancel" onclick={() => { freeText = false }}>{t('cancel')}</button>
          <button class="btn btn-send" onclick={() => choose(freeValue.trim() || null, { freeText: true })}>
            {t('send')}
          </button>
        </div>
      {/if}
      <div class="ask-foot">
        <span class="ask-timer" class:urgent={timeout > 0 && remaining <= 10}>{timerText}</span>
        <div class="ask-foot-actions">
          <button class="link-btn" onclick={() => { freeText = !freeText }}>{t('askUserOther')}</button>
          <button class="btn btn-cancel" onclick={dismiss}>{t('askUserDismiss')}</button>
        </div>
      </div>
      <p class="ask-hint">{t('askUserKeysHint')}</p>
      {#if otherWaiting > 0}
        <!-- 其它会话也在等你回答：问题不会因为切走而消失，但先说清楚还有几个 -->
        <p class="ask-queued">{t('askUserQueuedOther').replace('{count}', String(otherWaiting))}</p>
      {/if}
    </div>
  {/if}
</ConfirmDialog>

<style>
  .ask {
    min-width: 340px;
  }
  .ask-question {
    margin: 0 0 16px 0;
    color: var(--text);
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .ask-options {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 46vh;
    overflow-y: auto;
  }
  .ask-option {
    display: flex;
    align-items: center;
    gap: 10px;
    text-align: left;
    padding: 10px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    cursor: pointer;
    font-size: 0.9rem;
  }
  .ask-option:hover,
  .ask-option.selected {
    border-color: var(--primary);
    background: var(--bg-hover, var(--bg-secondary));
  }
  .ask-key {
    flex: none;
    width: 20px;
    height: 20px;
    line-height: 20px;
    text-align: center;
    border-radius: 4px;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    font-size: 0.75rem;
  }
  .ask-text {
    flex: 1;
    word-break: break-word;
  }
  .ask-default {
    flex: none;
    font-size: 0.7rem;
    color: var(--primary);
    border: 1px solid var(--primary);
    border-radius: 4px;
    padding: 1px 6px;
  }
  .ask-free {
    margin-top: 12px;
    width: 100%;
    box-sizing: border-box;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg-secondary);
    color: var(--text);
    font: inherit;
    resize: vertical;
  }
  .ask-free-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 8px;
  }
  .ask-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 16px;
  }
  .ask-timer {
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
  .ask-timer.urgent {
    color: var(--danger);
  }
  .ask-foot-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .link-btn {
    background: none;
    border: none;
    color: var(--primary);
    cursor: pointer;
    font-size: 0.85rem;
    padding: 4px 2px;
  }
  .ask-hint {
    margin: 10px 0 0 0;
    color: var(--text-secondary);
    font-size: 0.75rem;
  }
  .ask-queued {
    margin: 6px 0 0 0;
    color: #d97706;
    font-size: 0.75rem;
  }
  .btn {
    padding: 6px 16px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .btn-cancel {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-send {
    background: var(--primary);
    color: #fff;
  }
</style>
