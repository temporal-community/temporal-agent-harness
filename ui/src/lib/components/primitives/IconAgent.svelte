<script lang="ts">
  /**
   * The IO design system's agent mark, from temporalio/ui
   * src/lib/io/icon/icons/agent.svelte @ 93d0fb2c.
   *
   * The paths are upstream's, verbatim. What is not upstream's is the svg
   * around them: every IO icon renders through icon-svg-wrapper.svelte, which
   * imports tailwind-merge and fixes its own size at 1.143em. This app has no
   * Tailwind, and the callers here size their icons in pixels the way the
   * Lucide set does, so the wrapper is replaced by a plain svg with a `size`
   * prop. Re-copy the paths, not the wrapper, if the icon is updated upstream.
   */
  interface Props {
    size?: number;
  }

  let { size = 15 }: Props = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 18 18"
  fill="none"
  xmlns="http://www.w3.org/2000/svg"
  aria-hidden="true"
>
  <path
    d="M4.75 10C5.57843 10 6.25 10.6716 6.25 11.5C6.25 12.3284 5.57843 13 4.75 13C3.92157 13 3.25 12.3284 3.25 11.5C3.25 10.6716 3.92157 10 4.75 10Z"
    fill="currentColor"
  />
  <path
    d="M9.75 10C10.5784 10 11.25 10.6716 11.25 11.5C11.25 12.3284 10.5784 13 9.75 13C8.92157 13 8.25 12.3284 8.25 11.5C8.25 10.6716 8.92157 10 9.75 10Z"
    fill="currentColor"
  />
  <path
    fill-rule="evenodd"
    clip-rule="evenodd"
    d="M7.25 4C7.94036 4 8.5 4.52274 8.5 5.16699V6.2959C9.28127 6.34775 10.099 6.43768 10.8477 6.53516C12.9925 6.81444 14.5 8.67382 14.5 10.7871V13.5176C14.5 15.7358 12.8461 17.6414 10.5986 17.8301C9.49319 17.9228 8.2687 18 7.25 18C6.2313 18 5.00681 17.9228 3.90137 17.8301C1.65386 17.6414 0 15.7358 0 13.5176V10.7871C0 8.67382 1.50748 6.81444 3.65234 6.53516C4.40098 6.43768 5.21873 6.34775 6 6.2959V5.16699C6 4.52274 6.55964 4 7.25 4ZM7.25 7.75C6.247 7.75 4.99163 7.87325 3.8457 8.02246C2.49696 8.19822 1.5 9.37726 1.5 10.7871V13.5176C1.5 14.9936 2.59352 16.2147 4.02734 16.335C5.11897 16.4265 6.29407 16.5 7.25 16.5C8.20593 16.5 9.38103 16.4265 10.4727 16.335C11.9065 16.2147 13 14.9936 13 13.5176V10.7871C13 9.37726 12.003 8.19822 10.6543 8.02246C9.50837 7.87325 8.253 7.75 7.25 7.75Z"
    fill="currentColor"
  />
  <path d="M16 4L18 5L16 6L15 8L14 6L12 5L14 4L15 2L16 4Z" fill="currentColor" />
  <path
    d="M10.667 1.33301L12 2L10.667 2.66699L10 4L9.33301 2.66699L8 2L9.33301 1.33301L10 0L10.667 1.33301Z"
    fill="currentColor"
  />
</svg>
