<script>
  import { t } from '../../lib/i18n.svelte.js'
  import { env } from '../../lib/api.js'

  let vars = $state([])
  let loading = $state(false)
  let error = $state('')

  async function loadEnvVars() {
    loading = true
    error = ''
    try {
      vars = await env.list()
    } catch (err) {
      error = err.message || 'Failed to load environment variables'
    } finally {
      loading = false
    }
  }

  $effect(() => loadEnvVars())
</script>

<div class="env-page">
  <h2>{t('nav_env')}</h2>
  {#if loading}
    <p>Loading...</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if vars.length === 0}
    <p>No environment variables configured.</p>
  {:else}
    <ul class="env-list">
      {#each vars as env}
        <li><strong>{env.name}:</strong> {env.value}</li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .env-page {
    padding: 20px;
  }
  .env-list {
    list-style: none;
    padding: 0;
  }
  .env-list li {
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
  .error {
    color: var(--danger);
  }
</style>
