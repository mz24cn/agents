<script>
  import { themeState, toggleTheme } from '../lib/theme.svelte.js'
  import { t } from '../lib/i18n.svelte.js'
</script>

<button class="theme-toggle" onclick={toggleTheme} aria-label={t('toggleTheme')}>
  {#if themeState.current === 'light'}
    <span class="theme-emoji">☀️</span>
  {:else}
    <span class="theme-emoji">🌙</span>
  {/if}
</button>

<style>
  .theme-toggle {
    background: none;
    border: 1px solid var(--border);
    color: var(--text);
    cursor: pointer;
    padding: 6px 0;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    width: 100%;
  }
  .theme-toggle:hover {
    background-color: var(--bg-secondary);
  }
  .theme-emoji {
    font-size: 1rem;
    line-height: 1;
  }
</style>
