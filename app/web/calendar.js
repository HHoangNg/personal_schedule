const form = document.querySelector("#calendar-form");
const taskList = document.querySelector("#task-list");
const addTaskButton = document.querySelector("#add-task");
const scanGmailButton = document.querySelector("#scan-gmail");
const saveButton = document.querySelector("#save-button");
const dayTabs = document.querySelector("#day-tabs");
const dayView = document.querySelector("#day-view");
const status = document.querySelector("#calendar-status");
const feedbackForm = document.querySelector("#feedback-form");
const recalculateButton = document.querySelector("#recalculate-button");
const assistantForm = document.querySelector("#assistant-form");
const assistantButton = document.querySelector("#assistant-button");
const assistantMessage = document.querySelector("#assistant-message");
const assistantResult = document.querySelector("#assistant-result");
let currentPlanExists = false;
let currentPlanId = "";
let currentPlanData = null;

const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;"
}[character]));

function addTaskRow(values = {}) {
  const row = document.createElement("article");
  row.className = "task-row";
  row.innerHTML = `<div class="task-row-top"><span class="task-number">Công việc</span><button type="button" class="remove-task">Xóa</button></div>
    <label>Tên công việc<input class="task-title" value="${escapeHtml(values.title || "")}" placeholder="Ví dụ: Học tiếng Anh" /></label>
    <div class="task-fields"><label>Thời lượng (phút)<input class="task-minutes" type="number" min="5" max="480" value="${values.estimated_minutes || 30}" /></label>
    <label>Kiểu deadline<select class="deadline-mode"><option value="none">Không đặt</option><option value="daily">Hằng ngày</option><option value="specific">Thời gian cụ thể</option></select></label>
    <label class="specific-deadline hidden">Ngày và giờ deadline<input class="deadline-at" type="datetime-local" /></label></div>`;
  row.querySelector(".deadline-mode").value = values.deadline_mode || "none";
  row.querySelector(".deadline-at").value = values.deadline_at ? String(values.deadline_at).slice(0, 16) : "";
  const mode = row.querySelector(".deadline-mode");
  const specific = row.querySelector(".specific-deadline");
  mode.addEventListener("change", () => specific.classList.toggle("hidden", mode.value !== "specific"));
  specific.classList.toggle("hidden", mode.value !== "specific");
  row.querySelector(".remove-task").addEventListener("click", () => {
    if (taskList.children.length > 1) row.remove();
  });
  taskList.appendChild(row);
}

function collectTasks() {
  return [...document.querySelectorAll(".task-row")].map((row) => {
    const title = row.querySelector(".task-title").value.trim();
    const deadlineMode = row.querySelector(".deadline-mode").value;
    const deadlineAt = row.querySelector(".deadline-at").value || null;
    if (!title && deadlineMode === "none" && !deadlineAt) return null;
    return {
      title,
      estimated_minutes: Number(row.querySelector(".task-minutes").value),
      deadline_mode: deadlineMode,
      deadline_at: deadlineAt,
    };
  }).filter(Boolean);
}

function periodLabel(period) {
  return { morning: "Sáng", afternoon: "Chiều", evening: "Tối", night: "Đêm" }[period] || period;
}

function renderDay(data, selectedDate) {
  const blocks = data.schedule.filter((item) => item.date === selectedDate);
  const groups = ["morning", "afternoon", "evening", "night"];
  dayView.innerHTML = `<div class="selected-day"><h3>${new Date(`${selectedDate}T12:00:00`).toLocaleDateString("vi-VN", { weekday: "long", day: "numeric", month: "long" })}</h3></div>${groups.map((period) => {
    const periodBlocks = blocks.filter((item) => item.period === period);
    if (!periodBlocks.length) return "";
    return `<section class="period"><h4>${periodLabel(period)}</h4><div class="block-list">${periodBlocks.map((block) => `<div class="time-block ${block.block_type} status-${block.status || "planned"}"><time>${block.start_time}<br /><span>${block.end_time}</span></time><div class="block-content"><strong>${escapeHtml(block.task_title)}</strong><small>${block.minutes} phút${block.block_type === "task" ? ` · ${statusLabel(block.status)}` : ""}</small></div>${block.block_type === "task" ? `<div class="block-actions"><button type="button" data-status="completed" data-block-id="${block.block_id}">Xong</button><button type="button" data-status="partial" data-block-id="${block.block_id}">Một phần</button><button type="button" data-status="skipped" data-block-id="${block.block_id}">Bỏ qua</button><button type="button" data-status="rescheduled" data-block-id="${block.block_id}">Dời</button></div>` : ""}</div>`).join("")}</div></section>`;
  }).join("")}`;
  dayView.querySelectorAll(".block-actions button").forEach((button) => button.addEventListener("click", () => updateBlockStatus(data, button.dataset.blockId, button.dataset.status)));
}

