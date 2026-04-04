<script>
  import { router } from './lib/router.svelte.js'
  import Layout from './components/Layout.svelte'
  import ChatPage from './components/chat/ChatPage.svelte'
  import ModelsPage from './components/models/ModelsPage.svelte'
  import ToolsPage from './components/tools/ToolsPage.svelte'
  import PromptsPage from './components/prompts/PromptsPage.svelte'
</script>

<Layout>
  {#if router.current === '#/chat'}
    <ChatPage />
  {:else if router.current === '#/models'}
    <ModelsPage />
  {:else if router.current === '#/tools'}
    <ToolsPage />
  {:else if router.current === '#/prompts'}
    <PromptsPage />
  {:else}
    <ChatPage />
  {/if}
</Layout>
