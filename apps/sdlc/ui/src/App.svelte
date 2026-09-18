<script lang="ts">
  import { onMount } from "svelte";
  import {
    ArrowUpRight,
    ArrowRight,
    Plus,
    Settings2,
    GitBranch,
    Check,
    Circle,
    Pause,
    Play,
    Square,
    Files,
    Terminal,
    Code2,
    MessageSquare,
    FlaskConical,
    ChevronRight,
    X,
    Activity,
    FolderOpen,
    Command,
    RefreshCw,
    ShieldCheck,
    AlertCircle,
    Bell,
    Brain,
    GitMerge,
    Search,
    Trash2,
  } from "@lucide/svelte";
  import {
    api,
    ApiError,
    type Home,
    type Task,
    type Project,
    type ProjectMerge,
    type ProviderCheck,
    type MessageDraft,
    type MessageReceipt,
  } from "./types";
  import ExecutionWorkspace from "./ExecutionWorkspace.svelte";
  import ReviewPanel from "./ReviewPanel.svelte";

  let home = $state<Home | null>(null);
  let active = $state<Task | null>(null);
  let view = $state("workspace");
  let tab = $state("activity");
  let composer = $state(false);
  let busy = $state(false);
  let error = $state("");
  let toast = $state("");
  let deleteDialog = $state<HTMLDialogElement>();
  let deleteTarget = $state<Task | null>(null);
  let removeWorkspace = $state(false);
  let deleting = $state(false);
  let deleteError = $state("");
  let mergeDialog = $state<HTMLDialogElement>();
  let mergeTarget = $state<Task | null>(null);
  let merging = $state(false);
  let mergeError = $state("");
  let locked = $state(false);
  let unlockToken = $state("");
  let projectId = $state("");
  let profileId = $state("");
  let recoveryProfileId = $state("");
  let mode = $state("change");
  let prompt = $state("");
  let parentTaskId = $state("");
  let projectPath = $state("");
  let feedback = $state("");
  let acceptanceNote = $state("");
  let query = $state("");
  let reviewPath = $state("");
  let reviewRequest = $state(0);
  let traceWorkflow = $state("");
  let fileNames = $state<string[]>([]);
  let selectedFile = $state("");
  let fileText = $state("");
  let fileHash = $state("");
  let originalText = $state("");
  let label = $state("");
  let provider = $state("openai");
  let model = $state("");
  let baseUrl = $state("");
  let apiKey = $state("");
  let envVar = $state("");
  let maxSteps = $state(24);
  let testingProfiles = $state<Record<string, boolean>>({});
  let providerChecks = $state<Record<string, ProviderCheck | undefined>>({});
  let providerCheckErrors = $state<Record<string, string>>({});
  let promptEl = $state<HTMLTextAreaElement>();
  let messageEl = $state<HTMLTextAreaElement>();
  let messageDrafts = $state<Record<string, MessageDraft>>({});
  let draftsLoaded = $state(false);
  let messageSending = $state("");
  let messageErrors = $state<Record<string, string>>({});
  let turns = $derived(active?.snapshot?.conversation?.value.turns ?? []);
  let currentTurn = $derived(turns.at(-1));
  let messageDraft = $derived(active ? messageDrafts[active.id] : undefined);
  let taskProfiles = $derived(
    home?.profiles.filter(
      (p) =>
        p.available !== false &&
        (p.provider !== "demo" ||
          home?.projects.some(
            (project) => project.id === projectId && project.demo,
          )),
    ) ?? [],
  );
  function preferredTaskProfile() {
    return (
      taskProfiles.find((p) => p.provider !== "demo")?.id ??
      taskProfiles[0]?.id ??
      ""
    );
  }
  $effect(() => {
    // Preserve an explicit choice while polling; choose a configured profile
    // on first load or when the selection no longer fits this repository.
    if (!taskProfiles.some((p) => p.id === profileId))
      profileId = preferredTaskProfile();
  });
  let messageProfiles = $derived(
    home?.profiles.filter(
      (p) => p.available !== false && (p.provider !== "demo" || project?.demo),
    ) ?? [],
  );
  let canSendMessage = $derived(
    Boolean(
      active?.can_message &&
      messageDraft?.prompt.trim() &&
      messageProfiles.some((p) => p.id === messageDraft?.profile_id) &&
      messageDraft &&
      messageDraft.max_steps >= 3 &&
      messageDraft.max_steps <= 200 &&
      !busy &&
      !messageSending &&
      fileText === originalText,
    ),
  );
  $effect(() => {
    if (draftsLoaded) {
      try {
        localStorage.setItem(
          "sdlc.message-drafts",
          JSON.stringify(messageDrafts),
        );
      } catch {
        /* In-memory drafts still work when browser storage is full. */
      }
    }
  });
  let progress = $derived(active?.snapshot?.value);
  let taskProfile = $derived(
    currentTurn ??
      active?.input?.profile ??
      home?.profiles.find((p) => p.id === active?.profile_id),
  );
  let failure = $derived(
    progress?.status === "failed"
      ? (progress.failure ?? {
          category: "legacy",
          title: "This task stopped before detailed errors were available",
          explanation:
            "This older run did not retain the underlying error details, so its exact cause cannot be recovered. New tasks now record a specific error category and recovery steps.",
          actions: [
            "Review the provider, API key, and exact API model ID in Settings. Add a corrected profile if needed.",
            "Choose Continue. Your original request is filled in and your workspace changes carry forward. Select the profile you want before starting.",
            "If the follow-up fails, its error card will show the captured status and what to do next.",
          ],
          http_status: null,
          provider_code: "",
        })
      : null,
  );
  let project = $derived(
    home?.projects.find((p) => p.id === active?.project_id),
  );
  let mergeProject = $derived(
    home?.projects.find((p) => p.id === mergeTarget?.project_id),
  );
  let projectMergeCurrent = $derived(
    Boolean(
      progress?.project_merge &&
      progress?.revision &&
      progress.project_merge.revision === progress.revision,
    ),
  );
  let filteredTasks = $derived(
    home?.tasks.filter((t) =>
      (t.title + t.project_name).toLowerCase().includes(query.toLowerCase()),
    ) ?? [],
  );
  let editable = $derived(
    active?.live &&
      !active.pending_message &&
      (progress?.status === "paused" || active.can_message) &&
      ["paused", "review", "failed", "cancelled"].includes(
        progress?.status ?? "",
      ),
  );
  let canDelete = $derived(
    active?.dispatch === "preparing" || active?.can_message,
  );
  let lastGate = "";
  let selection = 0;
  let refreshing = false;

  const statusLabel = (status?: string) =>
    ({
      needs_you: "Needs you",
      running: "Working",
      review: "Ready for review",
      accepted: "Accepted",
      merging: "Merging",
      paused: "Paused",
      failed: "Needs attention",
      cancelled: "Stopped",
      queued: "Queued",
    })[status ?? "queued"] ?? status;
  async function attempt(fn: () => Promise<void>) {
    busy = true;
    error = "";
    try {
      await fn();
    } catch (e) {
      error = String((e as Error).message);
    } finally {
      busy = false;
    }
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const selectedIdentity = active?.id;
    try {
      home = await api<Home>("/home");
      locked = false;
      if (!projectId && home.projects.length) projectId = home.projects[0].id;
      if (active) {
        const identity = active.id;
        const next = await api<Task>("/tasks/" + identity);
        if (active?.id !== identity) return;
        active = next;
        const draft = messageDrafts[identity];
        if (
          draft?.attempt &&
          next.snapshot?.conversation?.value.turns.some(
            (turn) => turn.message_id === draft.attempt?.id,
          )
        ) {
          // A response may be lost after the agent accepted the message.
          messageDrafts[identity] = {
            ...draft,
            prompt: "",
            attempt: undefined,
          };
          messageErrors[identity] = "";
        }
        const gate = next.snapshot?.value.gate;
        if (gate && gate.id !== lastGate) {
          lastGate = gate.id;
          feedback = "";
          if (
            "Notification" in window &&
            Notification.permission === "granted" &&
            document.hidden
          )
            new Notification("boltzmann needs you", { body: gate.title });
        }
      }
    } catch (e) {
      if (!home) locked = true;
      else if (!selectedIdentity || active?.id === selectedIdentity)
        error = (e as Error).message;
    } finally {
      refreshing = false;
    }
  }
  function confirmDelete() {
    if (!active || !canDelete) return;
    deleteTarget = active;
    removeWorkspace = false;
    deleteError = "";
    deleteDialog?.showModal();
  }
  function confirmMerge() {
    if (
      !active ||
      !active.live ||
      progress?.status !== "accepted" ||
      (progress.mode ?? active.mode) !== "change"
    )
      return;
    mergeTarget = active;
    mergeError = "";
    if (!mergeDialog?.open) mergeDialog?.showModal();
  }
  function closeMergeDialog() {
    merging = false;
    if (mergeDialog?.open) mergeDialog.close();
    mergeTarget = null;
    mergeError = "";
  }
  async function mergeTask() {
    if (!mergeTarget || merging) return;
    const identity = mergeTarget.id;
    merging = true;
    mergeError = "";
    try {
      const result = await api<ProjectMerge>(
        "/tasks/" + identity + "/merge",
        "POST",
      );
      closeMergeDialog();
      const count = result.files.length;
      toast = result.already_applied
        ? "These task changes are already present in " +
          result.project_path +
          "."
        : `Merged ${count} ${count === 1 ? "file" : "files"} into ${result.project_path} on ${result.branch}. The changes are uncommitted.`;
      await refresh();
    } catch (e) {
      mergeError = (e as Error).message;
    } finally {
      merging = false;
    }
  }
  async function deleteTask() {
    if (!deleteTarget || deleting) return;
    const identity = deleteTarget.id;
    deleting = true;
    deleteError = "";
    try {
      const result = await api<{
        workspace_removed: boolean;
        workspace: string;
      }>("/tasks/" + identity, "DELETE", { remove_workspace: removeWorkspace });
      ++selection;
      if (active?.id === identity) {
        active = null;
        selectedFile = "";
        fileText = "";
        originalText = "";
        localStorage.removeItem("sdlc.active");
      }
      if (parentTaskId === identity) parentTaskId = "";
      localStorage.removeItem("sdlc.review-draft." + identity);
      delete messageDrafts[identity];
      delete messageErrors[identity];
      if (home) home.tasks = home.tasks.filter((task) => task.id !== identity);
      error = "";
      toast = result.workspace_removed
        ? "Task and its isolated workspace deleted."
        : "Task deleted. Workspace kept at " + result.workspace;
      deleteDialog?.close();
      await refresh();
    } catch (e) {
      deleteError = (e as Error).message;
    } finally {
      deleting = false;
    }
  }
  async function selectTask(task: Task) {
    if (
      fileText !== originalText &&
      !confirm("Discard your unsaved editor changes?")
    )
      return;
    const selectionId = ++selection;
    const next = await api<Task>("/tasks/" + task.id);
    if (selectionId !== selection) return;
    if (active?.id !== next.id) {
      recoveryProfileId = "";
      acceptanceNote = "";
    }
    active = next;
    reviewPath = "";
    traceWorkflow = "";
    ensureMessageDraft(next);
    view = "workspace";
    composer = false;
    tab = "activity";
    selectedFile = "";
    fileText = "";
    originalText = "";
    feedback = "";
    lastGate = "";
    localStorage.setItem("sdlc.active", task.id);
  }
  function newTask() {
    if (!profileId || profileId === "demo") profileId = preferredTaskProfile();
    composer = true;
    view = "workspace";
    parentTaskId = "";
    setTimeout(() => promptEl?.focus(), 0);
  }
  async function startTask() {
    await attempt(async () => {
      const task = await api<Task>("/tasks", "POST", {
        id: crypto.randomUUID().replaceAll("-", ""),
        project_id: projectId,
        profile_id: profileId,
        mode,
        prompt,
        parent_task_id: parentTaskId,
      });
      localStorage.removeItem("sdlc.draft");
      prompt = "";
      parentTaskId = "";
      await selectTask(task);
      await refresh();
    });
  }
  async function demo() {
    await attempt(async () => {
      const item = await api<Project>("/demo", "POST");
      await refresh();
      projectId = item.id;
      prompt = "Fix the greeting so the existing unittest passes.";
      mode = "change";
      newTask();
      // The explicitly requested tour remains scripted and needs no API key.
      profileId = "demo";
    });
  }
  async function addProject() {
    await attempt(async () => {
      const item = await api<Project>("/projects", "POST", {
        path: projectPath,
      });
      projectPath = "";
      await refresh();
      projectId = item.id;
      toast = "Repository added. New tasks start from its committed HEAD.";
    });
  }
  async function testProvider(identity: string) {
    if (testingProfiles[identity]) return;
    testingProfiles[identity] = true;
    providerChecks[identity] = undefined;
    providerCheckErrors[identity] = "";
    try {
      providerChecks[identity] = await api<ProviderCheck>(
        "/profiles/" + identity + "/test",
        "POST",
      );
    } catch (e) {
      providerCheckErrors[identity] =
        "The connection test could not complete: " +
        (e as Error).message +
        ". Check that boltzmann is running and restart it if you just updated the app.";
    } finally {
      testingProfiles[identity] = false;
    }
  }
  async function control(command: string, approved = true) {
    await attempt(async () => {
      await api("/tasks/" + active!.id + "/control", "POST", {
        command,
        gate_id: progress?.gate?.id ?? "",
        approved,
        text:
          command === "accept" && progress?.engine === "v2"
            ? acceptanceNote
            : feedback,
      });
      await refresh();
    });
  }
  function openReview(path = "") {
    reviewPath = path;
    reviewRequest += 1;
    tab = "diff";
  }
  function openTrace(identity?: string) {
    traceWorkflow = identity ?? active?.workflow_id ?? "";
    tab = "debug";
  }
  function requestReviewFixes(prompt: string) {
    if (!active) return;
    ensureMessageDraft(active);
    if (
      messageDraft?.prompt.trim() &&
      !confirm(
        "Replace your current follow-up draft with these review comments?",
      )
    )
      return;
    editMessage({ prompt, mode: "change" });
    toast =
      "Review comments added to your follow-up. Check the request and select Send message.";
    tab = "activity";
    setTimeout(() => {
      messageEl?.focus();
      messageEl?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
  }
  async function selectTab(next: string) {
    tab = next;
    await attempt(async () => {
      if (next === "debug") traceWorkflow = active?.workflow_id ?? "";
      if (next === "files")
        fileNames = await api<string[]>("/tasks/" + active!.id + "/files");
    });
  }
  async function readFile(path: string) {
    if (
      fileText !== originalText &&
      !confirm("Discard your unsaved editor changes?")
    )
      return;
    await attempt(async () => {
      const f = await api<{ content: string; hash: string }>(
        "/tasks/" + active!.id + "/file?path=" + encodeURIComponent(path),
      );
      selectedFile = path;
      fileText = f.content;
      originalText = f.content;
      fileHash = f.hash;
    });
  }
  async function saveFile() {
    await attempt(async () => {
      const result = await api<{ hash: string }>(
        "/tasks/" + active!.id + "/file",
        "PUT",
        { path: selectedFile, content: fileText, expected_hash: fileHash },
      );
      fileHash = result.hash;
      originalText = fileText;
      toast =
        "Saved in the task workspace. Run checks again to verify your edit.";
    });
  }
  async function saveProfile() {
    await attempt(async () => {
      const item = await api<{ id: string }>("/profiles", "POST", {
        label,
        provider,
        model,
        base_url: provider === "compatible" ? baseUrl : "",
        api_key: apiKey,
        env_var: envVar,
        max_steps: maxSteps,
      });
      apiKey = "";
      label = "";
      model = "";
      envVar = "";
      await refresh();
      profileId = item.id;
      if (progress?.status === "failed") recoveryProfileId = item.id;
      toast =
        "Provider profile saved. Select Test connection to verify it before starting a task.";
    });
  }
  async function removeProfile(identity: string) {
    if (
      !confirm(
        "Remove this profile and its saved key from the OS keyring? Active tasks must be stopped first.",
      )
    )
      return;
    await attempt(async () => {
      await api("/profiles/" + identity, "DELETE");
      if (profileId === identity) profileId = "";
      await refresh();
      toast =
        "Profile removed. Environment variables, if used, are managed outside the app.";
    });
  }
  function forkTask() {
    const previous = active!;
    newTask();
    parentTaskId = previous.id;
    projectId = previous.project_id;
    prompt =
      progress?.status === "failed"
        ? (currentTurn?.prompt ??
          previous.input?.prompt ??
          progress.entries.find((e) => e.id === "request")?.text ??
          "")
        : "";
    mode = currentTurn?.mode ?? previous.mode;
    const candidate =
      progress?.status === "failed" && recoveryProfileId
        ? recoveryProfileId
        : (currentTurn?.profile_id ?? previous.profile_id);
    profileId = home?.profiles.some(
      (p) => p.id === candidate && p.available !== false,
    )
      ? candidate
      : "";
    toast =
      progress?.status === "failed"
        ? "Original request restored. Review it and select your profile before starting; this creates a new task with the preserved changes."
        : "Fork from this task’s changes into a separate task. Describe what to do next.";
  }
  function ensureMessageDraft(task: Task) {
    if (messageDrafts[task.id]) return;
    const latest = task.snapshot?.conversation?.value.turns.at(-1);
    const candidate = latest?.profile_id ?? task.profile_id;
    const profile = home?.profiles.find(
      (p) => p.id === candidate && p.available !== false,
    );
    messageDrafts[task.id] = {
      prompt: "",
      mode: latest?.mode ?? task.mode,
      profile_id: profile?.id ?? "",
      max_steps: latest?.max_steps ?? profile?.max_steps ?? 24,
    };
  }
  function editMessage(update: Partial<MessageDraft>) {
    if (!active || !messageDraft || messageSending) return;
    messageDrafts[active.id] = {
      ...messageDraft,
      ...update,
      attempt: undefined,
    };
    messageErrors[active.id] = "";
  }
  function continueTask() {
    if (!active) return;
    ensureMessageDraft(active);
    if (progress?.status === "failed" && !messageDraft?.prompt) {
      editMessage({
        prompt:
          currentTurn?.prompt ??
          active.input?.prompt ??
          "Continue from where you stopped.",
      });
    }
    if (recoveryProfileId) editMessage({ profile_id: recoveryProfileId });
    tab = "activity";
    setTimeout(() => {
      messageEl?.focus();
      messageEl?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
  }
  async function sendMessage() {
    if (!active || !messageDraft || !canSendMessage) return;
    const identity = active.id;
    const draft = messageDraft;
    draft.attempt ??= {
      id: crypto.randomUUID().replaceAll("-", ""),
      expected_turn: active.next_turn ?? 2,
    };
    messageSending = identity;
    messageErrors[identity] = "";
    try {
      const receipt = await api<MessageReceipt>(
        "/tasks/" + identity + "/messages",
        "POST",
        {
          ...draft.attempt,
          prompt: draft.prompt,
          mode: draft.mode,
          profile_id: draft.profile_id,
          max_steps: draft.max_steps,
        },
      );
      if (receipt.status === "rejected") {
        messageErrors[identity] =
          receipt.error ??
          "Message was not accepted. Review it and send again.";
        draft.attempt = undefined;
      } else {
        messageDrafts[identity] = { ...draft, prompt: "", attempt: undefined };
      }
      await refresh();
    } catch (e) {
      messageErrors[identity] = (e as Error).message;
      if (e instanceof ApiError && e.status >= 400 && e.status < 500)
        draft.attempt = undefined;
      // Keep the same ID after an uncertain network result, so retrying is safe.
      await refresh();
    } finally {
      messageSending = "";
    }
  }
  async function unlock() {
    await attempt(async () => {
      await api("/unlock", "POST", { token: unlockToken });
      unlockToken = "";
      await refresh();
    });
  }
  function keyboard(event: KeyboardEvent) {
    if ((event.metaKey || event.ctrlKey) && event.key === "k") {
      event.preventDefault();
      newTask();
    }
    if ((event.metaKey || event.ctrlKey) && event.key === ",") {
      event.preventDefault();
      view = "settings";
    }
    if (event.key === "Escape") composer = false;
  }
  onMount(() => {
    let disposed = false;
    (async () => {
      const fragment = new URLSearchParams(location.hash.slice(1));
      if (fragment.has("token")) {
        try {
          await api("/unlock", "POST", { token: fragment.get("token") });
        } catch (e) {
          error = (e as Error).message;
        }
        history.replaceState(null, "", location.pathname);
      }
      prompt = localStorage.getItem("sdlc.draft") ?? "";
      try {
        messageDrafts = JSON.parse(
          localStorage.getItem("sdlc.message-drafts") ?? "{}",
        );
      } catch {
        messageDrafts = {};
      }
      draftsLoaded = true;
      await refresh();
      const remembered = home?.tasks.find(
        (t) => t.id === localStorage.getItem("sdlc.active"),
      );
      if (remembered && !disposed) await selectTask(remembered);
    })();
    const timer = setInterval(refresh, 2000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  });
</script>

<svelte:window
  onkeydown={keyboard}
  onbeforeunload={(event) => {
    if (fileText !== originalText) event.preventDefault();
  }}
/>

{#if locked}
  <main class="unlock">
    <div class="brandmark" aria-hidden="true">
      <Brain class="brand-icon" size={23} strokeWidth={1.75} />
    </div>
    <span class="eyebrow">YOUR LOCAL DEVELOPMENT DESK</span>
    <h1>Welcome to boltzmann.</h1>
    <p>
      Open the launch URL printed by <code>sdlc</code>, or enter its local
      session token.
    </p>
    <form
      onsubmit={(e) => {
        e.preventDefault();
        unlock();
      }}
    >
      <input
        type="password"
        aria-label="Launch token"
        bind:value={unlockToken}
        autocomplete="off"
      /><button class="primary" disabled={busy}
        >Unlock <ArrowRight size={16} /></button
      >
    </form>
    {#if error}<p class="error">{error}</p>{/if}
  </main>
{:else}
  <div class="app-shell">
    <aside class="sidebar">
      <button
        class="brand"
        aria-label="boltzmann home"
        onclick={() => {
          view = "workspace";
          composer = false;
          active = null;
        }}
        ><span class="brandmark" aria-hidden="true"
          ><Brain class="brand-icon" size={23} strokeWidth={1.75} /></span
        ><span
          >boltzmann<span class="subbrand">DEVELOPMENT JUST HAPPENS</span></span
        ></button
      >
      <button class="new-task" onclick={newTask}
        ><Plus size={17} /> New task <kbd>⌘ K</kbd></button
      >
      <div class="sidebar-label">
        WORKSPACE <span>{home?.projects.length ?? 0}</span>
      </div>
      {#each home?.projects ?? [] as item}
        <button
          class:project-selected={projectId === item.id}
          class="project-row"
          onclick={() => {
            projectId = item.id;
            newTask();
          }}
          ><FolderOpen size={15} /><span>{item.name}</span>{#if item.demo}<span
              class="tiny-pill">DEMO</span
            >{/if}</button
        >
      {/each}
      <button class="text-button add-repo" onclick={() => (view = "settings")}
        ><Plus size={14} /> Add repository</button
      >
      <div class="sidebar-label tasks-label">
        TASKS <span>{home?.tasks.length ?? 0}</span>
      </div>
      <div class="search-field">
        <Search size={14} /><input
          placeholder="Find a task…"
          aria-label="Find a task"
          bind:value={query}
        />
      </div>
      <nav class="task-list" aria-label="Task history">
        {#each filteredTasks as task}
          <button
            class:selected={active?.id === task.id && view === "workspace"}
            class="task-item"
            onclick={() => attempt(() => selectTask(task))}
          >
            <span
              class="task-dot"
              class:attention={task.snapshot?.value.status === "needs_you"}
              class:done={task.snapshot?.value.status === "accepted"}
            ></span>
            <span
              ><strong>{task.title}</strong><small
                >{task.project_name} <span>·</span>
                {statusLabel(task.snapshot?.value.status)}</small
              ></span
            >
          </button>
        {/each}
        {#if !filteredTasks.length}<p class="sidebar-empty">
            Your work, decisions, and progress will live here.
          </p>{/if}
      </nav>
      <div class="sidebar-bottom">
        <button
          class:nav-active={view === "settings"}
          onclick={() => (view = "settings")}
          ><Settings2 size={16} /> Settings <kbd>⌘ ,</kbd></button
        >
        <div class="local-status">
          <span class:online={home?.health.temporal}></span> Local workspace
          <span class="version">V2</span>
        </div>
      </div>
    </aside>

    <div class="main-shell">
      <header class="topbar">
        <div>
          <span class="muted">Workspace</span><ChevronRight size={13} /><span
            >{view === "settings"
              ? "Settings"
              : active && !composer
                ? active.project_name
                : "Overview"}</span
          >
        </div>
        <div class="topbar-right">
          <span class="local-badge"><ShieldCheck size={13} /> LOCAL FIRST</span
          ><button
            class="icon-button"
            title="Enable desktop notifications"
            onclick={() => {
              if ("Notification" in window) Notification.requestPermission();
            }}><Bell size={16} /></button
          >
        </div>
      </header>
      {#if error}<div class="banner error" role="alert">
          <AlertCircle size={16} />{error}<button
            aria-label="Dismiss error"
            onclick={() => (error = "")}><X size={15} /></button
          >
        </div>{/if}
      {#if toast}<div class="banner notice" role="status">
          {toast}<button
            aria-label="Dismiss notice"
            onclick={() => (toast = "")}><X size={15} /></button
          >
        </div>{/if}

      {#if view === "settings"}
        <main class="settings-page">
          <span class="eyebrow">MAKE YOURSELF AT HOME</span>
          <h1>Your development setup.</h1>
          <p class="lede">Your repositories, your models, your machine.</p>
          <section class="settings-section">
            <div>
              <h2>Repositories</h2>
              <p>
                Use any local Git repository, including a GitHub checkout. Tasks
                start in a separate clone from committed HEAD.
              </p>
            </div>
            <div class="settings-body">
              {#each home?.projects ?? [] as item}<div class="repository-card">
                  <FolderOpen size={18} />
                  <div>
                    <strong>{item.name}</strong><code>{item.path}</code>
                  </div>
                  {#if item.github_url}<a
                      href={item.github_url}
                      target="_blank"
                      rel="noreferrer"
                      aria-label="Open GitHub repository"
                      ><ArrowUpRight size={17} /></a
                    >{/if}
                </div>{/each}
              <form
                class="inline-form"
                onsubmit={(e) => {
                  e.preventDefault();
                  addProject();
                }}
              >
                <input
                  placeholder="/absolute/path/to/repository"
                  aria-label="Repository path"
                  bind:value={projectPath}
                  required
                /><button disabled={busy}><Plus size={15} /> Add</button>
              </form>
              <button class="text-button" onclick={demo}
                >Try the built-in demo repository <ArrowRight
                  size={14}
                /></button
              >
            </div>
          </section>
          <section class="settings-section">
            <div>
              <h2>Model profiles</h2>
              <p>
                Keys are stored in your OS keyring. Environment references are
                also supported. Each task keeps a snapshot of its profile.
                OpenAI is supported for new tasks in this milestone.
              </p>
            </div>
            <div class="settings-body">
              <p class="provider-test-help">
                Test connection makes one model request using the saved
                credential and the agent's action schema. Provider charges may
                apply. No repository content is sent.
              </p>
              {#each home?.profiles ?? [] as item}
                {@const result = providerChecks[item.id]}
                <div class="profile-group">
                  <div class="profile-card">
                    <div class="provider-icon"><Code2 size={16} /></div>
                    <div class="profile-identity">
                      <strong>{item.label}</strong><small
                        >{item.provider}
                        {item.model
                          ? " / " + item.model
                          : " · scripted walkthrough"}</small
                      >
                    </div>
                    <span class="tiny-pill">{item.max_steps} CALLS</span>
                    {#if item.id !== "demo"}
                      <button
                        class="secondary provider-test-button"
                        aria-label={"Test connection for " + item.label}
                        disabled={busy ||
                          testingProfiles[item.id] ||
                          item.available === false}
                        onclick={() => testProvider(item.id)}
                        ><Activity size={14} />{testingProfiles[item.id]
                          ? "Testing…"
                          : "Test connection"}</button
                      >
                      <button
                        class="icon-button"
                        aria-label={"Remove profile " + item.label}
                        disabled={busy || testingProfiles[item.id]}
                        onclick={() => removeProfile(item.id)}
                        ><X size={14} /></button
                      >{/if}
                  </div>
                  {#if item.available === false}<p class="provider-test-help">
                      Saved for existing tasks. Add an OpenAI profile for new
                      work; other integrations will return in a future
                      milestone.
                    </p>{/if}
                  <div aria-live="polite" aria-atomic="true">
                    {#if testingProfiles[item.id]}
                      <div class="provider-check">
                        Reading the saved credential and checking the model's
                        structured response… This can take up to 45 seconds.
                      </div>
                    {:else if providerCheckErrors[item.id]}
                      <div class="provider-check failed">
                        <strong>Test unavailable</strong>
                        <p>{providerCheckErrors[item.id]}</p>
                      </div>
                    {:else if result}
                      <div
                        class="provider-check"
                        class:passed={result.ok}
                        class:failed={!result.ok}
                      >
                        <div class="provider-check-heading">
                          {#if result.ok}<ShieldCheck
                              size={18}
                            />{:else}<AlertCircle size={18} />{/if}
                          <strong>{result.title}</strong>
                          <span>{(result.elapsed_ms / 1000).toFixed(1)}s</span>
                        </div>
                        <p>{result.explanation}</p>
                        <dl>
                          <div>
                            <dt>Credential</dt>
                            <dd>{result.credential_source}</dd>
                          </div>
                          <div>
                            <dt>
                              {result.ok ? "Completed" : "Stopped during"}
                            </dt>
                            <dd>
                              {{
                                credential: "Read credential",
                                request: "Provider request",
                                response: "Validate agent response",
                              }[result.stage]}
                            </dd>
                          </div>
                          {#if result.endpoint}<div>
                              <dt>Endpoint</dt>
                              <dd><code>{result.endpoint}</code></dd>
                            </div>{/if}
                          {#if result.http_status}<div>
                              <dt>HTTP status</dt>
                              <dd>{result.http_status}</dd>
                            </div>{/if}
                          {#if result.provider_code}<div>
                              <dt>Provider code</dt>
                              <dd><code>{result.provider_code}</code></dd>
                            </div>{/if}
                          {#if result.parameter}<div>
                              <dt>Rejected parameter</dt>
                              <dd><code>{result.parameter}</code></dd>
                            </div>{/if}
                        </dl>
                        {#if result.provider_message}<div
                            class="provider-message"
                          >
                            <strong
                              >Provider message · credentials redacted</strong
                            >
                            <pre>{result.provider_message}</pre>
                          </div>{/if}
                        <ol>
                          {#each result.actions as action}<li>
                              {action}
                            </li>{/each}
                        </ol>
                      </div>
                    {/if}
                  </div>
                </div>{/each}
              <form
                class="profile-form"
                onsubmit={(e) => {
                  e.preventDefault();
                  saveProfile();
                }}
              >
                <h3>Add a provider</h3>
                <div class="form-grid">
                  <label
                    >Profile name<input
                      bind:value={label}
                      placeholder="My daily model"
                      required
                    /></label
                  ><label
                    >Provider<select bind:value={provider}
                      ><option value="openai">OpenAI</option></select
                    ></label
                  >
                </div>
                <label
                  >Model ID<input
                    bind:value={model}
                    placeholder="Enter a model available to your account"
                    required
                  /></label
                >{#if provider === "compatible"}<label
                    >API base URL<input
                      type="url"
                      bind:value={baseUrl}
                      placeholder="https://provider.example/v1"
                      required
                    /></label
                  >{/if}
                <div class="form-grid">
                  <label
                    >API key <span class="muted">→ OS keyring</span><input
                      type="password"
                      bind:value={apiKey}
                      autocomplete="new-password"
                      placeholder="Never stored in the task history"
                    /></label
                  ><label
                    >Or environment variable<input
                      bind:value={envVar}
                      placeholder="OPENAI_API_KEY"
                    /></label
                  >
                </div>
                <label
                  >Maximum model calls per task<input
                    type="number"
                    min="3"
                    max="200"
                    bind:value={maxSteps}
                  /></label
                ><button class="primary" disabled={busy}
                  >Save profile <ArrowRight size={15} /></button
                >
              </form>
            </div>
          </section>
          <section class="settings-section">
            <div>
              <h2>Runtime</h2>
              <p>
                A durable local worker and a persistent Temporal development
                server.
              </p>
            </div>
            <div class="runtime-card">
              <div>
                <span>Temporal + worker</span><strong
                  >{home?.health.temporal
                    ? "Connected"
                    : "Reconnecting"}</strong
                >
              </div>
              <p>
                Temporal is required while tasks run. Launch with
                <code>./apps/sdlc/dev</code> to start the app, worker, and local
                Temporal server together. Keep that terminal running. Install
                the Temporal CLI first; a separate server is only needed when
                you choose <code>--external-temporal</code>.
              </p>
              <div>
                <span>Harness package</span><code
                  >{home?.health.harness_version}</code
                >
              </div>
              <div>
                <span>Execution</span><strong
                  >Host · approval for every command</strong
                >
              </div>
              <p>
                Commands run on your computer and can access its files and
                network. Review each command before allowing it. Credentials are
                removed from the child environment; this is not a sandbox.
              </p>
              <code>{home?.health.data_dir}</code>
            </div>
          </section>
        </main>
      {:else if composer || !active}
        <main class="overview">
          <div class="overview-heading">
            <span class="eyebrow">YOUR DEVELOPMENT DESK</span>
            <h1>What are we building?</h1>
            <p class="lede">
              From the first question to a change you can trust.
            </p>
          </div>
          <section class="compose-card">
            <div class="compose-title">
              <div class="mini-mark"><Command size={18} /></div>
              <h2>
                {parentTaskId ? "Continue this work" : "Start with an idea"}
              </h2>
              <span class="tiny-pill">CODING AGENT V2</span>
            </div>
            <form
              onsubmit={(e) => {
                e.preventDefault();
                startTask();
              }}
            >
              <div class="mode-switch">
                <button
                  type="button"
                  class:chosen={mode === "change"}
                  onclick={() => (mode = "change")}
                  ><Code2 size={15} /> Make a change</button
                ><button
                  type="button"
                  class:chosen={mode === "ask"}
                  onclick={() => (mode = "ask")}
                  ><MessageSquare size={15} /> Ask the codebase</button
                >
              </div>
              <textarea
                class="prompt-input"
                bind:this={promptEl}
                bind:value={prompt}
                oninput={(event) =>
                  localStorage.setItem("sdlc.draft", event.currentTarget.value)}
                placeholder={mode === "ask"
                  ? "What would you like to understand about this repository?"
                  : "Describe a feature, a bug, or a small improvement…"}
                aria-label="Task description"
                required
                rows="5"></textarea>
              <div class="compose-options">
                <label
                  ><FolderOpen size={14} /><select
                    aria-label="Task repository"
                    bind:value={projectId}
                    required
                    ><option value="" disabled>Select repository</option
                    >{#each home?.projects ?? [] as item}<option value={item.id}
                        >{item.name}</option
                      >{/each}</select
                  ></label
                ><label
                  ><Activity size={14} /><select
                    aria-label="Model profile"
                    bind:value={profileId}
                    required
                    ><option value="" disabled>Select model profile</option
                    >{#each taskProfiles as item}<option value={item.id}
                        >{item.label}</option
                      >{/each}</select
                  ></label
                ><button
                  class="primary"
                  disabled={busy || !projectId || !profileId || !prompt.trim()}
                  >Start task <ArrowRight size={16} /></button
                >
              </div>
            </form>
            <div class="compose-foot">
              <GitBranch size={13} />{parentTaskId
                ? "Carries forward the previous workspace changes."
                : "A separate workspace. Your checkout stays yours."}<span
                >Plan → Build → Check → Review</span
              >
            </div>
          </section>
          <div class="welcome-grid">
            <button class="welcome-card" onclick={demo}
              ><div class="card-icon green"><FlaskConical size={21} /></div>
              <span class="eyebrow">A TWO-MINUTE TOUR</span>
              <h3>Take it for a test drive.</h3>
              <p>
                A tiny repository. One broken greeting. Follow a real durable
                workflow without an API key.
              </p>
              <span class="card-link"
                >Open the demo <ArrowUpRight size={16} /></span
              ></button
            ><button class="welcome-card" onclick={() => (view = "settings")}
              ><div class="card-icon"><FolderOpen size={21} /></div>
              <span class="eyebrow">BRING YOUR OWN PROJECT</span>
              <h3>Your next small win.</h3>
              <p>
                Connect a local repository and a model profile. Start with a
                question or a focused change.
              </p>
              <span class="card-link"
                >Set up your workspace <ArrowUpRight size={16} /></span
              ></button
            >
          </div>
          <div class="milestone-note">
            <span class="tiny-pill">MILESTONE 03</span>
            <p>
              Follow live agent execution, review files, and send targeted
              corrections in the same task. Ready for your testing before GitHub
              delivery and CI feedback.
            </p>
          </div>
        </main>
      {:else}
        <main class="task-page">
          <div class="task-heading">
            <div>
              <div class="task-meta">
                <span class="eyebrow"
                  >{(progress?.mode ?? active.mode) === "ask"
                    ? "CODEBASE QUESTION"
                    : "DEVELOPMENT TASK"}</span
                ><span class="mono">#{active.id.slice(0, 7)}</span>
              </div>
              <h1>{active.title}</h1>
              <div class="task-subtitle">
                <GitBranch size={14} /><span>sdlc/{active.id.slice(0, 8)}</span
                ><span>·</span><span
                  >{active.base_commit?.slice(0, 8) ??
                    "Preparing workspace"}</span
                >{#if active.source_dirty}<span class="dirty-label"
                    >Source has uncommitted changes · using HEAD</span
                  >{/if}
              </div>
            </div>
            <span
              class="status-pill"
              class:needs-you={progress?.status === "needs_you"}
              class:accepted={progress?.status === "accepted"}
              ><span></span>{statusLabel(progress?.status)}</span
            >
          </div>
          {#if active.live === false && active.snapshot}<div
              class="banner notice"
            >
              <RefreshCw size={15} />Reconnecting. Showing the last saved state;
              controls will resume with the worker.
            </div>{/if}
          <section class="progress-strip">
            <div class="progress-current">
              <span
                class="live-indicator"
                class:pulsing={progress?.status === "running"}
                ><Activity size={18} /></span
              >
              <div>
                <span class="eyebrow">{progress?.phase ?? "QUEUED"}</span
                ><strong
                  >{progress?.focus ??
                    active.preparation_error ??
                    active.dispatch_error ??
                    "Preparing your workspace and starting the agent"}</strong
                >
              </div>
            </div>
            <div class="task-controls">
              {#if active.engine === "v2" && progress?.status === "accepted" && (progress.mode ?? active.mode) === "change" && active.workspace}
                {#if projectMergeCurrent}<button
                    disabled
                    title={"Merged into " +
                      progress.project_merge?.project_path}
                    ><Check size={14} /> Merged into project</button
                  >{:else}<button
                    class="primary"
                    disabled={busy || !active.live}
                    onclick={confirmMerge}
                    title="Write the accepted changes into the project checkout"
                    ><GitMerge size={14} /> Merge into project</button
                  >{/if}
              {/if}{#if progress?.status === "running"}<button
                  disabled={busy}
                  onclick={() => control("pause")}
                  ><Pause size={14} /> Pause</button
                >{/if}{#if progress?.status === "paused"}<button
                  disabled={busy}
                  onclick={() => control("resume")}
                  ><Play size={14} /> Resume</button
                >{/if}{#if ["running", "paused", "needs_you"].includes(progress?.status ?? "")}<button
                  class="icon-button"
                  title="Stop after the current action"
                  disabled={busy}
                  onclick={() => control("cancel")}><Square size={14} /></button
                >{/if}{#if ["review", "failed", "cancelled", "accepted"].includes(progress?.status ?? "")}<button
                  onclick={continueTask}
                  >Continue <ArrowRight size={14} /></button
                >{/if}{#if progress?.status === "review"}<button
                  class="primary"
                  disabled={busy || !active.can_message}
                  onclick={() => control("accept")}
                  ><Check size={14} /> Accept</button
                >{/if}
              {#if active.can_message}<button
                  onclick={forkTask}
                  title="Create a separate task from this workspace"
                  ><GitBranch size={14} /> Fork task</button
                >{/if}
              <button
                disabled={busy || !canDelete}
                title={canDelete
                  ? "Delete task"
                  : "Stop the task before deleting it"}
                onclick={confirmDelete}><Trash2 size={14} /> Delete</button
              >
            </div>
          </section>
          {#if progress?.engine === "v2"}
            {#if progress.status === "review" && progress.mode === "change"}
              <label class="acceptance-note"
                >Acceptance note {progress.verification === "verified"
                  ? "(optional)"
                  : "(required to accept incomplete verification)"}
                <textarea
                  bind:value={acceptanceNote}
                  placeholder="Describe any remaining checks or findings you are accepting."
                  rows="2"></textarea>
              </label>
            {/if}
          {/if}
          <div class="task-columns">
            <section class="task-work">
              <nav class="tabs" aria-label="Task views">
                {#each [{ id: "activity", label: "Execution", icon: Activity }, { id: "diff", label: "Review", icon: Code2 }, { id: "files", label: "Files", icon: Files }, { id: "checks", label: "Checks", icon: Terminal }, { id: "debug", label: "Harness", icon: Activity }] as item}<button
                    class:active={tab === item.id}
                    onclick={() => selectTab(item.id)}
                    ><item.icon
                      size={15}
                    />{item.label}{#if item.id === "diff" && progress?.files_changed.length}<span
                        >{progress.files_changed.length}</span
                      >{/if}</button
                  >{/each}
              </nav>
              {#key active.id}
                <div hidden={tab !== "diff"}>
                  <ReviewPanel
                    taskId={active.id}
                    visible={tab === "diff"}
                    initialPath={reviewPath}
                    requestKey={reviewRequest}
                    onrequest={requestReviewFixes}
                  />
                </div>
              {/key}
              {#if tab === "activity"}
                {#if progress?.engine === "v2"}
                  {#key active.id}
                    <ExecutionWorkspace
                      {progress}
                      live={!!active.live}
                      version={active.version}
                      turnCalls={currentTurn?.model_calls ??
                        progress.model_calls}
                      onfile={openReview}
                      onreview={() => openReview()}
                      ontrace={openTrace}
                      onchecks={() => selectTab("checks")}
                    />
                  {/key}
                {/if}
                <div class="activity-pane">
                  {#if failure}
                    <section
                      class="failure-card"
                      aria-label="Task error and recovery"
                    >
                      <div class="failure-heading">
                        <AlertCircle size={21} />
                        <div>
                          <span class="eyebrow">TASK NEEDS ATTENTION</span>
                          <h2>{failure.title}</h2>
                        </div>
                      </div>
                      <p>{failure.explanation}</p>
                      <dl class="failure-details">
                        <div>
                          <dt>Provider</dt>
                          <dd>{taskProfile?.provider ?? "Unknown"}</dd>
                        </div>
                        <div>
                          <dt>Model</dt>
                          <dd>
                            {progress?.model || taskProfile?.model || "Unknown"}
                          </dd>
                        </div>
                        <div>
                          <dt>Stopped during</dt>
                          <dd>{progress?.phase}</dd>
                        </div>
                        {#if failure.http_status}<div>
                            <dt>HTTP status</dt>
                            <dd>{failure.http_status}</dd>
                          </div>{/if}
                        {#if failure.provider_code}<div>
                            <dt>Provider code</dt>
                            <dd>{failure.provider_code}</dd>
                          </div>{/if}
                      </dl>
                      <h3>What to do next</h3>
                      <ol>
                        {#each failure.actions as action}<li>
                            {action
                              .replace(
                                "starting the new task",
                                "sending the follow-up",
                              )
                              .replace(
                                "start a new task from the preserved workspace",
                                "send a follow-up in this task",
                              )}
                          </li>{/each}
                      </ol>
                      <p class="failure-preserved">
                        Your workspace and existing changes are preserved.
                        Continue prepares a follow-up here. It runs when you
                        select Send message.
                      </p>
                      <div class="failure-actions">
                        <button onclick={() => (view = "settings")}
                          ><Settings2 size={14} /> Provider settings</button
                        >
                        <button onclick={() => selectTab("debug")}
                          ><Activity size={14} /> Harness details</button
                        >
                        <button class="primary" onclick={continueTask}
                          >Continue <ArrowRight size={14} /></button
                        >
                      </div>
                    </section>
                  {/if}
                  {#if progress?.gate}<section class="gate-card">
                      <div class="gate-heading">
                        <span class="gate-icon"><AlertCircle size={19} /></span>
                        <div>
                          <span class="eyebrow">YOUR INPUT IS NEEDED</span>
                          <h2>{progress.gate.title}</h2>
                        </div>
                      </div>
                      <pre>{progress.gate.detail}</pre>
                      {#if progress.gate.kind === "command"}<p
                          class="command-warning"
                        >
                          Runs in <code>{active.workspace}</code> on your computer,
                          with access to host files and network. The child environment
                          excludes your provider keys. Allow only commands you trust.
                        </p>{/if}<textarea
                        rows="2"
                        aria-label="Feedback or answer"
                        placeholder={progress.gate.kind === "question"
                          ? "Your answer…"
                          : "Optional feedback or adjustments…"}
                        bind:value={feedback}></textarea>
                      <div class="gate-actions">
                        {#if progress.gate.kind !== "question"}<button
                            disabled={busy}
                            onclick={() => control("respond", false)}
                            >Decline / revise</button
                          >{/if}<button
                          class="primary"
                          disabled={busy ||
                            (progress.gate.kind === "question" &&
                              !feedback.trim())}
                          onclick={() => control("respond", true)}
                          >{progress.gate.kind === "command"
                            ? "Allow command"
                            : progress.gate.kind === "plan"
                              ? "Approve plan"
                              : "Send answer"}<ArrowRight size={15} /></button
                        >
                      </div>
                    </section>{/if}
                  {#if messageDraft}
                    <form
                      class="message-composer"
                      aria-label="Task conversation"
                      onsubmit={(event) => {
                        event.preventDefault();
                        void sendMessage();
                      }}
                    >
                      <div class="message-heading">
                        <label for="follow-up">Continue this task</label>
                        <span>Same conversation · same workspace</span>
                      </div>
                      {#if active.pending_message}<div
                          class="message-notice"
                          role="status"
                        >
                          <strong>Sending your message…</strong>
                          <p>
                            {active.pending_message.error ??
                              "Waiting for the agent to confirm receipt."}
                          </p>
                          <p>{active.pending_message.payload.prompt}</p>
                        </div>{/if}
                      {#if active.message_error}<div
                          class="message-notice"
                          role="alert"
                        >
                          <p>{active.message_error.error}</p>
                          <button
                            type="button"
                            onclick={() => {
                              if (
                                !messageDraft.prompt ||
                                confirm(
                                  "Replace your current draft with the undelivered message?",
                                )
                              )
                                editMessage({
                                  prompt: active!.message_error!.prompt,
                                });
                            }}>Restore unsent message</button
                          >
                        </div>{/if}
                      <textarea
                        id="follow-up"
                        bind:this={messageEl}
                        rows="3"
                        maxlength="20000"
                        placeholder="Ask a follow-up, refine the feature, or describe an issue to fix…"
                        value={messageDraft.prompt}
                        disabled={Boolean(messageSending)}
                        oninput={(event) =>
                          editMessage({ prompt: event.currentTarget.value })}
                        onkeydown={(event) => {
                          if (
                            (event.metaKey || event.ctrlKey) &&
                            event.key === "Enter"
                          ) {
                            event.preventDefault();
                            void sendMessage();
                          }
                        }}></textarea>
                      <div class="message-settings">
                        <label
                          >Mode<select
                            value={messageDraft.mode}
                            disabled={Boolean(messageSending)}
                            onchange={(event) =>
                              editMessage({ mode: event.currentTarget.value })}
                          >
                            <option value="ask">Ask (read only)</option>
                            <option value="change">Make changes</option>
                          </select></label
                        >
                        <label
                          >Model profile<select
                            value={messageDraft.profile_id}
                            disabled={Boolean(messageSending)}
                            onchange={(event) => {
                              const selected = messageProfiles.find(
                                (p) => p.id === event.currentTarget.value,
                              );
                              editMessage({
                                profile_id: event.currentTarget.value,
                                max_steps: selected?.max_steps ?? 24,
                              });
                            }}
                          >
                            <option value="" disabled>Select a profile</option>
                            {#each messageProfiles as profile}<option
                                value={profile.id}>{profile.label}</option
                              >{/each}
                          </select></label
                        >
                        <label
                          >Max calls / message<input
                            type="number"
                            min="3"
                            max="200"
                            step="1"
                            value={messageDraft.max_steps}
                            disabled={Boolean(messageSending)}
                            oninput={(event) =>
                              editMessage({
                                max_steps: event.currentTarget.valueAsNumber,
                              })}
                          /></label
                        >
                      </div>
                      {#if messageErrors[active.id]}<p
                          class="error"
                          role="alert"
                        >
                          {messageErrors[active.id]}
                        </p>{/if}
                      <div class="message-footer">
                        <p>
                          {fileText !== originalText
                            ? "Save or discard your file edits before sending."
                            : !active.live
                              ? "Reconnect to boltzmann to send. Your draft is saved in this browser."
                              : progress?.gate
                                ? "Respond to the approval or question above first. You can draft your next message here."
                                : progress?.status === "paused"
                                  ? "Resume or stop the current message before sending. Your draft is saved."
                                  : !active.can_message
                                    ? "You can draft while the agent works. Send when it finishes or after you stop it."
                                    : messageDraft.mode === "change"
                                      ? "New changes start with a plan for your approval. ⌘ / Ctrl + Enter to send."
                                      : "Ask uses the existing conversation and files. It cannot edit or run commands."}
                        </p>
                        <button
                          class="primary"
                          type="submit"
                          disabled={!canSendMessage}
                        >
                          {messageSending === active.id
                            ? "Sending…"
                            : "Send message"}<ArrowRight size={15} />
                        </button>
                      </div>
                    </form>
                  {/if}
                  <details class="activity-history">
                    <summary
                      >Activity · {progress?.entries.length ?? 0} updates
                      <span>Latest first</span></summary
                    >
                    <div class="timeline">
                      {#each [...(progress?.entries ?? [])].reverse() as entry}<article
                          class="timeline-entry"
                        >
                          <div
                            class="entry-avatar"
                            class:you={entry.role === "you"}
                          >
                            {#if entry.role === "agent"}
                              <Brain
                                class="brand-icon"
                                size={18}
                                strokeWidth={1.75}
                                aria-hidden="true"
                              />
                            {:else}
                              {entry.role === "you" ? "Y" : "!"}
                            {/if}
                          </div>
                          <div>
                            <div class="entry-meta">
                              <strong
                                >{entry.role === "you"
                                  ? "You"
                                  : entry.role === "agent"
                                    ? "boltzmann"
                                    : entry.role === "implementer"
                                      ? "Implementer"
                                      : entry.role === "reviewer"
                                        ? "Independent reviewer"
                                        : "Tool result"}</strong
                              ><span
                                >{entry.id === "request"
                                  ? "THE REQUEST"
                                  : entry.id.endsWith("-request")
                                    ? "FOLLOW-UP"
                                    : entry.role === "agent"
                                      ? "AGENT UPDATE"
                                      : ""}</span
                              >
                            </div>
                            <p>{entry.text}</p>
                          </div>
                        </article>{/each}{#if !progress?.entries.length}<div
                          class="empty-pane"
                        >
                          <Activity size={23} />
                          <h3>Starting your durable agent…</h3>
                          <p>
                            You can close this tab and return. The task belongs
                            to the worker.
                          </p>
                        </div>{/if}
                    </div>
                  </details>
                </div>
              {:else if tab === "files"}<div class="file-browser">
                  <nav aria-label="Workspace files">
                    {#each fileNames as name}<button
                        class:chosen={name === selectedFile}
                        onclick={() => readFile(name)}>{name}</button
                      >{/each}
                  </nav>
                  <div class="editor-pane">
                    {#if selectedFile}<div class="pane-toolbar">
                        <code>{selectedFile}</code><button
                          class="primary"
                          disabled={!editable ||
                            busy ||
                            fileText === originalText}
                          onclick={saveFile}
                          >Save{fileText !== originalText ? " •" : ""}</button
                        >
                      </div>
                      <textarea
                        class="file-editor"
                        aria-label="File contents"
                        bind:value={fileText}
                        readonly={!editable}
                        spellcheck="false"></textarea>
                      <p class="editor-note">
                        {editable
                          ? "Edits stay in this task’s workspace. Saving requires the file hash to still match."
                          : "Pause the agent before editing. Files are read-only while it is working."}
                      </p>{:else}<div class="empty-pane">
                        <Files size={24} />
                        <h3>Look under the hood.</h3>
                        <p>
                          Select a file to inspect it. Pause the agent to take
                          over.
                        </p>
                      </div>{/if}
                  </div>
                </div>
              {:else if tab === "checks"}<div class="checks-pane">
                  {#each progress?.checks ?? [] as check}<section
                      class="check-card"
                    >
                      <div>
                        <span
                          class:check-failed={check.exit_code !== 0 ||
                            check.stale}
                          class="check-result"
                          >{check.stale
                            ? "BEFORE LATEST EDIT"
                            : check.exit_code === 0
                              ? "PASS"
                              : "FAIL"}</span
                        ><code>{check.command}</code><span
                          >exit {check.exit_code}</span
                        >
                      </div>
                      <pre>{check.output}</pre>
                      {#if check.revision}<p class="coding-caption">
                          Checked revision <code
                            >{check.revision.slice(0, 12)}</code
                          >
                          · after command
                          <code
                            >{check.after_revision?.slice(0, 12) ||
                              "unknown"}</code
                          >
                        </p>{/if}
                    </section>{/each}{#if !progress?.checks.length}<div
                      class="empty-pane"
                    >
                      <Terminal size={25} />
                      <h3>Evidence, not guesses.</h3>
                      <p>
                        Commands need your approval. Their exit status and
                        output will appear here.
                      </p>
                    </div>{/if}
                </div>
              {:else if tab === "debug"}<div class="debug-notice">
                  <Activity size={14} />Embedded harness UI · inspect tools,
                  model calls, and internal state. Use boltzmann to control the
                  task. In the debugger, F toggles focus and Esc shows all
                  panes.
                </div>
                <!-- The harness's b=graph URL setting is the native F-key view. -->
                <iframe
                  class="harness-frame"
                  title="Agent harness debugger"
                  src={"/harness/?b=graph&s=" +
                    encodeURIComponent(traceWorkflow || active.workflow_id)}
                ></iframe>{/if}
            </section>
            <details class="task-details">
              <summary>Session, plan & workspace details</summary>
              <aside class="task-context">
                <section>
                  <span class="eyebrow">THE WAY FORWARD</span>
                  <h3>Task plan</h3>
                  <ol class="plan-list">
                    {#each progress?.steps ?? [] as step}<li
                        class:step-complete={step.status === "completed"}
                        class:step-active={step.status === "active"}
                      >
                        {#if step.status === "completed"}<Check
                            size={15}
                          />{:else}<Circle size={13} />{/if}<span
                          >{step.title}{#if step.status === "active"}<small
                              class="current-step">CURRENT</small
                            >{/if}</span
                        >
                      </li>{/each}
                  </ol>
                  <div class="next-action">
                    <ArrowRight size={15} />
                    <p>
                      {progress?.next_action ??
                        "The agent will inspect the repository first."}
                    </p>
                  </div>
                </section>
                {#if progress?.risks.length}<section>
                    <span class="eyebrow">ASSUMPTIONS & RISKS</span
                    >{#each progress.risks as risk}<p class="risk-note">
                        {risk}
                      </p>{/each}
                  </section>{/if}
                <section>
                  <span class="eyebrow">SESSION DETAILS</span>
                  <dl>
                    <div>
                      <dt>Model</dt>
                      <dd>{progress?.model ?? "Starting"}</dd>
                    </div>
                    <div>
                      <dt>Message {currentTurn?.number ?? 1} calls</dt>
                      <dd>
                        {currentTurn?.model_calls ?? progress?.model_calls ?? 0} /
                        {progress?.max_steps ?? "–"}
                      </dd>
                    </div>
                    <div>
                      <dt>Total model calls</dt>
                      <dd>{progress?.model_calls ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Total tokens in / out</dt>
                      <dd>
                        {progress?.input_tokens.toLocaleString() ?? 0} / {progress?.output_tokens.toLocaleString() ??
                          0}
                      </dd>
                    </div>
                    <div>
                      <dt>State version</dt>
                      <dd>v{active.version < 0 ? 0 : active.version}</dd>
                    </div>
                  </dl>
                  <p class="state-caption">
                    Rendered directly from the agent’s internal state. Restored
                    when you reconnect.
                  </p>
                  {#if active.snapshot?.conversation?.value.context_shortened}<p
                      class="state-caption"
                    >
                      Older model context was shortened. The full activity and
                      workspace are preserved.
                    </p>{/if}
                </section>
                <section>
                  <span class="eyebrow">WORKSPACE</span><code
                    class="workspace-path"
                    >{active.workspace ?? "Preparing…"}</code
                  >{#if project?.github_url}<a
                      class="text-button"
                      href={project.github_url}
                      target="_blank"
                      rel="noreferrer"
                      >View repository <ArrowUpRight size={14} /></a
                    >{/if}
                </section>
              </aside>
            </details>
          </div>
        </main>
      {/if}
    </div>
  </div>
{/if}

<dialog
  class="delete-dialog merge-dialog"
  bind:this={mergeDialog}
  aria-labelledby="merge-task-title"
  aria-describedby="merge-task-description"
  oncancel={(event) => {
    if (merging) event.preventDefault();
  }}
  onclose={() => {
    mergeTarget = null;
    mergeError = "";
  }}
>
  <h2 id="merge-task-title">Merge into project?</h2>
  <p class="delete-task-name">{mergeTarget?.title}</p>
  <p id="merge-task-description">
    This writes the accepted task changes into the working tree at:
  </p>
  {#if mergeProject?.path}<code class="workspace-path">{mergeProject.path}</code
    >{/if}
  <p class="delete-note">
    boltzmann will not create a commit or push. Existing project edits are
    preserved when the changes apply cleanly. Overlapping edits stop the merge
    without changing the project.
  </p>
  {#if fileText !== originalText}<p class="delete-note">
      Unsaved text in the task file editor is not part of the accepted changes.
    </p>{/if}
  {#if mergeError}<p class="delete-error" role="alert">{mergeError}</p>{/if}
  <div class="dialog-actions">
    <button disabled={merging} onclick={closeMergeDialog}>Cancel</button>
    <button class="primary" disabled={merging} onclick={mergeTask}>
      <GitMerge size={14} />{merging ? "Merging…" : "Merge changes"}
    </button>
  </div>
</dialog>

<dialog
  class="delete-dialog"
  bind:this={deleteDialog}
  aria-labelledby="delete-task-title"
  aria-describedby="delete-task-description"
  oncancel={(event) => {
    if (deleting) event.preventDefault();
  }}
  onclose={() => {
    deleteTarget = null;
  }}
>
  <h2 id="delete-task-title">Delete this task?</h2>
  <p class="delete-task-name">{deleteTarget?.title}</p>
  <p id="delete-task-description">
    This removes the task from boltzmann. You won’t be able to continue it here.
    Its workspace files are kept unless you choose to remove them below.
  </p>
  {#if fileText !== originalText}<p>
      Unsaved editor changes will be discarded.
    </p>{/if}
  <label class="delete-workspace-option">
    <input type="checkbox" bind:checked={removeWorkspace} disabled={deleting} />
    <span>Also permanently delete this task’s isolated workspace</span>
  </label>
  {#if deleteTarget?.workspace}<code class="workspace-path"
      >{deleteTarget.workspace}</code
    >{/if}
  <p class="delete-note">
    Your source repository stays intact. Temporal history is retained.
  </p>
  {#if deleteError}<p class="delete-error" role="alert">{deleteError}</p>{/if}
  <div class="dialog-actions">
    <button disabled={deleting} onclick={() => deleteDialog?.close()}
      >Cancel</button
    >
    <button class="danger" disabled={deleting} onclick={deleteTask}>
      <Trash2 size={14} />{deleting
        ? "Deleting…"
        : removeWorkspace
          ? "Delete task and workspace"
          : "Delete task"}
    </button>
  </div>
</dialog>
