<script>
  import { onMount } from 'svelte'
  import { auth, build } from '../../lib/api.js'
  import { copyToClipboard } from '../../lib/clipboard.js'
  import { t } from '../../lib/i18n.svelte.js'

  /** @type {string} Injected at build time by Vite (YYMMdd_HHmmss) */
  const frontendBuild = typeof __BUILD_TIME__ !== 'undefined' ? __BUILD_TIME__ : ''

  const ttlOptions = [
    { value: 3600, labelKey: 'authTtl1Hour' },
    { value: 21600, labelKey: 'authTtl6Hours' },
    { value: 43200, labelKey: 'authTtl12Hours' },
    { value: 86400, labelKey: 'authTtl1Day' },
    { value: 604800, labelKey: 'authTtl7Days' },
    { value: 2592000, labelKey: 'authTtl30Days' },
    { value: 7776000, labelKey: 'authTtl90Days' },
    { value: 15552000, labelKey: 'authTtl180Days' },
    { value: 31536000, labelKey: 'authTtl1Year' },
  ]

  let loading = $state(false)
  let saving = $state(false)
  let error = $state('')
  let message = $state('')
  let hasPassword = $state(false)
  let password = $state('')
  let cookieTtl = $state(604800)
  let apiKey = $state('')
  let setupToken = $state('')
  let setupTokenExpiresAt = $state('')
  let backendBuild = $state('')

  let setupLink = $derived(`${window.location.origin}/v1/setup${setupToken ? `?token=${encodeURIComponent(setupToken)}` : ''}`)
  let windowsSetupCommand = $derived(`irm ${setupLink} | iex`)
  let linuxSetupCommand = $derived(`curl -fsSL ${setupLink} | sh`)
  let setupTokenExpiresDisplay = $derived(formatSetupTokenExpiresAt(setupTokenExpiresAt))

  function pad2(value) {
    return String(value).padStart(2, '0')
  }

  function formatTimezoneOffset(date) {
    const offsetMinutes = -date.getTimezoneOffset()
    const sign = offsetMinutes >= 0 ? '+' : '-'
    const absMinutes = Math.abs(offsetMinutes)
    const hours = Math.floor(absMinutes / 60)
    const minutes = absMinutes % 60
    return `UTC${sign}${pad2(hours)}:${pad2(minutes)}`
  }

  function formatSetupTokenExpiresAt(value) {
    if (!value) return ''

    const date = new Date(value)
    if (Number.isNaN(date.getTime())) {
      return value
    }

    const localTime = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
    const timezoneLabel = timezone ? `${timezone}, ${formatTimezoneOffset(date)}` : formatTimezoneOffset(date)
    return t('authSetupTokenExpiresDisplay', { time: localTime, timezone: timezoneLabel })
  }

  function authSettingsError(err, fallbackKey) {
    if (err?.code === 'invalid_password_format') return t('authInvalidPasswordFormat')
    if (err?.code === 'invalid_cookie_ttl') return t('authInvalidCookieTtl')
    if (err?.code === 'disable_auth_failed') return t('authDisableFailed')
    return err?.status ? t(fallbackKey) : (err?.message || t(fallbackKey))
  }

  onMount(() => {
    loadConfig()
    loadBuildInfo()
  })

  async function loadBuildInfo() {
    try {
      const data = await build.info()
      backendBuild = data.backend_build || ''
    } catch {
      // ignore \u2014 backendBuild stays empty
    }
  }

  async function loadConfig() {
    loading = true
    error = ''
    message = ''
    apiKey = ''
    try {
      const data = await auth.config()
      hasPassword = !!data.has_password
      cookieTtl = data.cookie_ttl_seconds || 604800
      setupToken = data.setup_token || ''
      setupTokenExpiresAt = data.setup_token_expires_at || ''
    } catch (err) {
      error = authSettingsError(err, 'authLoadFailed')
    } finally {
      loading = false
    }
  }

  async function saveConfig() {
    saving = true
    error = ''
    message = ''
    apiKey = ''
    try {
      const payload = { cookie_ttl_seconds: Number(cookieTtl) }
      if (password) payload.password = password
      const data = await auth.updateConfig(payload)
      hasPassword = !!data.has_password
      cookieTtl = data.cookie_ttl_seconds || cookieTtl
      setupToken = data.setup_token || ''
      setupTokenExpiresAt = data.setup_token_expires_at || ''
      apiKey = data.api_key || ''
      password = ''
      message = apiKey ? t('authEnabledSavedMessage') : t('authSettingsSavedMessage')
    } catch (err) {
      error = authSettingsError(err, 'authSaveFailed')
    } finally {
      saving = false
    }
  }

  async function logout() {
    saving = true
    error = ''
    message = ''
    apiKey = ''
    try {
      await auth.logout()
      message = t('authLogoutSuccessMessage')
    } catch (err) {
      error = authSettingsError(err, 'authLogoutFailed')
    } finally {
      saving = false
    }
  }

  async function disableAuth() {
    if (!window.confirm(t('authDisableConfirm'))) return
    saving = true
    error = ''
    message = ''
    apiKey = ''
    try {
      const data = await auth.disable()
      hasPassword = !!data.has_password
      cookieTtl = data.cookie_ttl_seconds || 604800
      setupToken = ''
      setupTokenExpiresAt = ''
      password = ''
      message = t('authDisabledMessage')
    } catch (err) {
      error = authSettingsError(err, 'authDisableFailed')
    } finally {
      saving = false
    }
  }

  async function copyText(text) {
    if (!text) return
    error = ''
    const ok = await copyToClipboard(text)
    if (ok) {
      message = t('copiedToClipboard')
    } else {
      error = t('copyFailedManual')
    }
  }
