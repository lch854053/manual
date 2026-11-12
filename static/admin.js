const $ = (selector) => document.querySelector(selector);
let documents = [];

function credentials() {
  const bytes = new TextEncoder().encode($("#adminKey").value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return { "X-Admin-Key": `b64:${btoa(binary)}` };
}

function setResult(type, title, message, findings = []) {
  const box = $("#uploadResult");
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
  if (file.size > 30 * 1024 * 1024) {
    setResult("error", "업로드 차단", "파일 크기는 30MB를 초과할 수 없습니다.");
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

loadDocumentOptions();
bindExistingDocument();
bindFileZone();
$("#uploadForm").addEventListener("submit", upload);
$("#loadAuditsButton").addEventListener("click", loadAudits);
