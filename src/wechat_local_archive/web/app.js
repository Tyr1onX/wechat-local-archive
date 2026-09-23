const $ = (id) => document.getElementById(id);

let selectedChat = null;
let taskTimer = null;
let searchTimer = null;
let currentStatus = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
}

function showError(error) {
  $("errorBox").textContent = error instanceof Error ? error.message : String(error);
  $("errorBox").hidden = false;
}

function clearError() {
  $("errorBox").hidden = true;
  $("errorBox").textContent = "";
}

function setBadge(text, mode = "") {
  const badge = $("statusBadge");
  badge.textContent = text;
  badge.className = "badge" + (mode ? " " + mode : "");
}

async function loadStatus() {
  clearError();
  try {
    currentStatus = await api("/api/status");
    $("outputRoot").textContent = currentStatus.output_root;
    $("presetSelect").value = currentStatus.asr_preset || "balanced";
    if (currentStatus.ready) {
      setBadge("已就绪", "ready");
      $("setup").hidden = true;
      $("workspace").hidden = false;
      await loadChats($("searchInput").value);
    } else {
      setBadge("需要初始化");
      $("workspace").hidden = true;
      $("setup").hidden = false;
      await loadAccounts();
    }
  } catch (error) {
    setBadge("检查失败", "error-state");
    showError(error);
  }
}

async function loadAccounts() {
  const { accounts } = await api("/api/accounts");
  const select = $("accountSelect");
  select.replaceChildren();
  for (const item of accounts) {
    const option = document.createElement("option");
    option.value = item.account;
    option.textContent = item.account;
    select.appendChild(option);
  }
  select.hidden = accounts.length <= 1;
}

async function loadChats(search = "") {
  const { chats } = await api("/api/chats?search=" + encodeURIComponent(search));
  const list = $("chatList");
  list.replaceChildren();

  for (const chat of chats) {
    const button = document.createElement("button");
    button.className = "chat" + (selectedChat?.username === chat.username ? " selected" : "");
    button.type = "button";
    const name = document.createElement("span");
    name.className = "chat-name";
    name.textContent = chat.name || chat.username;
    const count = document.createElement("span");
    count.className = "chat-count";
    count.textContent = chat.message_count ?? 0;
    button.append(name, count);
    button.addEventListener("click", () => selectChat(chat));
    list.appendChild(button);
  }

  if (selectedChat && !chats.some((chat) => chat.username === selectedChat.username)) {
    selectChat(null);
  } else if (!selectedChat && search && chats.length === 1) {
    selectChat(chats[0]);
  }
}

function selectChat(chat) {
  selectedChat = chat;
  $("emptySelection").hidden = Boolean(chat);
  $("chatPanel").hidden = !chat;
  $("resultPanel").hidden = true;
  if (chat) {
    $("chatName").textContent = chat.name || chat.username;
    $("chatMeta").textContent = `${chat.message_count ?? 0} 条消息`;
  }
  for (const button of document.querySelectorAll(".chat")) {
    button.classList.toggle("selected", button.querySelector(".chat-name")?.textContent === (chat?.name || chat?.username));
  }
  updateControls();
}

function updateControls(busy = false) {
  const hasChat = Boolean(selectedChat);
  $("exportButton").disabled = busy || !hasChat;
  $("verifyButton").disabled = busy || !hasChat;
  $("openRootButton").disabled = busy;
  $("searchInput").disabled = busy;
  for (const input of document.querySelectorAll(".options input, .options select")) input.disabled = busy;
  if (!$("voiceOption").checked) {
    $("transcribeOption").checked = false;
    $("transcribeOption").disabled = true;
    $("presetSelect").disabled = true;
  } else if (!busy) {
    $("transcribeOption").disabled = false;
    $("presetSelect").disabled = !$("transcribeOption").checked;
  }
}

async function initialize() {
  clearError();
  try {
    const account = $("accountSelect").hidden ? null : $("accountSelect").value;
    await runTask("/api/bootstrap", account ? { account } : {});
  } catch (error) {
    showError(error);
  }
}

function exportBody() {
  return {
    chat: selectedChat.username,
    start: $("startDate").value,
    end: $("endDate").value,
    images: $("imagesOption").checked,
    voice: $("voiceOption").checked,
    other_media: $("otherOption").checked,
    transcribe: $("transcribeOption").checked,
    asr_preset: $("presetSelect").value,
  };
}

async function startExport() {
  if (!selectedChat) return;
  clearError();
  try {
    await runTask("/api/export", exportBody());
  } catch (error) {
    showError(error);
  }
}

