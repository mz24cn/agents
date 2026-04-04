<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { getText } = $props()

  function handleCopy() {
    navigator.clipboard.writeText(getText()).catch(() => {})
  }
</script>

<button class="copy-btn" onclick={handleCopy}>{t('copy')}</button>

<style>
  .copy-btn {
    padding: 2px 8px;
    font-size: 0.7em;
    color: var(--text-secondary, #888);
    background: var(--bg-tertiary, rgba(0,0,0,0.15));
    border: none;
    border-radius: 4px;
    cursor: pointer;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
    transition: background 0.1s;
  }
  .copy-btn:hover {
    background: var(--bg-secondary, rgba(0,0,0,0.2));
    color: var(--text, #333);
  }
  .copy-btn:active {
    background: var(--primary, #4a9eff);
    color: #fff;
  }
</style>