function statusLabel(status) {
  return { planned: "Chưa thực hiện", completed: "Đã hoàn thành", partial: "Hoàn thành một phần", skipped: "Đã bỏ qua", rescheduled: "Đã dời lịch" }[status || "planned"] || status;
}

function localDateString() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function renderCalendar(data) {
  currentPlanData = data;
  currentPlanId = data.plan_id || "";
  const displayName = data.plan_input?.display_name || document.querySelector("#display-name").value.trim() || data.plan_input?.user_id || "";
  const userName = document.querySelector("#calendar-user-name");
  if (userName) userName.textContent = displayName ? `Của ${displayName}` : "";
  if (data.plan_input?.display_name && !document.querySelector("#display-name").value.trim()) {
    document.querySelector("#display-name").value = data.plan_input.display_name;
  }
  const dates = [...new Set(data.schedule.map((item) => item.date))].sort();
  dayTabs.innerHTML = dates.map((date, index) => `<button type="button" class="day-tab ${index === 0 ? "active" : ""}" data-date="${date}"><strong>${new Date(`${date}T12:00:00`).toLocaleDateString("vi-VN", { weekday: "short" })}</strong><span>${new Date(`${date}T12:00:00`).getDate()}/${new Date(`${date}T12:00:00`).getMonth() + 1}</span></button>`).join("");
  dayTabs.querySelectorAll(".day-tab").forEach((tab) => tab.addEventListener("click", () => {
    dayTabs.querySelectorAll(".day-tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    renderDay(data, tab.dataset.date);
  }));
  if (dates.length) renderDay(data, dates[0]);
  status.textContent = "Đã lưu";
  currentPlanExists = true;
  updateSaveButtonMode();
  loadAnalytics();
}

async function updateBlockStatus(data, blockId, blockStatus) {
  if (!currentPlanId) return;
  let actualMinutes = null;
  let reason = "";
  if (blockStatus === "partial") actualMinutes = Number(window.prompt("Bạn đã làm được bao nhiêu phút?", "15") || 0);
  if (blockStatus === "skipped" || blockStatus === "rescheduled") reason = window.prompt("Lý do hoặc ghi chú:", "") || "";
  try {
    const response = await fetch(`/v1/schedule/${encodeURIComponent(currentPlanId)}/blocks/${encodeURIComponent(blockId)}/status?user_id=${encodeURIComponent(document.querySelector("#user-id").value.trim())}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: blockStatus, actual_minutes: actualMinutes, reason }),
    });
    if (!response.ok) throw new Error("Không thể lưu trạng thái công việc");
    renderCalendar(await response.json());
    status.textContent = "Đã ghi nhận thực tế";
  } catch (error) { status.textContent = error.message; }
}

async function loadAnalytics() {
  const userId = document.querySelector("#user-id").value.trim();
  if (!userId) return;
  try {
    const response = await fetch(`/v1/analytics/productivity?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) return;
    const data = await response.json();
    document.querySelector("#analytics-summary").textContent = `Hoàn thành ${Math.round((data.profile.completion_rate || 0) * 100)}% · ${data.profile.effective_period}`;
  } catch (_) { /* Analytics không được làm hỏng lịch */ }
}

function resetTaskInputs() {
  taskList.innerHTML = "";
  addTaskRow();
  document.querySelector("#planning-notes").value = "";
}

async function loadCalendar() {
  const userId = document.querySelector("#user-id").value.trim();
  if (!userId) return;
  try {
    const listResponse = await fetch(`/v1/plans?user_id=${encodeURIComponent(userId)}`);
    const plans = await listResponse.json();
    if (!plans.length) {
      currentPlanExists = false;
      updateSaveButtonMode();
      status.textContent = "Chưa có lịch cho mã người dùng này";
      dayTabs.innerHTML = "";
      dayView.innerHTML = `<div class="empty-calendar">Tạo lịch để xem các khối công việc, thời gian cá nhân và nghỉ ngơi.</div>`;
      const userName = document.querySelector("#calendar-user-name");
      if (userName) userName.textContent = "";
      return;
    }
    const response = await fetch(`/v1/plans/${encodeURIComponent(plans[0].plan_id)}?user_id=${encodeURIComponent(userId)}`);
    if (response.ok) renderCalendar(await response.json());
  } catch (_) { status.textContent = "Chưa tải được lịch đã lưu"; }
}

function updateSaveButtonMode() {
  saveButton.textContent = currentPlanExists ? "Chỉnh sửa lịch 14 ngày" : "Tạo lịch 14 ngày";
}

addTaskButton.addEventListener("click", () => addTaskRow());
addTaskRow();
loadCalendar();
document.querySelector("#user-id").addEventListener("change", loadCalendar);
document.querySelector("#user-id").addEventListener("blur", loadCalendar);

assistantForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = document.querySelector("#user-id").value.trim();
  const message = assistantMessage.value.trim();
  if (!userId || message.length < 3) {
    assistantResult.textContent = "Hãy nhập mã người dùng và yêu cầu muốn AI xử lý.";
    return;
  }
  assistantButton.disabled = true;
  assistantButton.textContent = "AI đang suy luận…";
  assistantResult.textContent = "Đang lấy lịch sử cá nhân và phân tích yêu cầu…";
  try {
    const response = await fetch("/v1/assistant/message", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, display_name: document.querySelector("#display-name").value.trim(), message }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Không thể xử lý yêu cầu với trợ lý AI");
    if (data.plan) {
      renderCalendar(data.plan);
      resetTaskInputs();
    }
    const intent = data.intent || {};
    assistantResult.textContent = `AI đã nhận dạng: ${intent.action || "chưa rõ"} · độ tin cậy ${Math.round((intent.confidence || 0) * 100)}%${intent.requires_confirmation ? " · thay đổi lớn nên được kiểm tra lại" : ""}`;
    assistantMessage.value = "";
  } catch (error) {
    assistantResult.textContent = error.message;
  } finally {
    assistantButton.disabled = false;
    assistantButton.textContent = "AI hiểu và cập nhật lịch";
  }
});

