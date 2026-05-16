<script>
  import { t } from '../lib/i18n.svelte.js'
  let {
    open,
    title,
    message = '',
    confirmText = t('confirm'),
    cancelText = t('cancel'),
    customText = '',
    onCustom = null,
    onConfirm,
    onCancel,
  } = $props()
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="overlay" onclick={onCancel} onkeydown={(e) => e.key === 'Escape' && onCancel()}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="dialog" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <h3 class="dialog-title">{title}</h3>
      {#if message}
        <p class="dialog-message">{message}</p>
      {/if}
      <div class="dialog-actions">
        <button class="btn btn-cancel" onclick={onCancel}>{cancelText}</button>
        {#if customText && onCustom}
          <button class="btn btn-custom" onclick={onCustom}>{customText}</button>
        {/if}
        <button class="btn btn-confirm" onclick={onConfirm}>{confirmText}</button>
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
  .btn-confirm {
    background: var(--danger);
    color: #fff;
  }
  .btn-confirm:hover {
    background: var(--danger-hover);
  }
</style>
