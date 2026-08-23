<script>
  let {
    visible = false,
    loading = false,
    received = 0,
    total = 0,
    ariaLabel = 'Loading',
  } = $props()

  let determinate = $derived(total > 0)
  let percent = $derived(determinate ? Math.min(100, Math.max(0, received / total * 100)) : 0)
</script>

{#if visible && loading}
  <div
    class="download-progress"
    class:indeterminate={!determinate}
    role="progressbar"
    aria-label={ariaLabel}
    aria-valuemin={determinate ? 0 : undefined}
    aria-valuemax={determinate ? 100 : undefined}
    aria-valuenow={determinate ? Math.round(percent) : undefined}
  >
    <div class="download-progress-fill" style:width={determinate ? `${percent}%` : '35%'}></div>
  </div>
{/if}

<style>
  .download-progress {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    z-index: 30;
    width: 100%;
    height: 4px;
    overflow: hidden;
    background: color-mix(in srgb, var(--primary) 15%, transparent);
    pointer-events: none;
  }

  .download-progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #22c55e 0%, #06b6d4 45%, #6366f1 100%);
    box-shadow: 0 0 8px color-mix(in srgb, var(--primary) 65%, transparent);
    transition: width 80ms linear;
  }

  .download-progress.indeterminate .download-progress-fill {
    position: absolute;
    animation: download-progress-indeterminate 1.15s ease-in-out infinite;
  }

  @keyframes download-progress-indeterminate {
    from { transform: translateX(-110%); }
    to { transform: translateX(310%); }
  }
</style>
