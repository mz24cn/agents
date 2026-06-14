<script>
  import { router, parseRoute } from './lib/router.svelte.js'
  import Layout from './components/Layout.svelte'
  import ChatPage from './components/chat/ChatPage.svelte'
  import ModelsPage from './components/models/ModelsPage.svelte'
  import ToolsPage from './components/tools/ToolsPage.svelte'
  import PromptsPage from './components/prompts/PromptsPage.svelte'
  import AgentsPage from './components/agents/AgentsPage.svelte'
  import EnvPage from './components/env/EnvPage.svelte'
  import SetupPage from './components/setup/SetupPage.svelte'
  import AuthDialog from './components/AuthDialog.svelte'

  let currentPath = $derived(parseRoute().path)
  let isChatActive = $derived(currentPath === '#/chat' || !['#/models', '#/tools', '#/prompts', '#/agents', '#/env', '#/setup'].includes(currentPath))
</script>

<Layout>
  <!-- ChatPage 始终挂载，通过 CSS 显隐，确保页面切换不影响流式推理 -->
  <div style="display: {isChatActive ? 'contents' : 'none'}">
    <ChatPage />
  </div>
  {#if !isChatActive}
    {#if currentPath === '#/models'}
      <ModelsPage />
    {:else if currentPath === '#/tools'}
      <ToolsPage />
    {:else if currentPath === '#/prompts'}
      <PromptsPage />
    {:else if currentPath === '#/agents'}
      <AgentsPage />
    {:else if currentPath === '#/env'}
      <EnvPage />
    {:else if currentPath === '#/setup'}
      <SetupPage />
    {/if}
  {/if}
</Layout>
<AuthDialog />
