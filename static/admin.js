const $ = (selector) => document.querySelector(selector);
let documents = [];

function credentials(selector = "#adminKey") {
  const bytes = new TextEncoder().encode($(selector).value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return { "X-Admin-Key": `b64:${btoa(binary)}` };
}

function setResult(type, title, message, findings = [], target = "#uploadResult") {
  const box = $(target);
  box.className = `upload-result show ${type}`;
  box.replaceChildren();
  const heading = document.createElement("strong");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.textContent = message;
  box.append(heading, copy);
  if (findings.length) {
    const list = document.createElement("ul");
    findings.forEach((finding) => {
      const item = document.createElement("li");
      item.textContent = `${finding.type} · ${finding.location} · ${finding.count}건`;
      list.appendChild(item);
    });
    box.appendChild(list);
  }
}

async function loadDocumentOptions() {
  try {
    const categoryResponse = await fetch("/api/categories");
    if (!categoryResponse.ok) throw new Error("분류 목록을 불러오지 못했습니다.");
    const categoryPayload = await categoryResponse.json();
    documents = [];
    let page = 1;
    let total = 0;
    do {
      const response = await fetch(`/api/documents?page=${page}&page_size=100`);
      if (!response.ok) throw new Error("기존 문서 목록을 불러오지 못했습니다.");
      const payload = await response.json();
      documents.push(...payload.items);
      total = payload.total;
      page += 1;
    } while (documents.length < total);
    const select = $("#existingDocument");
    documents.forEach((documentItem) => {
      const option = document.createElement("option");
      option.value = documentItem.id;
      option.textContent = `${documentItem.category} / ${documentItem.title} (v${documentItem.version})`;
      select.appendChild(option);
    });
    const datalist = $("#categoryOptions");
    (categoryPayload.items || []).forEach((category) => {
      const option = document.createElement("option");
      option.value = category.name;
      datalist.appendChild(option);
    });
  } catch {
    setResult("error", "목록 조회 실패", "기존 문서 목록을 불러오지 못했습니다. 서버 상태를 확인하세요.");
  }
}

function bindExistingDocument() {
  $("#existingDocument").addEventListener("change", (event) => {
    const item = documents.find((documentItem) => String(documentItem.id) === event.target.value);
    const isRevision = Boolean(item);
    $("#changeNote").required = isRevision;
    $("#changeNoteLabel").classList.toggle("required", isRevision);
    if (!item) {
      ["#title", "#category", "#department", "#summary", "#changeNote"].forEach((selector) => { $(selector).value = ""; });
      return;
    }
    $("#title").value = item.title;
    $("#category").value = item.category;
    $("#department").value = item.department;
    $("#summary").value = item.summary;
    $("#changeNote").focus();
  });
}

function bindFileZone() {
  const input = $("#manualFile");
  const zone = $("#dropZone");
  input.addEventListener("change", () => {
    const file = input.files[0];
    $("#selectedFile").textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)}MB` : "";
    zone.classList.toggle("selected", Boolean(file));
  });
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("drag"); }));
  zone.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) return;
    const transfer = new DataTransfer();
    transfer.items.add(event.dataTransfer.files[0]);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function upload(event) {
  event.preventDefault();
  const file = $("#manualFile").files[0];
  if (!file) return;
  if (file.size > 60 * 1024 * 1024) {
    setResult("error", "업로드 차단", "파일 크기는 60MB를 초과할 수 없습니다.");
    return;
  }
  const button = $("#submitButton");
  button.disabled = true;
  button.textContent = "파일 구조와 개인정보를 검사하는 중...";
  $("#uploadResult").className = "upload-result";

  const data = new FormData(event.currentTarget);
  if (!data.get("existing_document_id")) data.delete("existing_document_id");
  try {
    const response = await fetch("/api/admin/documents", { method: "POST", headers: credentials(), body: data });
    const payload = await response.json();
    if (!response.ok) {
      const detail = payload.detail;
      if (typeof detail === "object") throw Object.assign(new Error(detail.message || "업로드가 차단되었습니다."), { findings: detail.findings || [] });
      throw new Error(detail || "업로드에 실패했습니다.");
    }
    setResult("success", "등록 완료", `${payload.message} 버전 ${payload.version}, 본문 ${payload.characters.toLocaleString("ko-KR")}자가 등록되었습니다.`);
    $("#manualFile").value = "";
    $("#selectedFile").textContent = "";
  } catch (error) {
    setResult("error", "업로드 차단", error.message, error.findings || []);
  } finally {
    button.disabled = false;
    button.textContent = "개인정보 검사 후 등록";
  }
}

async function loadAudits() {
  const container = $("#auditList");
  container.textContent = "검사 기록을 불러오는 중...";
  try {
    const response = await fetch("/api/admin/audits?limit=20", { headers: credentials() });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "인증에 실패했습니다.");
    container.replaceChildren();
    if (!payload.items.length) { container.textContent = "검사 기록이 없습니다."; return; }
    payload.items.forEach((item) => {
      const row = document.createElement("div");
      row.className = `audit-row ${item.result}`;
      const status = document.createElement("b");
      status.textContent = item.result === "accepted" ? "통과" : item.result === "blocked" ? "차단" : "실패";
      const copy = document.createElement("span");
      copy.textContent = `${item.filename} · ${new Date(item.created_at).toLocaleString("ko-KR")}${item.detected_types ? ` · ${item.detected_types}` : ""}`;
      row.append(status, copy);
      container.appendChild(row);
    });
  } catch (error) {
    container.textContent = error.message;
  }
}

let sourceInfo = null;

function sourceViewUrl(start, end) {
  const version = encodeURIComponent(sourceInfo?.version || "");
  return `/source-manual.pdf?start=${start}&end=${end}&v=${version}#page=1&view=FitH`;
}

function renderSourceStatus() {
  const status = $("#sourceStatus");
  status.classList.toggle("missing", !sourceInfo?.available);
  if (!sourceInfo?.available) {
    status.textContent = "아직 원본 PDF가 없습니다. 새 원본 PDF를 올리고 쪽수를 입력하세요.";
    return;
  }
  const updated = new Date(sourceInfo.updated_at).toLocaleString("ko-KR");
  const bytes = sourceInfo.size_bytes;
  const size = bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)}MB` : `${Math.max(1, Math.round(bytes / 1024))}KB`;
  status.textContent = `현재 원본은 전체 ${sourceInfo.page_count}쪽, ${size}이며 ${updated}에 반영되었습니다.`;
}

function updateSourceRow(row) {
  const start = Number(row.querySelector("[data-field=start]").value);
  const end = Number(row.querySelector("[data-field=end]").value);
  const link = row.querySelector("a");
  const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 1 && end >= start;
  const fits = valid && sourceInfo?.available && end <= sourceInfo.page_count;
  row.querySelectorAll("input").forEach((input) => input.classList.toggle("invalid", !valid));
  if (fits) {
    link.href = sourceViewUrl(start, end);
    link.removeAttribute("aria-disabled");
  } else {
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
  }
}

function renderSourceRows() {
  const container = $("#sourcePageRows");
  container.replaceChildren();
  (sourceInfo?.pages || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "source-page-row";
    row.dataset.category = item.category;
    const name = document.createElement("strong");
    name.textContent = item.category;
    const inputs = ["start", "end"].map((field) => {
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.step = "1";
      input.required = true;
      input.value = item[field];
      input.dataset.field = field;
      input.setAttribute("aria-label", `${item.category} ${field === "start" ? "시작 쪽" : "끝 쪽"}`);
      input.addEventListener("input", () => updateSourceRow(row));
      return input;
    });
    const view = document.createElement("a");
    view.textContent = "보기";
    view.target = "_blank";
    view.rel = "noopener";
    view.title = "현재 원본에서 이 쪽수를 새 창으로 확인";
    row.append(name, ...inputs, view);
    container.appendChild(row);
    updateSourceRow(row);
  });
}

async function loadSourceManual() {
  try {
    const response = await fetch("/api/source-manual");
    if (response.status === 404) throw new Error("서버가 이전 버전으로 실행 중입니다. 열려 있는 검은 서버 창을 모두 닫고 start-server.bat을 다시 실행한 뒤 이 화면을 Ctrl + F5로 새로고침하세요.");
    if (!response.ok) throw new Error("원본 정보를 불러오지 못했습니다. 서버 상태를 확인하세요.");
    sourceInfo = await response.json();
    renderSourceStatus();
    renderSourceRows();
  } catch (error) {
    $("#sourceStatus").classList.add("missing");
    $("#sourceStatus").textContent = error.message || "원본 정보를 불러오지 못했습니다. 서버 상태를 확인하세요.";
  }
}

function collectSourcePages() {
  return [...document.querySelectorAll(".source-page-row")].map((row) => ({
    category: row.dataset.category,
    start: Number(row.querySelector("[data-field=start]").value),
    end: Number(row.querySelector("[data-field=end]").value),
  }));
}

async function saveSourceManual(event) {
  event.preventDefault();
  const file = $("#sourceFile").files[0];
  if (file && file.size > 60 * 1024 * 1024) {
    setResult("error", "저장하지 못했습니다", "파일 크기는 60MB를 초과할 수 없습니다.", [], "#sourceResult");
    return;
  }
  if (file && !window.confirm("원본 매뉴얼을 새 PDF로 교체합니다. 이전 원본은 자동 보관됩니다. 계속할까요?")) return;
  const button = $("#sourceSubmit");
  button.disabled = true;
  button.textContent = file ? "새 원본을 검사하고 저장하는 중..." : "쪽수를 저장하는 중...";
  $("#sourceResult").className = "upload-result";
  const data = new FormData();
  data.append("admin_name", $("#sourceAdminName").value);
  data.append("pages", JSON.stringify(collectSourcePages()));
  if (file) data.append("file", file);
  try {
    const response = await fetch("/api/admin/source-manual", { method: "POST", headers: credentials("#sourceAdminKey"), body: data });
    if (response.status === 404 || response.status === 405) throw new Error("서버가 이전 버전으로 실행 중입니다. 열려 있는 검은 서버 창을 모두 닫고 start-server.bat을 다시 실행한 뒤 이 화면을 Ctrl + F5로 새로고침하세요.");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail;
      throw new Error(typeof detail === "string" ? detail : detail?.[0]?.msg || "저장하지 못했습니다.");
    }
    sourceInfo = payload;
    renderSourceStatus();
    renderSourceRows();
    $("#sourceFile").value = "";
    setResult("success", "저장했습니다", `${payload.message} 열람 화면을 새로고침하면 바로 반영됩니다.`, [], "#sourceResult");
  } catch (error) {
    setResult("error", "저장하지 못했습니다", error.message, [], "#sourceResult");
  } finally {
    button.disabled = false;
    button.textContent = "원본과 쪽수 저장";
  }
}

function syncCredentialFields() {
  [["#adminKey", "#sourceAdminKey"], ["#adminName", "#sourceAdminName"]].forEach(([first, second]) => {
    $(first).addEventListener("input", () => { $(second).value = $(first).value; });
    $(second).addEventListener("input", () => { $(first).value = $(second).value; });
  });
}

loadDocumentOptions();
loadSourceManual();
syncCredentialFields();
$("#sourceForm").addEventListener("submit", saveSourceManual);
bindExistingDocument();
bindFileZone();
$("#uploadForm").addEventListener("submit", upload);
$("#loadAuditsButton").addEventListener("click", loadAudits);