async function startVerify() {
  if (!selectedChat) return;
  clearError();
  try {
    await runTask("/api/verify", { chat: selectedChat.username });
  } catch (error) {
    showError(error);
  }
}

async function runTask(endpoint, body) {
  if (taskTimer) clearTimeout(taskTimer);
  $("resultPanel").hidden = true;
  await api(endpoint, { method: "POST", body: JSON.stringify(body) });
  $("taskPanel").hidden = false;
  updateControls(true);
  setBadge("处理中", "busy");
  await pollTask();
}

async function pollTask() {
  try {
    const task = await api("/api/task");
    renderTask(task);
    if (task.state === "running") {
      taskTimer = setTimeout(pollTask, 700);
      return;
    }

    taskTimer = null;
    $("taskPanel").hidden = true;
    updateControls(false);

    if (task.state === "done") {
      await handleTaskDone(task);
    } else if (task.state === "cancelled") {
      setBadge(currentStatus?.ready ? "已就绪" : "需要初始化", currentStatus?.ready ? "ready" : "");
      showResult("已取消", "原有归档未被删除。", false);
    } else if (task.state === "error") {
      setBadge("操作失败", "error-state");
      showError(task.error || "操作失败");
    }
  } catch (error) {
    taskTimer = null;
    updateControls(false);
    setBadge("操作失败", "error-state");
    showError(error);
  }
}

function renderTask(task) {
  $("taskLabel").textContent = task.label || "正在处理…";
  $("cancelButton").disabled = Boolean(task.cancelling);
  if (task.total > 0) {
    const percent = Math.min(100, Math.round(task.current * 100 / task.total));
    $("taskProgress").value = percent;
    $("taskCount").textContent = `${task.current} / ${task.total}`;
  } else {
    $("taskProgress").removeAttribute("value");
    $("taskCount").textContent = "";
  }
}

async function handleTaskDone(task) {
  if (task.task === "bootstrap") {
    showResult("初始化完成", "", false);
    await loadStatus();
    return;
  }

  setBadge("已就绪", "ready");
  if (task.task === "export") {
    const result = task.result || {};
    const warning = result.warnings?.length ? ` · ${result.warnings.length} 条提醒` : "";
    showResult(
      "归档完成",
      `${result.message_count ?? 0} 条消息 · ${result.attachment_count ?? 0} 个附件 · ${result.transcript_count ?? 0} 条语音已转写${warning}`,
      true,
    );
  } else if (task.task === "verify") {
    const result = task.result || {};
    const ok = !result.errors?.length;
    showResult(
      ok ? "检查通过" : "检查未通过",
      ok
        ? `${result.message_count ?? 0} 条消息 · ${result.attachment_count ?? 0} 个附件 · 语音转写 ${result.voice_transcript_count ?? 0}/${result.voice_count ?? 0}`
        : (result.errors?.[0] || "归档存在问题"),
      true,
    );
  }
}

function showResult(title, summary, actions) {
  $("resultPanel").hidden = false;
  $("resultTitle").textContent = title;
  $("resultSummary").textContent = summary;
  $("resultActions").hidden = !actions;
}

async function cancelTask() {
  clearError();
  try {
    await api("/api/task/cancel", { method: "POST", body: "{}" });
    $("cancelButton").disabled = true;
    $("taskLabel").textContent = "正在取消，等待当前操作完成…";
  } catch (error) {
    showError(error);
  }
}

async function openLocal(path, body = {}) {
  clearError();
  try {
    await api(path, { method: "POST", body: JSON.stringify(body) });
  } catch (error) {
    showError(error);
  }
}

$("initializeButton").addEventListener("click", initialize);
$("exportButton").addEventListener("click", startExport);
$("verifyButton").addEventListener("click", startVerify);
$("cancelButton").addEventListener("click", cancelTask);
$("openRootButton").addEventListener("click", () => openLocal("/api/open-folder"));
$("openFolderButton").addEventListener("click", () => selectedChat && openLocal("/api/open-folder", { chat: selectedChat.username }));
$("openChatButton").addEventListener("click", () => selectedChat && openLocal("/api/open-chat", { chat: selectedChat.username }));
$("checkAgainButton").addEventListener("click", startVerify);

$("voiceOption").addEventListener("change", () => updateControls(false));
$("transcribeOption").addEventListener("change", () => updateControls(false));
$("searchInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadChats($("searchInput").value).catch(showError), 180);
});

loadStatus().then(async () => {
  try {
    const task = await api("/api/task");
    if (task.state === "running") {
      $("taskPanel").hidden = false;
      updateControls(true);
      setBadge("处理中", "busy");
      await pollTask();
    }
  } catch (error) {
    showError(error);
  }
});