recalculateButton.addEventListener("click", async () => {
  const userId = document.querySelector("#user-id").value.trim();
  if (!userId || !currentPlanId) { status.textContent = "Hãy tạo lịch trước khi đánh giá lại"; return; }
  recalculateButton.disabled = true;
  recalculateButton.textContent = "AI đang phân tích…";
  try {
    const response = await fetch("/v1/workflow/recalculate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, confirm_advice: true }),
    });
    if (!response.ok) throw new Error("Không thể đánh giá lại lịch");
    renderCalendar(await response.json());
    status.textContent = "Đã áp dụng phân tích từ lịch sử thực tế";
  } catch (error) { status.textContent = error.message; }
  finally { recalculateButton.disabled = false; recalculateButton.textContent = "AI đánh giá lại"; }
});

feedbackForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = document.querySelector("#user-id").value.trim();
  if (!userId) { status.textContent = "Hãy nhập mã người dùng trước"; return; }
  try {
    const response = await fetch("/v1/feedback/daily", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: userId,
        feedback_date: localDateString(),
        energy: Number(document.querySelector("#feedback-energy").value),
        focus: Number(document.querySelector("#feedback-focus").value),
        effective_period: document.querySelector("#feedback-period").value,
        schedule_feeling: document.querySelector("#feedback-feeling").value,
        procrastinated_tasks: document.querySelector("#feedback-procrastinated").value.split(",").map((item) => item.trim()).filter(Boolean),
        note: document.querySelector("#feedback-note").value.trim(),
      }),
    });
    if (!response.ok) throw new Error("Không thể lưu feedback");
    status.textContent = "Đã lưu feedback; AI sẽ dùng dữ liệu này ở lần cập nhật lịch tiếp theo";
    loadAnalytics();
  } catch (error) { status.textContent = error.message; }
});

scanGmailButton.addEventListener("click", async () => {
  const userId = document.querySelector("#user-id").value.trim();
  if (!userId) { status.textContent = "Hãy nhập mã người dùng trước khi quét Gmail"; return; }
  scanGmailButton.disabled = true;
  scanGmailButton.textContent = "Đang quét Gmail…";
  status.textContent = "Đang đọc Gmail 3 ngày gần nhất";
  try {
    const response = await fetch("/v1/integrations/gmail/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: userId,
        display_name: document.querySelector("#display-name").value.trim(),
        days: Number(document.querySelector("#gmail-days").value || 3),
        max_results: 50,
      }),
    });
    if (!response.ok) throw new Error("Không thể quét Gmail. Hãy kiểm tra OAuth Gmail và thử lại.");
    renderCalendar(await response.json());
    resetTaskInputs();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    scanGmailButton.disabled = false;
    scanGmailButton.textContent = "Quét Gmail và cập nhật lịch";
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const tasks = collectTasks();
  const planningNotes = document.querySelector("#planning-notes").value.trim();
  if (!tasks.length && planningNotes.length < 3) {
    status.textContent = "Hãy chọn deadline, thêm công việc hoặc nhập thông tin cập nhật lịch";
    return;
  }
  saveButton.disabled = true;
  saveButton.textContent = "AI đang cập nhật lịch…";
  try {
    const response = await fetch("/v1/workflow/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: document.querySelector("#user-id").value.trim(),
        display_name: document.querySelector("#display-name").value.trim(),
        raw_input: tasks.map((task) => task.title).join(", ") || planningNotes,
        task_inputs: tasks,
        planning_notes: planningNotes,
        horizon_days: 14,
      }),
    });
    if (!response.ok) throw new Error("Không thể cập nhật lịch. Hãy kiểm tra cấu hình LLM và dữ liệu nhập.");
    renderCalendar(await response.json());
    resetTaskInputs();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    saveButton.disabled = false;
    updateSaveButtonMode();
  }
});
