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

  let currentPath = $derived(parseRoute().path)
</script>

<Layout>
  {#if currentPath === '#/chat'}
    <ChatPage />
  {:else if currentPath === '#/models'}
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
  {:else}
    <ChatPage />
  {/if}
</Layout>
