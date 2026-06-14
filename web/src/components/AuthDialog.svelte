<script>
  import { authDialog, submitAuthLogin, cancelAuthLogin } from '../lib/auth-state.svelte.js'

  function handleKeydown(e) {
    if (e.key === 'Enter') submitAuthLogin()
    else if (e.key === 'Escape') cancelAuthLogin()
  }
</script>

{#if authDialog.open}
  <div class="auth-backdrop" role="presentation">
    <div class="auth-dialog" role="dialog" aria-modal="true" aria-label="Authentication required">
      <h2>需要授权</h2>
      <p class="hint">请输入访问密码后继续。</p>
      <input
        class="password-input"
        type="password"
        bind:value={authDialog.password}
        onkeydown={handleKeydown}
        placeholder="密码"
      />
      {#if authDialog.error}
        <div class="error">{authDialog.error}</div>
      {/if}
      <div class="actions">
        <button class="btn secondary" onclick={cancelAuthLogin} disabled={authDialog.submitting}>取消</button>
        <button class="btn primary" onclick={submitAuthLogin} disabled={authDialog.submitting || !authDialog.password}>
          {authDialog.submitting ? '登录中...' : '登录'}
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
  h2 {
    margin: 0 0 8px;
    font-size: 1.25rem;
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
