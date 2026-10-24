<script lang="ts">
  import { onDestroy } from "svelte";
  import { prefersReducedMotion } from "svelte/motion";
  import * as THREE from "three";
  import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
  import {
    PlanarLayout,
    ease,
    peerParticles,
    segments,
    targets,
    type SceneMode,
    type Vec3
  } from "$lib/state/swarmLayout";
  import type { SwarmGraph } from "$lib/state/swarmView";

  /**
   * A swarm in 3D: members as instanced spheres (board lanes as cubes) colored by phase and sized
   * by role, parent links as lines, claims to the board as faint threads, and a peer turn in
   * flight as particles running from the asker to the peer.
   *
   * Hundreds of members are two draw calls, not hundreds: each kind of shape is one
   * InstancedMesh whose per-member matrix and color are rewritten each frame, and each kind of
   * edge is one LineSegments buffer. Where things go is decided by `swarmLayout` (the plane from
   * d3-force, height from the mode); this component only eases members toward those targets
   * and draws them. With reduced motion, members jump to their places and particles hold still.
   *
   * Colors come from the console's own CSS tokens, read off the canvas, so the scene follows the
   * theme. A browser with no WebGL calls `onUnsupported` and draws nothing.
   */
  interface Props {
    graph: SwarmGraph;
    mode: SceneMode;
    selectedId?: string | null;
    onUnsupported?: () => void;
  }

  let { graph, mode, selectedId = $bindable(null), onUnsupported }: Props = $props();

  let host = $state<HTMLDivElement | null>(null);

  const layout = new PlanarLayout();
  const current = new Map<string, Vec3>();
  let goal = new Map<string, Vec3>();

  // Everything three.js, created once the host is mounted (never in a server render).
  let renderer: THREE.WebGLRenderer | null = null;
  let scene: THREE.Scene | null = null;
  let camera: THREE.PerspectiveCamera | null = null;
  let controls: OrbitControls | null = null;
  let spheres: THREE.InstancedMesh | null = null;
  let cubes: THREE.InstancedMesh | null = null;
  let parentLines: THREE.LineSegments | null = null;
  let boardLines: THREE.LineSegments | null = null;
  let particles: THREE.Points | null = null;
  let observer: ResizeObserver | null = null;
  let frame = 0;
  const clock = new THREE.Clock();
  /** Instance index → workflow id, per mesh, for picking. */
  let sphereIds: string[] = [];
  let cubeIds: string[] = [];

  const sphereGeometry = new THREE.SphereGeometry(1, 16, 12);
  const cubeGeometry = new THREE.BoxGeometry(1.6, 1.6, 1.6);

  // ---------------------------------------------------------------- colors

  const toneCache = new Map<string, THREE.Color>();

  /**
   * A CSS custom property's color, normalized through a 2D canvas so any CSS syntax works, and
   * read as sRGB (three converts it to its linear working space).
   */
  function tone(name: string): THREE.Color {
    const cached = toneCache.get(name);
    if (cached) return cached;
    const raw = host ? getComputedStyle(host).getPropertyValue(name).trim() : "";
    const probe = document.createElement("canvas").getContext("2d");
    let color = new THREE.Color(0x888888);
    if (probe && raw) {
      probe.fillStyle = raw;
      const normalized = probe.fillStyle; // "#rrggbb" or "rgba(r, g, b, a)"
      const rgba = /rgba?\(([^)]+)\)/.exec(normalized);
      if (rgba) {
        const [r, g, b] = rgba[1].split(",").map((v) => Number.parseFloat(v) / 255);
        color = new THREE.Color().setRGB(r, g, b, THREE.SRGBColorSpace);
      } else color = new THREE.Color(normalized);
    }
    toneCache.set(name, color);
    return color;
  }

  /** Re-read the tokens when the theme flips. */
  function watchTheme(): () => void {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const clear = () => {
      toneCache.clear();
      applyTheme();
    };
    media.addEventListener("change", clear);
    const attributes = new MutationObserver(clear);
    attributes.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
    return () => {
      media.removeEventListener("change", clear);
      attributes.disconnect();
    };
  }

  function applyTheme(): void {
    if (!scene) return;
    scene.background = tone("--surface-0").clone();
    (parentLines?.material as THREE.LineBasicMaterial | undefined)?.color.copy(tone("--text-3"));
    (boardLines?.material as THREE.LineBasicMaterial | undefined)?.color.copy(tone("--accent"));
    (particles?.material as THREE.PointsMaterial | undefined)?.color.copy(tone("--reasoning"));
  }

  // ---------------------------------------------------------------- setup

  let stopTheme: (() => void) | null = null;

  $effect(() => {
    if (!host || renderer) return;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true });
    } catch {
      onUnsupported?.();
      return;
    }
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    host.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(50, 1, 1, 6000);
    camera.position.set(0, 260, 620);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = !prefersReducedMotion.current;
    controls.autoRotateSpeed = 0.35;
    // A reader who grabs the view has taken over; stop turning it for them.
    controls.addEventListener("start", () => {
      if (controls) controls.autoRotate = false;
    });

    scene.add(new THREE.AmbientLight(0xffffff, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(300, 600, 400);
    scene.add(sun);

    parentLines = new THREE.LineSegments(
      new THREE.BufferGeometry(),
      new THREE.LineBasicMaterial({ transparent: true, opacity: 0.45 })
    );
    boardLines = new THREE.LineSegments(
      new THREE.BufferGeometry(),
      new THREE.LineBasicMaterial({ transparent: true, opacity: 0.14 })
    );
    particles = new THREE.Points(
      new THREE.BufferGeometry(),
      new THREE.PointsMaterial({ size: 3.2, sizeAttenuation: true, transparent: true, opacity: 0.95 })
    );
    scene.add(parentLines, boardLines, particles);
    applyTheme();
    stopTheme = watchTheme();

    observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    clock.start();
    frame = requestAnimationFrame(loop);
  });

  function resize(): void {
    if (!host || !renderer || !camera) return;
    const { width, height } = host.getBoundingClientRect();
    if (width === 0 || height === 0) return;
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  /** An InstancedMesh with room for `count`, replacing `mesh` when it is too small. */
  function capacity(mesh: THREE.InstancedMesh | null, geometry: THREE.BufferGeometry, count: number) {
    if (mesh && mesh.instanceMatrix.count >= count) {
      mesh.count = count;
      return mesh;
    }
    if (mesh) {
      scene?.remove(mesh);
      mesh.dispose();
    }
    const room = Math.max(64, 2 ** Math.ceil(Math.log2(Math.max(1, count))));
    const created = new THREE.InstancedMesh(
      geometry,
      new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.05 }),
      room
    );
    created.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    created.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(room * 3), 3);
    created.count = count;
    scene?.add(created);
    return created;
  }

  // ---------------------------------------------------------------- per poll and per frame

  $effect(() => {
    // A new poll or a new mode: reheat the plane if the shape changed, and re-aim every member.
    layout.update(graph);
    goal = targets(graph, layout, mode);
  });

  const matrix = new THREE.Matrix4();
  const scale = new THREE.Vector3();
  const position = new THREE.Vector3();
  const rotation = new THREE.Quaternion();
  const white = new THREE.Color(1, 1, 1);
  const highlight = new THREE.Color();

  function loop(): void {
    frame = requestAnimationFrame(loop);
    if (!renderer || !scene || !camera) return;
    const still = prefersReducedMotion.current;
    if (layout.tick(still ? 300 : 1)) goal = targets(graph, layout, mode);
    ease(current, goal, still ? 1 : 0.12);
    draw(clock.getElapsedTime(), still);
    controls?.update();
    renderer.render(scene, camera);
  }

  function draw(t: number, still: boolean): void {
    const members = graph.nodes.filter((n) => current.has(n.id));
    const boards = members.filter((n) => n.role === "board");
    const others = members.filter((n) => n.role !== "board");
    spheres = capacity(spheres, sphereGeometry, others.length);
    cubes = capacity(cubes, cubeGeometry, boards.length);
    sphereIds = others.map((n) => n.id);
    cubeIds = boards.map((n) => n.id);
    // Members waiting on a person pulse, unless motion is reduced.
    const pulse = still ? 1 : 1 + 0.25 * Math.sin(t * 5);
    for (const [mesh, list] of [
      [spheres, others],
      [cubes, boards]
    ] as const) {
      list.forEach((node, i) => {
        const [x, y, z] = current.get(node.id)!;
        const urgent = node.phase === "awaiting_approval" || node.phase === "awaiting_callback";
        const size = node.radius * (urgent && !node.closed ? pulse : 1) * (node.closed ? 0.6 : 1);
        matrix.compose(position.set(x, y, z), rotation, scale.set(size, size, size));
        mesh.setMatrixAt(i, matrix);
        const color = tone(node.tone);
        mesh.setColorAt(
          i,
          node.id === selectedId ? highlight.copy(color).lerp(white, 0.55) : color
        );
      });
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
    setPositions(parentLines, segments(graph.edges, current, new Set(["parent"])));
    setPositions(boardLines, segments(graph.edges, current, new Set(["board"])));
    setPositions(particles, peerParticles(graph.edges, current, still ? 0 : t));
  }

  function setPositions(object: THREE.LineSegments | THREE.Points | null, data: Float32Array): void {
    if (!object) return;
    const geometry = object.geometry;
    const existing = geometry.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (existing && existing.array.length === data.length) {
      (existing.array as Float32Array).set(data);
      existing.needsUpdate = true;
    } else {
      geometry.setAttribute("position", new THREE.BufferAttribute(data, 3));
    }
    geometry.computeBoundingSphere();
  }

  // ---------------------------------------------------------------- picking

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pressed: { x: number; y: number } | null = null;

  function onDown(event: PointerEvent) {
    pressed = { x: event.clientX, y: event.clientY };
  }

  /** A press that did not orbit is a click: on a member it selects it, elsewhere it clears. */
  function onUp(event: PointerEvent) {
    const start = pressed;
    pressed = null;
    if (!start || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4) return;
    if (!renderer || !camera) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(pointer, camera);
    const meshes = [spheres, cubes].filter((m): m is THREE.InstancedMesh => m !== null);
    const hit = raycaster.intersectObjects(meshes, false)[0];
    if (!hit || hit.instanceId === undefined) {
      selectedId = null;
      return;
    }
    selectedId = (hit.object === cubes ? cubeIds : sphereIds)[hit.instanceId] ?? null;
  }

  onDestroy(() => {
    if (frame) cancelAnimationFrame(frame);
    observer?.disconnect();
    stopTheme?.();
    controls?.dispose();
    for (const mesh of [spheres, cubes]) mesh?.dispose();
    for (const object of [parentLines, boardLines, particles]) {
      object?.geometry.dispose();
      (object?.material as THREE.Material | undefined)?.dispose();
    }
    sphereGeometry.dispose();
    cubeGeometry.dispose();
    renderer?.dispose();
    renderer?.domElement.remove();
  });
</script>

<div
  bind:this={host}
  class="scene"
  role="application"
  aria-label="Swarm in 3D: drag to orbit, scroll to zoom, click a member to select it"
  onpointerdown={onDown}
  onpointerup={onUp}
></div>

<style>
  .scene {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    background: var(--surface-0);
    touch-action: none;
    cursor: grab;
  }

  .scene:active {
    cursor: grabbing;
  }
</style>
