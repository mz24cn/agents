<script>
  import { authDialog, submitAuthLogin, cancelAuthLogin } from '../lib/auth-state.svelte.js'
  import { LANGS, i18n, setLang, t } from '../lib/i18n.svelte.js'

  function handleKeydown(e) {
    if (e.key === 'Enter') submitAuthLogin()
    else if (e.key === 'Escape') cancelAuthLogin()
  }
</script>

{#if authDialog.open}
  <div class="auth-backdrop" role="presentation">
    <div class="auth-dialog" role="dialog" aria-modal="true" aria-label={t('authRequiredLabel')}>
      <div class="auth-header">
        <h2>{t('authRequiredTitle')}</h2>
        <div class="lang-switch" aria-label={t('languageSwitch')}>
          {#each LANGS as lang}
            <button
              type="button"
              class:active={i18n.lang === lang.code}
              aria-pressed={i18n.lang === lang.code}
              onclick={() => setLang(lang.code)}
            >{lang.label}</button>
          {/each}
        </div>
      </div>
      <p class="hint">{t('authRequiredHint')}</p>
      <input
        class="password-input"
        type="password"
        bind:value={authDialog.password}
        onkeydown={handleKeydown}
        placeholder={t('authPasswordPlaceholder')}
      />
      {#if authDialog.error}
        <div class="error">{authDialog.error}</div>
      {/if}
      <div class="actions">
        <button class="btn secondary" onclick={cancelAuthLogin} disabled={authDialog.submitting}>{t('cancel')}</button>
        <button class="btn primary" onclick={submitAuthLogin} disabled={authDialog.submitting || !authDialog.password}>
          {authDialog.submitting ? t('authLoggingIn') : t('authLogin')}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .auth-backdrop {
    position: fixed;
    inset: 0;
    z-index: 10000;
    background: rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 20px;
  }
  .auth-dialog {
    width: min(420px, 100%);
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 18px 60px rgba(0,0,0,0.28);
    padding: 22px;
  }
  .auth-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
  }
  h2 {
    margin: 0;
    font-size: 1.25rem;
  }
  .lang-switch {
    display: inline-flex;
    flex-shrink: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-secondary);
  }
  .lang-switch button {
    border: none;
    border-radius: 0;
    padding: 4px 8px;
    cursor: pointer;
    background: transparent;
    color: var(--text-secondary);
    font-size: 0.78rem;
  }
  .lang-switch button.active {
    background: var(--primary);
    color: white;
  }
  .hint {
    margin: 0 0 16px;
    color: var(--text-secondary);
    font-size: 0.92rem;
  }
  .password-input {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg-secondary);
    color: var(--text);
    padding: 10px 12px;
    font-size: 1rem;
    outline: none;
  }
  .password-input:focus {
    border-color: var(--primary);
  }
  .error {
    margin-top: 10px;
    color: var(--danger);
    font-size: 0.9rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 18px;
  }
  .btn {
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 8px 14px;
    cursor: pointer;
    background: var(--bg-secondary);
    color: var(--text);
  }
  .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .btn.primary {
    border-color: var(--primary);
    background: var(--primary);
    color: white;
  }
  .btn.secondary:hover:not(:disabled) {
    background: var(--border);
  }
</style>
