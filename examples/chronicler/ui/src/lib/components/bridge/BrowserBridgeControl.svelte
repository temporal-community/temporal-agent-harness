<script lang="ts">
  import { FolderCog, FolderOpen, RefreshCw, ShieldAlert, X } from "@lucide/svelte";
  import { onMount } from "svelte";
  import { browserBridgeRuntime } from "$lib/bridge/runtime";
  import type { BridgeStatus } from "$lib/bridge/types";

  let status = $state<BridgeStatus>({ ...browserBridgeRuntime.status });
  let open = $state(false);
  let busy = $state(false);
  let actionError = $state<string | null>(null);
  const controller = browserBridgeRuntime.controller;

  onMount(() => {
    const unsubscribe = browserBridgeRuntime.subscribe((next) => (status = { ...next }));
    const release = browserBridgeRuntime.mount();
    return () => {
      unsubscribe();
      release();
    };
  });

  const indicatorClass = $derived(
    status.phase === "connected"
      ? "connected"
      : status.phase === "standby"
        ? "standby"
        : status.phase === "error" || status.phase === "permission-needed"
          ? "attention"
          : "idle"
  );

  const label = $derived(
    status.phase === "connected"
      ? "Local bridge"
      : status.phase === "standby"
        ? "Bridge standby"
        : status.phase === "permission-needed"
          ? "Reconnect bridge"
          : "Connect folder"
  );

  async function runAction(action: () => Promise<void>): Promise<void> {
    busy = true;
    actionError = null;
    try {
      await action();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      actionError = error instanceof Error ? error.message : String(error);
    } finally {
      busy = false;
    }
  }
</script>

