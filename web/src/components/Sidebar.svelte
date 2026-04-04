<script>
  import { router } from '../lib/router.svelte.js'
  import ThemeToggle from './ThemeToggle.svelte'
  import { t, i18n, setLang, LANGS } from '../lib/i18n.svelte.js'

  const navItems = [
    { hash: '#/chat', key: 'nav_chat' },
    { hash: '#/models', key: 'nav_models' },
    { hash: '#/tools', key: 'nav_tools' },
    { hash: '#/prompts', key: 'nav_prompts' },
  ]
</script>

<aside class="sidebar">
  <nav class="nav">
    {#each navItems as item}
      <a
        href={item.hash}
        class="nav-item"
        class:active={router.current === item.hash}
      >
        {t(item.key)}
      </a>
    {/each}
  </nav>
  <div class="sidebar-footer">
    <ThemeToggle />
    <div class="lang-switcher">
      {#each LANGS as lang}
        <button
          class="lang-btn"
          class:active={i18n.lang === lang.code}
          onclick={() => setLang(lang.code)}
        >{lang.label}</button>
      {/each}
    </div>
  </div>
</aside>

<style>
  .sidebar {
    width: 220px;
    min-height: 100vh;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
  }
  .nav {
    display: flex;
    flex-direction: column;
    padding: 16px 0;
    flex: 1;
  }
  .nav-item {
    display: block;
    padding: 12px 20px;
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.95rem;
    transition: background-color 0.15s, color 0.15s;
  }
  .nav-item:hover {
    background-color: var(--border);
    color: var(--text);
  }
  .nav-item.active {
    color: var(--primary);
    background-color: var(--bg);
    font-weight: 600;
    border-right: 3px solid var(--primary);
  }
  .sidebar-footer {
    padding: 16px 20px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .lang-switcher {
    display: flex;
    gap: 6px;
  }
  .lang-btn {
    flex: 1;
    padding: 4px 0;
    border-radius: 5px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text-secondary);
    font-size: 0.8rem;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }
  .lang-btn:hover {
    background: var(--border);
    color: var(--text);
  }
  .lang-btn.active {
    background: var(--primary);
    color: #fff;
    border-color: var(--primary);
  }
</style>
