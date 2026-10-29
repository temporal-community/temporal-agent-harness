<script lang="ts">
  import { ChevronDown } from "@lucide/svelte";
  import type { Snippet } from "svelte";
  import type { HTMLSelectAttributes } from "svelte/elements";

  /**
   * The one dropdown in the app. A native select, so the list, the keyboard, screen readers
   * and touch stay the platform's; the box is a `.field` drawn the way the composer's handler
   * picker is (`.field.select` in app.css): the chip's mono label register and its 12px chevron.
   */
  export type SelectOption = string | { value: string; label: string };

  interface Props extends Omit<HTMLSelectAttributes, "value" | "class" | "children"> {
    value?: string;
    options: readonly SelectOption[];
    /** A disabled first option shown while the value is empty. Without it an unanswered
        select draws its first option, a choice the value does not hold. */
    placeholder?: string;
    /** A word or icon ahead of the value, inside the box; the box becomes the select's label. */
    lead?: Snippet;
    class?: string;
  }

  let { value = $bindable(""), options, placeholder, lead, class: extraClass, ...rest }: Props = $props();

  const items = $derived(options.map((option) => (typeof option === "string" ? { value: option, label: option } : option)));
</script>

<svelte:element this={lead ? "label" : "span"} class={["field", "select", extraClass]}>
  {#if lead}<span class="select-lead">{@render lead()}</span>{/if}
  <select {...rest} bind:value>
    {#if placeholder != null}<option value="" disabled>{placeholder}</option>{/if}
    <!-- By position: a schema enum may repeat a value, and a keyed each throws on duplicates. -->
    {#each items as item, index (index)}
      <option value={item.value}>{item.label}</option>
    {/each}
  </select>
  <ChevronDown class="select-chevron" size={12} aria-hidden="true" />
</svelte:element>
