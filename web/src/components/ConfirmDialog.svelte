<script>
  import { t } from '../lib/i18n.svelte.js'
  let {
    open,
    title,
    message = '',
    confirmText = t('confirm'),
    cancelText = t('cancel'),
    closeText = '',
    customText = '',
    onCustom = null,
    onConfirm,
    onCancel,
    hideCancel = false,
  } = $props()

  $effect(() => {
    if (!open) return
    const handler = (e) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  })
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="overlay" onclick={onCancel}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="dialog" onclick={(e) => e.stopPropagation()}>
      <h3 class="dialog-title">{title}</h3>
      {#if message}
        <p class="dialog-message">{message}</p>
      {/if}
      <div class="dialog-actions">
        {#if !hideCancel}
        <button class="btn btn-cancel" onclick={onCancel}>{cancelText}</button>
        {/if}
        {#if customText && onCustom}
          <button class="btn btn-custom" onclick={onCustom}>{customText}</button>
        {/if}
        {#if closeText}
          <button class="btn btn-close" onclick={onConfirm}>{closeText}</button>
        {:else}
          <button class="btn btn-confirm" onclick={onConfirm}>{confirmText}</button>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .dialog {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px;
    min-width: 320px;
    max-width: 90vw;
  }
  .dialog-title {
    margin: 0 0 20px 0;
    font-size: 1.1rem;
    color: var(--text);
  }
  .dialog-message {
    margin: -8px 0 20px 0;
    color: var(--text-secondary);
    line-height: 1.5;
    white-space: pre-wrap;
  }
  .dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
  }
  .btn {
    padding: 8px 20px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .btn-cancel {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-cancel:hover {
    opacity: 0.8;
  }
  .btn-custom {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-custom:hover {
    opacity: 0.8;
  }
  .btn-close {
    background: var(--bg-secondary);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-close:hover {
    opacity: 0.8;
  }
  .btn-confirm {
    background: var(--danger);
    color: #fff;
  }
  .btn-confirm:hover {
    background: var(--danger-hover);
  }
</style>