<div class="bridge-control">
  <button
    type="button"
    class="bridge-trigger"
    aria-haspopup="dialog"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    <span class={`indicator ${indicatorClass}`} aria-hidden="true"></span>
    <FolderCog size={15} />
    <span>{label}</span>
    {#if status.pendingCount > 0}<strong>{status.pendingCount}</strong>{/if}
  </button>

  {#if open}
    <div class="bridge-popover" role="dialog" aria-label="Browser bridge">
      <header>
        <div>
          <h2>Browser bridge</h2>
          <p>Local operations run only in the selected campaign directory.</p>
        </div>
        <button class="icon" type="button" aria-label="Close browser bridge" onclick={() => (open = false)}>
          <X size={16} />
        </button>
      </header>

      <div class="folder-row">
        <FolderOpen size={18} />
        <div>
          <span>Campaign directory</span>
          <strong>{status.directoryName ?? "Not selected"}</strong>
        </div>
      </div>

      <dl class="binding" aria-label="Bridge routing">
        <div><dt>Bridge</dt><dd>{status.bridgeId}</dd></div>
        <div><dt>Root</dt><dd>{status.rootId}</dd></div>
        {#if status.handleBindingId}
          <div class="handle-binding"><dt>Folder binding</dt><dd>{status.handleBindingId}</dd></div>
        {/if}
      </dl>

      <div class={`state-card ${indicatorClass}`}>
        {#if status.phase === "permission-needed"}<ShieldAlert size={17} />{:else}<RefreshCw size={17} />{/if}
        <div>
          <strong>{status.phase.replaceAll("-", " ")}</strong>
          <span>{status.detail}</span>
        </div>
      </div>

      {#if status.unsupportedOperations.length}
        <p class="warning">
          Waiting for another executor: {status.unsupportedOperations.join(", ")}
        </p>
      {/if}
      {#if actionError}<p class="error">{actionError}</p>{/if}

      <footer>
        <span>{status.completedCount} completed this visit</span>
        {#if status.phase !== "unsupported"}
          <button
            class="action"
            type="button"
            disabled={busy || (
              status.phase !== "permission-needed" &&
              status.directoryName !== null &&
              !status.canRebind
            )}
            onclick={() => runAction(
              status.phase === "permission-needed"
                ? () => controller.reconnect()
                : () => controller.chooseDirectory()
            )}
          >
            {busy
              ? "Connecting…"
              : status.phase === "permission-needed"
                ? "Reconnect"
                : status.directoryName
                  ? "Change folder"
                  : "Choose folder"}
          </button>
        {/if}
      </footer>
    </div>
  {/if}
</div>

<style>
  .bridge-control { position: relative; }
  .bridge-trigger {
    height: 30px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 0 10px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    color: var(--text-2);
    background: var(--control-bg);
    cursor: pointer;
    font-size: 11px;
    font-weight: 650;
  }
  .bridge-trigger:hover, .bridge-trigger:focus-visible { color: var(--text-1); background: var(--control-hover); outline: none; }
  .bridge-trigger strong { min-width: 17px; padding: 1px 5px; border-radius: 999px; color: var(--surface-0); background: var(--warning); text-align: center; }
  .indicator { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); }
  .indicator.connected { background: var(--success); box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 14%, transparent); }
  .indicator.standby { background: var(--warning); }
  .indicator.attention { background: var(--error); }
  .bridge-popover {
    position: absolute;
    top: calc(100% + 10px);
    right: 0;
    z-index: 30;
    width: min(390px, calc(100vw - 24px));
    padding: 14px;
    border: 1px solid var(--border-strong);
    border-radius: 12px;
    background: var(--surface-2);
    box-shadow: var(--shadow-popover);
  }
  header { display: flex; justify-content: space-between; gap: 14px; }
  h2 { margin: 0; color: var(--text-1); font-size: 14px; }
  p { margin: 3px 0 0; color: var(--text-3); font-size: 11px; line-height: 1.45; }
  .icon { width: 28px; height: 28px; display: grid; place-items: center; border: 0; border-radius: 6px; color: var(--text-2); background: transparent; cursor: pointer; }
  .icon:hover { color: var(--text-1); background: var(--control-hover); }
  .folder-row { margin-top: 14px; display: flex; align-items: center; gap: 10px; padding: 11px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-1); color: var(--accent); }
  .folder-row div, .state-card div { min-width: 0; display: grid; gap: 2px; }
  .folder-row span, .state-card span { color: var(--text-3); font-size: 10px; }
  .folder-row strong { overflow: hidden; color: var(--text-1); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
  .binding { margin: 8px 0 0; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .binding div { min-width: 0; display: grid; gap: 2px; padding: 8px 10px; border-radius: 7px; background: var(--surface-1); }
  .binding dt { color: var(--text-3); font-size: 9px; text-transform: uppercase; letter-spacing: .05em; }
  .binding dd { margin: 0; overflow: hidden; color: var(--text-2); font-family: ui-monospace, monospace; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
  .binding .handle-binding { grid-column: 1 / -1; }
  .state-card { margin-top: 8px; display: flex; gap: 10px; align-items: center; padding: 10px 11px; border-radius: 8px; color: var(--text-2); background: var(--surface-1); }
  .state-card strong { color: var(--text-1); font-size: 11px; text-transform: capitalize; }
  .state-card.connected { color: var(--success); }
  .state-card.standby { color: var(--warning); }
  .state-card.attention { color: var(--error); }
  .warning, .error { padding: 8px 10px; border-radius: 6px; }
  .warning { color: var(--warning); background: color-mix(in srgb, var(--warning) 9%, transparent); }
  .error { color: var(--error); background: color-mix(in srgb, var(--error) 9%, transparent); }
  footer { margin-top: 12px; display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text-3); font-size: 10px; }
  .action { height: 30px; padding: 0 11px; border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border)); border-radius: 6px; color: var(--text-1); background: color-mix(in srgb, var(--accent) 14%, var(--surface-1)); cursor: pointer; font-size: 11px; font-weight: 650; }
  .action:disabled { cursor: wait; opacity: .6; }
</style>