</script>

<div class="auth-page">
  <div class="page-content">
    <div class="page-heading">
      <div>
        <h2>{t('authSettingsTitle')}</h2>
        <p>{t('authSettingsSubtitle')}</p>
      </div>
      <div class="status-pill" class:enabled={hasPassword}>
        <span class="dot"></span>
        {hasPassword ? t('authEnabled') : t('authNotConfigured')}
      </div>
    </div>

    {#if loading}
      <div class="loading">{t('loading')}</div>
    {:else}
      {#if message}
        <div class="success-msg">{message}</div>
      {/if}
      {#if error}
        <div class="error-msg">{error}</div>
      {/if}

      {#if apiKey}
        <section class="api-key-card">
          <div class="card-header">
            <div>
              <h3>{t('authApiKeyOneTimeTitle')}</h3>
              <p>{t('authApiKeyOneTimeHint')}</p>
            </div>
            <button class="btn btn-primary" type="button" onclick={() => copyText(apiKey)}>{t('copyApiKey')}</button>
          </div>
          <input class="code-input" readonly value={apiKey} aria-label="API Key" />
        </section>
      {/if}

      <div class="settings-stack">

        {#if frontendBuild || backendBuild}
          <section class="card build-card">
            <div class="card-header compact">
              <div>
                <h3>Build version</h3>
              </div>
            </div>
            <div class="build-versions">
              {#if frontendBuild}
                <div class="build-row">
                  <span class="build-label">Frontend</span>
                  <code class="build-value">{frontendBuild}</code>
                </div>
              {/if}
              {#if backendBuild}
                <div class="build-row">
                  <span class="build-label">Backend</span>
                  <code class="build-value">{backendBuild}</code>
                </div>
              {/if}
            </div>
          </section>
        {/if}

        <section class="card main-card">
          <div class="card-header compact">
            <div>
              <h3>{hasPassword ? t('authEditConfigTitle') : t('authEnableTitle')}</h3>
              <p>{t('authConfigDescription')}</p>
            </div>
          </div>

          <form class="settings-form" onsubmit={(event) => { event.preventDefault(); saveConfig() }}>
            <div class="form-row with-actions">
              <div class="field">
                <label for="auth-password-input">{t('authAccessPassword')}</label>
                <input
                  id="auth-password-input"
                  type="password"
                  bind:value={password}
                  placeholder={hasPassword ? t('authPasswordUnchangedPlaceholder') : t('authPasswordNewPlaceholder')}
                />
                <div class="hint">{t('authPasswordHint')}</div>
              </div>

              <div class="field">
                <label for="auth-cookie-ttl-select">{t('authCookieTtl')}</label>
                <select id="auth-cookie-ttl-select" bind:value={cookieTtl}>
                  {#each ttlOptions as opt}
                    <option value={opt.value}>{t(opt.labelKey)}</option>
                  {/each}
                </select>
                <div class="hint">{t('authCookieTtlHint')}</div>
              </div>

              <div class="actions side-actions">
                <button class="btn btn-primary" type="submit" disabled={saving || (!password && !hasPassword)}>
                  {saving ? t('saving') : (hasPassword ? t('saveSettings') : t('authEnableTitle'))}
                </button>
                <button class="btn btn-secondary" type="button" onclick={loadConfig} disabled={saving}>{t('reload')}</button>
              </div>
            </div>
          </form>
        </section>

        <section class="card command-card">
          <div class="card-header compact">
            <div>
              <h3>{t('authInstallExportCommandsTitle')}</h3>
              <p>{t('authInstallExportCommandsHint')}</p>
            </div>
          </div>

          <div class="command-list">
            <div class="command-row">
              <label for="windows-setup-command">Windows PowerShell</label>
              <div class="copy-row">
                <input id="windows-setup-command" readonly value={windowsSetupCommand} aria-label={t('authWindowsCommandAria')} />
                <button class="btn btn-secondary" type="button" onclick={() => copyText(windowsSetupCommand)}>{t('copy')}</button>
              </div>
            </div>

            <div class="command-row">
              <label for="linux-setup-command">Linux/macOS shell</label>
              <div class="copy-row">
                <input id="linux-setup-command" readonly value={linuxSetupCommand} aria-label={t('authLinuxCommandAria')} />
                <button class="btn btn-secondary" type="button" onclick={() => copyText(linuxSetupCommand)}>{t('copy')}</button>
              </div>
            </div>
          </div>

          {#if setupTokenExpiresAt}
            <div class="hint warning">{t('authSetupTokenWarning', { expires: setupTokenExpiresDisplay })}</div>
          {:else}
            <div class="hint">{t('authSetupNoTokenHint')}</div>
          {/if}
        </section>

        {#if hasPassword}
          <section class="card danger-card">
            <div class="card-header with-actions-header">
              <div>
                <h3>{t('authLoginAndAuthorizationTitle')}</h3>
                <p>{t('authLoginAndAuthorizationHint')}</p>
              </div>
              <div class="danger-actions side-actions">
                <button class="btn btn-secondary" type="button" onclick={logout} disabled={saving}>{t('authLogoutCurrentBrowser')}</button>
                <button class="btn btn-danger" type="button" onclick={disableAuth} disabled={saving}>{t('authDisableAndClear')}</button>
              </div>
            </div>
          </section>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .auth-page {
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .page-content {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
  }
  .page-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 18px;
  }
  h2, h3, p { margin: 0; }
  h2 {
    font-size: 1.35rem;
    color: var(--text);
    margin-bottom: 6px;
  }
  h3 {
    font-size: 1rem;
    color: var(--text);
    margin-bottom: 6px;
  }
  p, .hint {
    color: var(--text-secondary);
    font-size: 0.88rem;
    line-height: 1.45;
  }
  .loading {
    text-align: center;
    padding: 48px 0;
    color: var(--text-secondary);
  }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-secondary);
    color: var(--text-secondary);
    font-size: 0.88rem;
    font-weight: 600;
  }
  .status-pill .dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--text-secondary);
  }
  .status-pill.enabled {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 45%, var(--border));
  }
  .status-pill.enabled .dot {
    background: var(--success);
  }
  .success-msg, .error-msg {
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 0.9rem;
    margin-bottom: 14px;
  }
  .success-msg {
    background: rgba(34, 197, 94, 0.12);
    color: var(--success);
    border: 1px solid rgba(34, 197, 94, 0.28);
  }
  .error-msg {
    background: var(--danger);
    color: #fff;
  }
  .settings-stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .card, .api-key-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg);
    padding: 16px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.02);
  }
  .api-key-card {
    margin-bottom: 16px;
    border-color: color-mix(in srgb, var(--primary) 45%, var(--border));
    background: color-mix(in srgb, var(--primary) 7%, var(--bg));
  }
  .card-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 14px;
  }
  .card-header.compact {
    margin-bottom: 12px;
  }
  .card-header.with-actions-header {
    align-items: center;
    margin-bottom: 0;
  }
  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .form-row.with-actions {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) auto;
    gap: 14px;
    align-items: start;
  }
  .field, .command-row {
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  label {
    color: var(--text);
    font-size: 0.9rem;
    font-weight: 600;
  }
  input, select {
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    padding: 9px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 0.9rem;
    outline: none;
  }
  input:focus, select:focus {
    border-color: var(--primary);
  }
  input[readonly] {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.84rem;
  }
  .code-input {
    margin-top: 4px;
  }
  .command-list {
    display: flex;
    flex-direction: column;
    gap: 14px;
    margin-bottom: 12px;
  }
  .copy-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
  }
  .actions, .danger-actions {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .side-actions {
    justify-content: flex-end;
    align-items: flex-start;
    min-width: max-content;
  }
  .form-row .side-actions {
    padding-top: 25px;
  }
  .btn {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 14px;
    background: var(--bg-secondary);
    color: var(--text);
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }
  .btn:hover:not(:disabled) { opacity: 0.86; }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--primary);
    border-color: var(--primary);
    color: #fff;
  }
  .btn-secondary {
    background: var(--bg-secondary);
  }
  .btn-danger {
    background: var(--danger);
    border-color: var(--danger);
    color: #fff;
  }
  .warning {
    color: var(--warning);
  }

  .page-content::-webkit-scrollbar {
    width: 8px;
  }
  .page-content::-webkit-scrollbar-track {
    background: transparent;
  }
  .page-content::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: 4px;
    transition: background 0.2s;
  }
  .page-content:hover::-webkit-scrollbar-thumb {
    background: var(--border);
  }
  .page-content:hover::-webkit-scrollbar-thumb:hover {
    background: var(--text-secondary);
  }

  @media (max-width: 980px) {
    .form-row.with-actions {
      grid-template-columns: 1fr 1fr;
    }
    .form-row .side-actions {
      grid-column: 1 / -1;
      padding-top: 0;
      justify-content: flex-end;
    }
  }
  @media (max-width: 720px) {
    .page-content {
      padding: 16px;
    }
    .page-heading, .card-header, .card-header.with-actions-header {
      flex-direction: column;
      align-items: stretch;
    }
    .form-row.with-actions, .copy-row {
      grid-template-columns: 1fr;
    }
    .side-actions {
      justify-content: flex-start;
      min-width: 0;
    }
  }

  .build-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg);
    padding: 16px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.02);
  }
  .build-versions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .build-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .build-label {
    color: var(--text-secondary);
    font-size: 0.88rem;
    min-width: 72px;
    font-weight: 600;
  }
  .build-value {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.88rem;
    color: var(--text);
    background: var(--bg-secondary);
    padding: 3px 8px;
    border-radius: 4px;
  }
</style>