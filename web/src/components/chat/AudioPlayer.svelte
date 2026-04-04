<script>
  import { t } from '../../lib/i18n.svelte.js'
  let { audio = '' } = $props()
</script>

{#if audio}
  <div class="audio-player">
    <audio controls src="data:audio/wav;base64,{audio}">
      {t('audioNotSupported')}
    </audio>
  </div>
{/if}

<style>
  .audio-player {
    margin-top: 6px;
  }
  .audio-player audio {
    max-width: 100%;
  }
</style>
