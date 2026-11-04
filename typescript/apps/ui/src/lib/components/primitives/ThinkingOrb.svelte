<script lang="ts">
  /**
   * The busy affordance for a turn in flight: the Temporal mark turning, with a
   * light running its circuit, breathing underneath.
   *
   * Three motions on three separate properties, deliberately. The whole glyph
   * rotates — a `transform` on the <svg>, four-fold symmetric so a quarter turn
   * lands on itself and the eye reads a steady spin. Riding on top, a short
   * bright segment traces each of the mark's two ellipses via `stroke-dash`, a
   * paint operation inside the same element. Under both, the element's
   * `opacity` breathes.
   *
   * They compose rather than fight because no two touch the same property, and
   * their periods (3429ms, 2810ms, 1207ms) are pairwise coprime, so the three
   * never resynchronise into a pattern the eye can catch.
   *
   * The two `.trace` paths are the reason this works at all. The mark is a
   * single FILLED compound path — outer silhouette plus three counters — so
   * stroke-dash on it traces all four contours at once and reads as crawling,
   * not tracing. Instead the trace rides two invisible ellipses laid on the
   * ring's midline, derived from the mark's own geometry: outer semi-axes
   * (7.9793, 2.9864) from the x-extents and the contour crossing at
   * (10.7958, 5.18243), inner (7.1721, 2.1569) from the counters, midline the
   * mean of the two — rx 7.5757, ry 2.5716 about the glyph's centre
   * (7.99886, 7.97933). The ring measures 0.807 thick across and 0.829 down,
   * near enough uniform that one midline ellipse tracks it the whole way round.
   *
   * `pathLength="100"` normalises each ellipse so the dash array is a percentage
   * of the circuit and needs no perimeter arithmetic (it is ~33.85 user units).
   * Written as arc paths rather than <ellipse> because pathLength is only
   * dependably honoured on <path>.
   *
   * CSS keyframes rather than svelte/motion: Tween and Spring move a value
   * toward a target and have no notion of a loop, so a perpetual turn would
   * cost a reactive write and a re-render every frame, forever, for chrome.
   *
   * The source file's clipPath is dropped rather than inlined. It clipped to a
   * rect the glyph already fits inside, so it bought nothing, and it carried a
   * fixed id that would collide the moment two of these render on one page.
   *
   * The track is stroked on top of its own fill, and that is not decoration. As
   * a pure fill at 16px its ring lands at roughly 0.8px, which reads as a
   * hairline smudge. The stroke puts the ink back.
   *
   * Which is why the viewBox is offset rather than `0 0 16 16`: the strokes add
   * half their width beyond the path on every side, and the glyph's top tip sits
   * exactly on y=0 while its left tip sits at x=0.02, so the bare box would clip
   * the top and not the left. The box is grown by 0.5 and re-centred on the
   * glyph's true centre.
   */
  interface Props {
    /** Edge length in px. */
    size?: number;
    /** Merged with the orb's own classes rather than replacing them. */
    class?: string;
    /** Everything else (aria-*, data-*) lands on the svg. */
    [key: string]: unknown;
  }

  let { size = 18, class: extraClass = "", ...rest }: Props = $props();

  const classes = $derived(["thinking-orb", extraClass].filter(Boolean).join(" "));
</script>

<!-- Spread first, so the shape and the sizing the component is answerable for
     are written after it and win — same order as Chip and IconButton. -->
<svg
  {...rest}
  class={classes}
  style="--thinking-orb-size: {size}px"
  viewBox="-0.50114 -0.52067 17 17"
  fill="none"
  xmlns="http://www.w3.org/2000/svg"
  aria-hidden="true"
>
  <!-- The breathe lives on this group, not the <svg>: the trace's glyph cell fades the svg
       itself to swap in the disclosure chevron on hover, and an opacity animation on the
       same element would beat that declaration. -->
  <g class="body">
    <path
      class="track"
      d="M10.7958 5.18243C10.4187 2.35783 9.46449 0 7.99886 0C6.53323 0 5.57902 2.35783 5.20196 5.18243C2.37736 5.55948 0.0195312 6.5137 0.0195312 7.97933C0.0195312 9.44496 2.37778 10.3992 5.20196 10.7762C5.57902 13.6004 6.53323 15.9587 7.99886 15.9587C9.46449 15.9587 10.4187 13.6004 10.7958 10.7762C13.6204 10.3992 15.9782 9.44496 15.9782 7.97933C15.9782 6.5137 13.6199 5.55948 10.7958 5.18243ZM5.10976 9.95349C2.40465 9.56279 0.826973 8.66853 0.826973 7.97933C0.826973 7.29013 2.40465 6.39587 5.10976 6.00517C5.05023 6.65633 5.01922 7.32114 5.01922 7.97933C5.01922 8.63752 5.05023 9.30274 5.10976 9.95349ZM7.99886 0.807442C8.68806 0.807442 9.58232 2.38512 9.97302 5.09023C9.32227 5.0307 8.65705 4.99969 7.99886 4.99969C7.34067 4.99969 6.67545 5.0307 6.0247 5.09023C6.4154 2.38512 7.30966 0.807442 7.99886 0.807442ZM10.888 9.95349C10.7548 9.97292 10.2087 10.037 10.0706 10.0506C10.0569 10.1891 9.99245 10.7349 9.97343 10.868C9.58274 13.5731 8.68847 15.1508 7.99927 15.1508C7.31007 15.1508 6.41581 13.5731 6.02511 10.868C6.0061 10.7349 5.9416 10.1887 5.92796 10.0506C5.86511 9.4094 5.82708 8.72062 5.82708 7.97933C5.82708 7.23804 5.86511 6.54925 5.92796 5.9076C6.5692 5.84476 7.25798 5.80672 7.99927 5.80672C8.74057 5.80672 9.42935 5.84476 10.0706 5.9076C10.2091 5.92124 10.7548 5.98574 10.888 6.00475C13.5931 6.39545 15.1712 7.29013 15.1712 7.97891C15.1712 8.6677 13.5931 9.56279 10.888 9.95349Z"
      fill="currentColor"
      stroke="currentColor"
      stroke-width="0.7"
    />
    <path
      class="trace"
      d="M0.42316 7.97933a7.5757 2.5716 0 1 0 15.1514 0a7.5757 2.5716 0 1 0 -15.1514 0"
      pathLength="100"
    />
    <path
      class="trace lag"
      d="M5.42726 7.97933a2.5716 7.5757 0 1 0 5.1432 0a2.5716 7.5757 0 1 0 -5.1432 0"
      pathLength="100"
    />
  </g>
</svg>

<style>
  .thinking-orb {
    display: block;
    flex: 0 0 auto;
    width: var(--thinking-orb-size);
    height: var(--thinking-orb-size);
    color: var(--thinking-orb-color, var(--text-3));
    animation: orb-turn 3429ms linear infinite;
  }

  .body {
    transform-box: fill-box;
    transform-origin: center;
    animation: orb-breathe 1207ms var(--ease-in-out) infinite;
  }

  /* 1207ms. Read literally that is ~50 a minute, well above any breath, so this
     is a pulse with a breath's shape rather than a breath: eased, and swelling
     as it brightens, where a pulse would only flash. Tuned by eye,
     not derived.

     All three periods are pairwise coprime here, so nothing relocks inside an
     hour. Worth preserving if these are retuned — and note it is the SHORT
     common multiples that show, not the existence of one.

     The swell is the `scale` property, NOT `transform: scale()`. The spin above
     already owns `transform`, and two animations on one property do not
     compose — the later would simply win and the rotation would stop. `scale`
     is its own property, applied before `transform`, so the two stack. Six
     percent is about a pixel at 16px: felt rather than seen, which is the
     point. */
  @keyframes orb-breathe {
    0%,
    100% {
      opacity: 0.63;
      scale: 0.94;
    }
    50% {
      opacity: 1;
      scale: 1;
    }
  }

  /* Linear, and a full revolution. An eased turn would stall and lurch at the
     seam every cycle, which is the one thing a loop cannot afford. */
  @keyframes orb-turn {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  /* Full strength: the mark is the thing, and the light is a colour laid over
     it rather than the only lit part of it. */
  .track {
    opacity: 1;
  }

  /* Painted after the track in document order, so the light sits on top of the
     mark rather than under it. Falls back to currentColor, so a caller that
     sets only --thinking-orb-color still gets a coherent monochrome orb. */
  .trace {
    fill: none;
    stroke: var(--thinking-orb-trace-color, currentColor);
    stroke-width: 0.7;
    stroke-linecap: round;
    /* Of 100, by pathLength. 15 is a segment, not a comet and not a chase. */
    stroke-dasharray: 15 85;
    animation: orb-trace 2810ms linear infinite;
  }

  /* Half a cycle behind, so the pair reads as one light going round rather than
     two setting off side by side. Must stay exactly half of orb-trace's period
     — drift it and the two segments stop being opposite. Which is why the trace
     period is kept even: an odd one has no exact half. */
  .lag {
    animation-delay: -1405ms;
  }

  /* Linear and a whole circuit: the segment must arrive where it started at the
     same speed it left, or the loop ticks once per lap. */
  @keyframes orb-trace {
    from {
      stroke-dashoffset: 100;
    }
    to {
      stroke-dashoffset: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    /* The busy affordance stays; all three movements go — the spin on the svg, the
       breathe on its group, and the light. The light is dropped rather
       than frozen, since a stopped segment parked on one side of the mark reads
       as a rendering fault, not as a paused animation. */
    .thinking-orb {
      animation: none;
    }

    .body {
      animation: none;
    }

    .trace {
      display: none;
    }

    .track {
      opacity: 0.85;
    }
  }
</style>
