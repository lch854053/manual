const state = {
  categories: [],
  permissions: [],
  handbooks: [],
  relatedSites: [],
  documents: [],
  current: null,
  versions: [],
  activeCategory: "",
  activeResource: "manual",
  currentPermission: null,
  currentHandbook: null,
  requestId: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function adminAuthHeaders(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return { "X-Admin-Key": `b64:${btoa(binary)}` };
}

const sourcePages = {
  "출생·사망": [3, 10],
  "가족관계시스템": [11, 14],
  "주민등록시스템": [15, 45],
  "부동산종합공부": [46, 49],
  "토지이용계획": [50, 50],
  "건축행정시스템": [51, 53],
  "차세대지방세": [54, 59],
  "부동산공시가격": [60, 60],
  "부동산거래관리": [61, 62],
  "외국인정보공동이용": [63, 66],
  "민원처리운영창구": [67, 70],
  "자동차등록원부": [71, 71],
};

const resourceLabels = {
  permission: "권한신청서식",
  handbook: "편람",
  links: "관련사이트",
  legalForms: "국가법령정보센터 별표서식",
};

function formatDate(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
}

function formatBytes(value) {
  if (!value) return "시스템 예시자료";
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))}KB`;
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2300);
}

function renderBreadcrumb(label, categoryHandler = null) {
  const breadcrumb = $("#breadcrumb");
  breadcrumb.replaceChildren();
  const home = document.createElement("a");
  home.href = "/";
  home.textContent = "홈";
  breadcrumb.appendChild(home);
  if (categoryHandler) {
    const category = document.createElement("button");
    category.type = "button";
    category.textContent = categoryHandler.label;
    category.addEventListener("click", categoryHandler.action);
    breadcrumb.appendChild(category);
  }
  const current = document.createElement("span");
  current.textContent = label;
  breadcrumb.appendChild(current);
}

async function api(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error("자료를 불러오지 못했습니다.");
  return response.json();
}

async function fetchAllDocuments(parameters = {}) {
  const items = [];
  let page = 1;
  let total = 0;
  do {
    const params = new URLSearchParams({ ...parameters, page: String(page), page_size: "100" });
    const payload = await api(`/api/documents?${params}`);
    items.push(...payload.items);
    total = payload.total;
    page += 1;
  } while (items.length < total);
  return { items, total };
}

function reportError(error) {
  showToast(error?.message || "요청을 처리하지 못했습니다.");
}

function renderCategories() {
  const list = $("#categoryList");
  list.replaceChildren();
  const permissionMode = state.activeResource === "permission";
  const handbookMode = state.activeResource === "handbook";
  const items = permissionMode
    ? state.permissions.map((item) => ({ name: item.title, key: item.slug }))
    : handbookMode
      ? state.handbooks.map((item) => ({ name: item.title, key: item.slug }))
      : state.categories.map((item) => ({ name: item.name, key: item.name, count: item.count }));
  const total = permissionMode || handbookMode ? items.length : state.categories.reduce((sum, item) => sum + item.count, 0);
  $("#categoryEyebrow").textContent = permissionMode ? "권한 신청" : handbookMode ? "업무 편람" : "업무 매뉴얼";
  $("#allDocumentsLabel").textContent = permissionMode ? "전체 권한" : handbookMode ? "전체 편람" : "전체 업무";
  $("#totalCategoryCount").textContent = total;
  items.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.category = item.key;
    button.classList.toggle("active", state.activeCategory === item.key);
    const number = document.createElement("i");
    number.textContent = String(index + 1).padStart(2, "0");
    const label = document.createElement("span");
    label.textContent = item.name;
    button.append(number, label);
    button.addEventListener("click", () => {
      const action = permissionMode ? openPermission(item.key) : handbookMode ? openHandbook(item.key) : openCategory(item.name);
      action.catch(reportError);
    });
    list.appendChild(button);
  });
}

function setActiveResource(resource) {
  state.activeResource = resource;
  $$('[data-resource]').forEach((button) => {
    button.classList.toggle("active", button.dataset.resource === resource);
  });
}

function renderPermissionList(items) {
  const list = $("#resultList");
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-results";
    const title = document.createElement("strong");
    title.textContent = "검색 결과가 없습니다.";
    const description = document.createElement("span");
    description.textContent = "시스템명이나 담당부서로 다시 검색해 보세요.";
    empty.append(title, description);
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const article = document.createElement("article");
    const category = document.createElement("span");
    category.className = "result-category";
    category.textContent = "권한신청서식";
    const title = document.createElement("button");
    title.type = "button";
    title.className = "result-item-title";
    title.textContent = item.title;
    title.addEventListener("click", () => openPermission(item.slug).catch(reportError));
    const summary = document.createElement("p");
    summary.textContent = `${item.department} 담당`;
    article.append(category, title, summary);
    list.appendChild(article);
  });
}

function renderPermissionAttachments(items) {
  const list = $("#permissionAttachmentList");
  list.replaceChildren();
  $("#permissionAttachmentCount").textContent = items.length;
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "permission-attachment-empty";
    empty.textContent = "첨부된 한글서식이 없습니다.";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "permission-attachment-row";
    const type = document.createElement("b");
    type.textContent = item.file_type.toUpperCase();
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.original_filename;
    const meta = document.createElement("span");
    meta.textContent = `${formatBytes(item.size_bytes)} · ${formatDate(item.created_at)} · ${item.created_by}`;
    copy.append(name, meta);
    const download = document.createElement("a");
    download.href = `/api/permission-attachments/${item.id}/download`;
    download.textContent = "내려받기";
    row.append(type, copy, download);
    list.appendChild(row);
  });
}

function renderRelatedSites(container) {
  state.relatedSites.forEach((site, index) => {
    const article = document.createElement("article");
    article.className = "related-site-card";
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    setRelatedSiteName(name, site.name);
    const host = document.createElement("small");
    host.textContent = new URL(site.url).host;
    copy.append(name, host);
    const link = document.createElement("a");
    link.href = site.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "바로가기";
    article.append(number, copy, link);
    container.appendChild(article);
  });
}

function setRelatedSiteName(element, name) {
  const match = name.match(/^(모바일 신분증 진위확인·사본저장)\s*(시스템)$/);
  if (!match) {
    element.textContent = name;
    return;
  }
  element.append(match[1], document.createElement("br"), match[2]);
}

function renderRelatedSitePanel() {
  const list = $("#relatedSiteList");
  list.replaceChildren();
  state.relatedSites.forEach((site, index) => {
    const link = document.createElement("a");
    link.href = site.url;
    link.target = "_blank";
    link.rel = "noopener";
    const number = document.createElement("span");
    number.textContent = String(index + 1).padStart(2, "0");
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    setRelatedSiteName(name, site.name);
    const host = document.createElement("small");
    host.textContent = new URL(site.url).host;
    copy.append(name, host);
    link.append(number, copy);
    list.appendChild(link);
  });
}

async function loadRelatedSites() {
  const payload = await api("/api/related-sites");
  state.relatedSites = payload.items;
  renderRelatedSitePanel();
}

async function loadPermissionAttachments(slug) {
  const payload = await api(`/api/permissions/${encodeURIComponent(slug)}/attachments`);
  renderPermissionAttachments(payload.items);
}

function showPermissionList(query = "", updateUrl = true) {
  ++state.requestId;
  setActiveResource("permission");
  setActiveCategory("");
  renderCategories();
  const normalized = query.trim().toLocaleLowerCase("ko-KR");
  const items = normalized
    ? state.permissions.filter((item) => `${item.title} ${item.department}`.toLocaleLowerCase("ko-KR").includes(normalized))
    : state.permissions;
  $("#manualView").hidden = true;
  $("#resultView").hidden = false;
  $("#resultTotal").textContent = items.length.toLocaleString("ko-KR");
  $("#resultTitle").textContent = query ? `“${query}” 권한 검색 결과` : "전체 권한신청서식";
  renderBreadcrumb(query ? "권한 통합검색" : "권한신청서식");
  renderPermissionList(items);
  if (updateUrl) history.pushState({}, "", query ? `/?resource=permission&q=${encodeURIComponent(query)}` : "/?resource=permission");
  closeCategoryPanel();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openPermission(slug, updateUrl = true) {
  const requestId = ++state.requestId;
  const [item, attachmentPayload] = await Promise.all([
    api(`/api/permissions/${encodeURIComponent(slug)}`),
    api(`/api/permissions/${encodeURIComponent(slug)}/attachments`),
  ]);
  if (requestId !== state.requestId) return;
  state.currentPermission = item;
  setActiveResource("permission");
  setActiveCategory(slug);
  renderCategories();
  $("#resultView").hidden = true;
  $("#manualView").hidden = false;
  $("#sourcePanel").hidden = true;
  $("#permissionPanel").hidden = false;
  $("#handbookPanel").hidden = true;
  $("#legalFormsPanel").hidden = true;
  $("#documentCategory").textContent = "권한신청서식";
  $("#documentStatus").textContent = "안내";
  $("#documentTitle").textContent = item.title;
  $("#documentSummary").textContent = "통합민원대 시스템 권한 신청 절차";
  $("#documentDepartment").textContent = item.department;
  $("#documentMeta").hidden = false;
  $("#documentUpdatedMeta").hidden = true;
  $("#permissionTitle").textContent = item.title;
  $("#permissionDepartment").textContent = item.department;
  $("#permissionMethod").textContent = item.method;
  const notes = $("#permissionNotes");
  const noteList = notes.querySelector("ul");
  noteList.replaceChildren();
  item.notes.forEach((note) => {
    const row = document.createElement("li");
    row.textContent = note;
    noteList.appendChild(row);
  });
  notes.hidden = !item.notes.length;
  const link = $("#permissionLink");
  link.hidden = !item.url;
  if (item.url) link.href = item.url;
  renderPermissionAttachments(attachmentPayload.items);
  $("#permissionAttachments").hidden = item.attachments_enabled === false;
  $("#permissionUploadResult").textContent = "";
  renderBreadcrumb(item.title, {
    label: "권한신청서식",
    action: () => showPermissionList(),
  });
  if (updateUrl) history.pushState({}, "", `/?resource=permission&item=${encodeURIComponent(item.slug)}`);
  closeCategoryPanel();
  $("#documentTitle").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderHandbookList(items) {
  const list = $("#resultList");
  list.replaceChildren();
  items.forEach((item) => {
    const article = document.createElement("article");
    const category = document.createElement("span");
    category.className = "result-category";
    category.textContent = "업무편람";
    const title = document.createElement("button");
    title.type = "button";
    title.className = "result-item-title";
    title.textContent = item.title;
    title.addEventListener("click", () => openHandbook(item.slug).catch(reportError));
    const summary = document.createElement("p");
    summary.textContent = item.attachment_count
      ? `첨부자료 ${item.attachment_count}개`
      : "첨부된 편람 자료가 없습니다.";
    article.append(category, title, summary);
    list.appendChild(article);
  });
}

function showHandbookList(query = "", updateUrl = true) {
  ++state.requestId;
  setActiveResource("handbook");
  setActiveCategory("");
  renderCategories();
  const normalized = query.trim().toLocaleLowerCase("ko-KR");
  const items = normalized
    ? state.handbooks.filter((item) => item.title.toLocaleLowerCase("ko-KR").includes(normalized))
    : state.handbooks;
  $("#manualView").hidden = true;
  $("#resultView").hidden = false;
  $("#resultTotal").textContent = items.length.toLocaleString("ko-KR");
  $("#resultTitle").textContent = query ? `“${query}” 편람 검색 결과` : "전체 편람";
  renderBreadcrumb(query ? "편람 통합검색" : "편람");
  renderHandbookList(items);
  if (updateUrl) history.pushState({}, "", query ? `/?resource=handbook&q=${encodeURIComponent(query)}` : "/?resource=handbook");
  closeCategoryPanel();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderHandbookAttachments(items) {
  const list = $("#handbookAttachmentList");
  const viewer = $("#handbookPdfViewer");
  list.replaceChildren();
  viewer.hidden = true;
  $("#handbookPdfFrame").removeAttribute("src");
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "permission-attachment-empty";
    empty.textContent = "첨부된 편람 자료가 없습니다.";
    list.appendChild(empty);
    return;
  }
  let firstPdf = null;
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "handbook-attachment-row";
    const type = document.createElement("b");
    type.textContent = item.file_type.toUpperCase();
    const copy = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.original_filename;
    const meta = document.createElement("span");
    meta.textContent = `${formatBytes(item.size_bytes)} · ${formatDate(item.created_at)} · ${item.created_by}`;
    copy.append(name, meta);
    const actions = document.createElement("div");
    if (item.file_type === "pdf") {
      firstPdf ||= item;
      const view = document.createElement("button");
      view.type = "button";
      view.textContent = "바로보기";
      view.addEventListener("click", () => showHandbookPdf(item));
      actions.appendChild(view);
    }
    const download = document.createElement("a");
    download.href = `/api/handbook-attachments/${item.id}/download`;
    download.textContent = "내려받기";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "delete";
    remove.textContent = "삭제";
    remove.addEventListener("click", () => deleteHandbookAttachment(item.id).catch(reportError));
    actions.append(download, remove);
    row.append(type, copy, actions);
    list.appendChild(row);
  });
  if (firstPdf) showHandbookPdf(firstPdf);
}

function showHandbookPdf(item) {
  const url = `/api/handbook-attachments/${item.id}/view#view=FitH`;
  $("#handbookPdfTitle").textContent = item.original_filename;
  $("#handbookPdfOpen").href = url;
  $("#handbookPdfFrame").src = url;
  $("#handbookPdfViewer").hidden = false;
}

async function reloadHandbook() {
  if (!state.currentHandbook) return;
  const item = await api(`/api/handbooks/${encodeURIComponent(state.currentHandbook.slug)}`);
  state.currentHandbook = item;
  renderHandbookAttachments(item.attachments);
  const payload = await api("/api/handbooks");
  state.handbooks = payload.items;
  renderCategories();
  setActiveCategory(item.slug);
}

async function deleteHandbookAttachment(attachmentId) {
  const adminKey = $("#handbookAdminKey").value;
  const result = $("#handbookUploadResult");
  if (!adminKey) {
    result.textContent = "자료를 삭제하려면 관리자 키를 입력하세요.";
    result.className = "permission-upload-result error";
    return;
  }
  if (!window.confirm("이 편람 자료를 삭제하시겠습니까? 삭제 후에는 복구할 수 없습니다.")) return;
  const response = await fetch(`/api/admin/handbook-attachments/${attachmentId}`, {
    method: "DELETE",
    headers: adminAuthHeaders(adminKey),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "편람 자료 삭제에 실패했습니다.");
  await reloadHandbook();
  result.textContent = payload.message;
  result.className = "permission-upload-result success";
}

async function openHandbook(slug, updateUrl = true) {
  const requestId = ++state.requestId;
  const item = await api(`/api/handbooks/${encodeURIComponent(slug)}`);
  if (requestId !== state.requestId) return;
  state.currentHandbook = item;
  setActiveResource("handbook");
  setActiveCategory(slug);
  renderCategories();
  $("#resultView").hidden = true;
  $("#manualView").hidden = false;
  $("#sourcePanel").hidden = true;
  $("#permissionPanel").hidden = true;
  $("#handbookPanel").hidden = false;
  $("#legalFormsPanel").hidden = true;
  $("#documentCategory").textContent = "업무편람";
  $("#documentStatus").textContent = "자료";
  $("#documentTitle").textContent = item.title;
  $("#documentSummary").textContent = "통합민원대 업무편람 첨부자료";
  $("#documentMeta").hidden = true;
  $("#handbookTitle").textContent = item.title;
  $("#handbookUploadResult").textContent = "";
  renderHandbookAttachments(item.attachments);
  renderBreadcrumb(item.title, { label: "편람", action: () => showHandbookList() });
  if (updateUrl) history.pushState({}, "", `/?resource=handbook&item=${encodeURIComponent(item.slug)}`);
  closeCategoryPanel();
  $("#documentTitle").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showLegalForms(updateUrl = true) {
  ++state.requestId;
  setActiveResource("legalForms");
  setActiveCategory("");
  $("#categoryEyebrow").textContent = "법령 서식";
  $("#allDocumentsLabel").textContent = "별표·서식 검색";
  $("#totalCategoryCount").textContent = "";
  $("#categoryList").replaceChildren();
  $("#resultView").hidden = true;
  $("#manualView").hidden = false;
  $("#sourcePanel").hidden = true;
  $("#permissionPanel").hidden = true;
  $("#handbookPanel").hidden = true;
  $("#legalFormsPanel").hidden = false;
  $("#documentCategory").textContent = "법령정보";
  $("#documentStatus").textContent = "검색";
  $("#documentTitle").textContent = "국가법령정보센터 별표서식";
  $("#documentSummary").textContent = "법령에 포함된 별표와 서식을 검색하고 내려받습니다.";
  $("#documentMeta").hidden = true;
  const frame = $("#legalFormsFrame");
  if (!frame.hasAttribute("src")) frame.src = "/law-proxy/lsBylSc.do?menuId=9&subMenuId=55&tabMenuId=261&eventGubun=060118";
  renderBreadcrumb("국가법령정보센터 별표서식");
  if (updateUrl) history.pushState({}, "", "/?resource=legalForms");
  closeCategoryPanel();
  $("#documentTitle").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showResourceSection(resource, updateUrl = true) {
  const label = resourceLabels[resource];
  if (!label) return;
  ++state.requestId;
  setActiveResource(resource);
  setActiveCategory("");
  $("#categoryEyebrow").textContent = label;
  $("#allDocumentsLabel").textContent = `전체 ${label}`;
  const total = resource === "links" ? state.relatedSites.length : 0;
  $("#totalCategoryCount").textContent = total;
  $("#categoryList").replaceChildren();
  $("#manualView").hidden = true;
  $("#resultView").hidden = false;
  $("#resultTotal").textContent = total;
  $("#resultTitle").textContent = label;
  renderBreadcrumb(label);
  const list = $("#resultList");
  list.replaceChildren();
  if (resource === "links") {
    renderRelatedSites(list);
    if (updateUrl) history.pushState({}, "", "/?resource=links");
    closeCategoryPanel();
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  const empty = document.createElement("div");
  empty.className = "empty-results";
  const title = document.createElement("strong");
  title.textContent = "등록된 자료가 없습니다.";
  const description = document.createElement("span");
  description.textContent = `${label} 자료가 등록되면 이곳에서 바로 확인할 수 있습니다.`;
  empty.append(title, description);
  list.appendChild(empty);
  if (updateUrl) history.pushState({}, "", `/?resource=${resource}`);
  closeCategoryPanel();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openCategory(category) {
  const payload = await fetchAllDocuments({ category });
  if (payload.items.length === 1) {
    await openDocument(payload.items[0].slug);
    closeCategoryPanel();
    return;
  }
  await showDocumentList({ category });
}

function setActiveCategory(category = "") {
  state.activeCategory = category;
  $("#allDocumentsButton").classList.toggle("active", !category);
  $("#allDocumentsButton").setAttribute("aria-current", !category ? "page" : "false");
  $$("#categoryList button").forEach((button) => {
    const active = button.dataset.category === category;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
}

function renderResultList(items) {
  const list = $("#resultList");
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-results";
    empty.innerHTML = "<strong>검색 결과가 없습니다.</strong><span>검색어를 줄이거나 다른 업무 분류를 선택해 보세요.</span>";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const article = document.createElement("article");
    const category = document.createElement("span");
    category.className = "result-category";
    category.textContent = item.category;
    const title = document.createElement("button");
    title.type = "button";
    title.className = "result-item-title";
    title.textContent = item.title;
    title.addEventListener("click", () => openDocument(item.slug).catch(reportError));
    const summary = document.createElement("p");
    summary.textContent = item.summary || "등록된 문서 요약이 없습니다.";
    const meta = document.createElement("div");
    [item.department, `${formatDate(item.updated_at)} 개정`].forEach((value) => {
      const span = document.createElement("span");
      span.textContent = value;
      meta.appendChild(span);
    });
    article.append(category, title, summary, meta);
    list.appendChild(article);
  });
}

async function showDocumentList({ category = "", query = "" } = {}, updateUrl = true) {
  const requestId = ++state.requestId;
  const parameters = {};
  if (category) parameters.category = category;
  if (query) parameters.q = query;
  const payload = await fetchAllDocuments(parameters);
  if (requestId !== state.requestId) return;
  state.documents = payload.items;
  setActiveResource("manual");
  renderCategories();
  setActiveCategory(category);
  $("#manualView").hidden = true;
  $("#resultView").hidden = false;
  $("#resultTotal").textContent = payload.total.toLocaleString("ko-KR");
  $("#resultTitle").textContent = query ? `“${query}” 검색 결과` : category || "전체 업무 매뉴얼";
  renderBreadcrumb(query ? "통합검색" : category || "전체 업무");
  renderResultList(payload.items);
  if (updateUrl) history.pushState({}, "", query ? `/?q=${encodeURIComponent(query)}` : category ? `/?category=${encodeURIComponent(category)}` : "/");
  closeCategoryPanel();
  $("#resultTitle").setAttribute("tabindex", "-1");
  $("#resultTitle").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function createOutlineLink(item) {
  const link = document.createElement("a");
  link.href = `#${item.anchor}`;
  link.className = `level-${item.level}`;
  link.textContent = item.title;
    link.addEventListener("click", () => $("#mobileOutlineList").classList.remove("open"));
    link.addEventListener("click", () => $("#outlineToggle").setAttribute("aria-expanded", "false"));
  return link;
}

function renderOutline(outline) {
  [$("#outlineList"), $("#mobileOutlineList")].forEach((container) => {
    container.replaceChildren();
    if (!outline.length) {
      const empty = document.createElement("p");
      empty.className = "aside-empty";
      empty.textContent = "자동 추출된 목차가 없습니다.";
      container.appendChild(empty);
      return;
    }
    outline.forEach((item) => container.appendChild(createOutlineLink(item)));
  });
}

function renderContent(documentData) {
  const content = $("#manualContent");
  content.replaceChildren();
  const headingMap = new Map(documentData.outline.map((item) => [item.position, item]));
  documentData.content.split(/\r?\n/).forEach((rawLine, position) => {
    const line = rawLine.trim();
    if (!line) return;
    const outline = headingMap.get(position);
    let element;
    if (outline) {
      element = document.createElement(outline.level === 1 ? "h2" : outline.level === 2 ? "h3" : "h4");
      element.id = outline.anchor;
    } else if (/^[·•*-]\s*/.test(line)) {
      element = document.createElement("p");
      element.className = "bullet-line";
    } else if (/^(※|☑|★)/.test(line)) {
      element = document.createElement("p");
      element.className = "notice-line";
    } else {
      element = document.createElement("p");
    }
    element.textContent = element.classList.contains("bullet-line") ? line.replace(/^[·•*-]\s*/, "") : line;
    content.appendChild(element);
  });
}

function renderVersions(items) {
  const list = $("#historyList");
  list.replaceChildren();
  $("#historyCount").textContent = items.length;
  items.forEach((item, index) => {
    const row = document.createElement("li");
    if (index === 0) row.className = "current";
    const version = document.createElement("b");
    version.textContent = `버전 ${item.version}`;
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.change_note || "개정사유 없음";
    const meta = document.createElement("span");
    meta.textContent = `${formatDate(item.created_at)} · ${item.created_by}${item.original_filename ? ` · ${item.original_filename}` : ""}`;
    copy.append(title, meta);
    row.append(version, copy);
    list.appendChild(row);
  });
}

async function openDocument(identifier, updateUrl = true) {
  const requestId = ++state.requestId;
  const documentData = await api(`/api/documents/${encodeURIComponent(identifier)}`);
  if (requestId !== state.requestId) return;
  state.current = documentData;
  setActiveResource("manual");
  renderCategories();
  setActiveCategory(documentData.category);
  $("#resultView").hidden = true;
  $("#manualView").hidden = false;
  $("#sourcePanel").hidden = false;
  $("#permissionPanel").hidden = true;
  $("#handbookPanel").hidden = true;
  $("#legalFormsPanel").hidden = true;
  $("#documentCategory").textContent = documentData.category;
  $("#documentStatus").textContent = documentData.status === "published" ? "현행" : "보관";
  $("#documentTitle").textContent = documentData.title;
  $("#documentSummary").textContent = documentData.summary;
  $("#documentDepartment").textContent = documentData.department;
  $("#documentMeta").hidden = false;
  $("#documentUpdatedMeta").hidden = false;
  $("#documentUpdated").textContent = formatDate(documentData.updated_at);
  renderBreadcrumb(documentData.title, {
    label: documentData.category,
    action: () => showDocumentList({ category: documentData.category }).catch(reportError),
  });
  const pageRange = documentData.file_type === "sample" ? sourcePages[documentData.category] : null;
  if (pageRange) {
    const [startPage, endPage] = pageRange;
    const sourceUrl = `/source-manual.pdf?start=${startPage}&end=${endPage}#page=1&view=FitH`;
    $("#sourcePageRange").textContent = startPage === endPage ? `${startPage}쪽` : `${startPage}~${endPage}쪽`;
    $("#sourceFrame").src = sourceUrl;
    $("#sourceOpenLink").href = sourceUrl;
    $("#sourceFallbackLink").href = sourceUrl;
  } else {
    $("#sourceFrame").removeAttribute("src");
    $("#sourcePageRange").textContent = "연결된 원문 없음";
  }
  if (updateUrl) history.pushState({}, "", `/?document=${encodeURIComponent(documentData.slug)}`);
  $("#documentTitle").setAttribute("tabindex", "-1");
  $("#documentTitle").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function closeCategoryPanel() {
  const panel = $("#categoryPanel");
  const restoreFocus = panel.contains(document.activeElement);
  panel.classList.remove("open");
  $("#mobileMenuButton").setAttribute("aria-expanded", "false");
  if (window.matchMedia("(max-width: 760px)").matches) {
    panel.inert = true;
    panel.setAttribute("aria-hidden", "true");
    if (restoreFocus) $("#mobileMenuButton").focus();
  }
}

function openCategoryPanel() {
  const panel = $("#categoryPanel");
  panel.inert = false;
  panel.removeAttribute("aria-hidden");
  panel.classList.add("open");
  $("#mobileMenuButton").setAttribute("aria-expanded", "true");
  panel.querySelector("button")?.focus();
}

function bindEvents() {
  $("#searchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = $("#searchInput").value.trim();
    if (!/[0-9A-Za-z가-힣]/.test(query)) { showToast("검색할 글자를 입력하세요."); return; }
    try {
      if (state.activeResource === "permission") showPermissionList(query);
      else if (state.activeResource === "handbook") showHandbookList(query);
      else await showDocumentList({ query });
    } catch (error) { reportError(error); }
  });
  $$("[data-query]").forEach((button) => button.addEventListener("click", () => {
    $("#searchInput").value = button.dataset.query;
    showDocumentList({ query: button.dataset.query }).catch(reportError);
  }));
  $("#allDocumentsButton").addEventListener("click", () => {
    if (state.activeResource === "permission") showPermissionList();
    else if (state.activeResource === "handbook") showHandbookList();
    else if (state.activeResource === "legalForms") showLegalForms();
    else if (state.activeResource === "manual") showDocumentList().catch(reportError);
    else showResourceSection(state.activeResource);
  });
  $$('[data-resource]').forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.resource === "manual") {
      history.pushState({}, "", "/");
      initializeRoute(false).catch(reportError);
    }
    else if (button.dataset.resource === "permission") showPermissionList();
    else if (button.dataset.resource === "handbook") showHandbookList();
    else if (button.dataset.resource === "legalForms") showLegalForms();
    else showResourceSection(button.dataset.resource);
  }));
  $("#mobileMenuButton").addEventListener("click", () => {
    const panel = $("#categoryPanel");
    if (panel.classList.contains("open")) closeCategoryPanel(); else openCategoryPanel();
  });
  $("#permissionUploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    const result = $("#permissionUploadResult");
    const file = $("#permissionFile").files[0];
    const adminKey = $("#permissionAdminKey").value;
    const adminName = $("#permissionAdminName").value.trim();
    if (!state.currentPermission || !file || !adminKey || !adminName) {
      result.textContent = "관리자 키, 담당자명과 한글파일을 모두 입력하세요.";
      result.className = "permission-upload-result error";
      return;
    }
    const form = new FormData();
    form.append("permission_slug", state.currentPermission.slug);
    form.append("admin_name", adminName);
    form.append("file", file);
    submit.disabled = true;
    result.textContent = "개인정보와 문서 구조를 검사하고 있습니다.";
    result.className = "permission-upload-result";
    try {
      const response = await fetch("/api/admin/permission-attachments", {
        method: "POST",
        headers: adminAuthHeaders(adminKey),
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || "첨부파일 등록에 실패했습니다.");
      }
      await loadPermissionAttachments(state.currentPermission.slug);
      $("#permissionFile").value = "";
      result.textContent = payload.message;
      result.className = "permission-upload-result success";
    } catch (error) {
      result.textContent = error.message;
      result.className = "permission-upload-result error";
    } finally {
      submit.disabled = false;
    }
  });
  $("#handbookUploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    const result = $("#handbookUploadResult");
    const file = $("#handbookFile").files[0];
    const adminKey = $("#handbookAdminKey").value;
    const adminName = $("#handbookAdminName").value.trim();
    if (!state.currentHandbook || !file || !adminKey || !adminName) {
      result.textContent = "관리자 키, 담당자명과 편람 파일을 모두 입력하세요.";
      result.className = "permission-upload-result error";
      return;
    }
    const form = new FormData();
    form.append("category_slug", state.currentHandbook.slug);
    form.append("admin_name", adminName);
    form.append("file", file);
    submit.disabled = true;
    result.textContent = "개인정보와 문서 구조를 검사하고 있습니다.";
    result.className = "permission-upload-result";
    try {
      const response = await fetch("/api/admin/handbook-attachments", {
        method: "POST",
        headers: adminAuthHeaders(adminKey),
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) {
        const detail = payload.detail;
        throw new Error(typeof detail === "string" ? detail : detail?.message || "편람 자료 등록에 실패했습니다.");
      }
      await reloadHandbook();
      $("#handbookFile").value = "";
      result.textContent = payload.message;
      result.className = "permission-upload-result success";
    } catch (error) {
      result.textContent = error.message;
      result.className = "permission-upload-result error";
    } finally {
      submit.disabled = false;
    }
  });
  $("#relatedSiteForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = $("#relatedSiteResult");
    const submit = event.currentTarget.querySelector('button[type="submit"]');
    const adminKey = $("#relatedSiteAdminKey").value;
    const adminName = $("#relatedSiteAdminName").value.trim();
    const name = $("#relatedSiteName").value.trim();
    const url = $("#relatedSiteUrl").value.trim();
    if (!adminKey || !adminName || !name || !url) {
      result.textContent = "모든 항목을 입력하세요.";
      result.className = "error";
      return;
    }
    const form = new FormData();
    form.append("admin_name", adminName);
    form.append("name", name);
    form.append("url", url);
    submit.disabled = true;
    result.textContent = "사이트를 등록하고 있습니다.";
    result.className = "";
    try {
      const response = await fetch("/api/admin/related-sites", {
        method: "POST",
        headers: adminAuthHeaders(adminKey),
        body: form,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "사이트 등록에 실패했습니다.");
      await loadRelatedSites();
      $("#relatedSiteName").value = "";
      $("#relatedSiteUrl").value = "";
      result.textContent = payload.message;
      result.className = "success";
    } catch (error) {
      result.textContent = error.message;
      result.className = "error";
    } finally {
      submit.disabled = false;
    }
  });
  window.addEventListener("popstate", () => initializeRoute(false).catch(reportError));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeCategoryPanel();
  });
  window.matchMedia("(max-width: 760px)").addEventListener("change", (event) => {
    if (event.matches) closeCategoryPanel();
    else { $("#categoryPanel").inert = false; $("#categoryPanel").removeAttribute("aria-hidden"); }
  });
}

async function initializeRoute(updateUrl = true) {
  const params = new URLSearchParams(location.search);
  if (params.get("resource") === "permission") {
    if (params.get("item")) return openPermission(params.get("item"), updateUrl);
    return showPermissionList(params.get("q") || "", updateUrl);
  }
  if (params.get("resource") === "handbook") {
    if (params.get("item")) return openHandbook(params.get("item"), updateUrl);
    return showHandbookList(params.get("q") || "", updateUrl);
  }
  if (params.get("resource") === "legalForms") return showLegalForms(updateUrl);
  if (resourceLabels[params.get("resource")]) return showResourceSection(params.get("resource"), updateUrl);
  if (params.get("document")) return openDocument(params.get("document"), updateUrl);
  if (params.get("q")) {
    const query = params.get("q");
    $("#searchInput").value = query;
    if (!/[0-9A-Za-z가-힣]/.test(query)) return showDocumentList({}, updateUrl);
    return showDocumentList({ query }, updateUrl);
  }
  if (params.get("category")) return showDocumentList({ category: params.get("category") }, updateUrl);
  const payload = await api("/api/documents?page_size=1");
  if (payload.items.length) return openDocument(payload.items[0].slug, updateUrl);
  return showDocumentList({}, updateUrl);
}

async function main() {
  bindEvents();
  if (window.matchMedia("(max-width: 760px)").matches) closeCategoryPanel();
  try {
    const [categoryPayload, permissionPayload, handbookPayload, relatedSitePayload] = await Promise.all([
      api("/api/categories"),
      api("/api/permissions"),
      api("/api/handbooks"),
      api("/api/related-sites"),
    ]);
    state.categories = categoryPayload.items;
    state.permissions = permissionPayload.items;
    state.handbooks = handbookPayload.items;
    state.relatedSites = relatedSitePayload.items;
    renderRelatedSitePanel();
    renderCategories();
    await initializeRoute(false);
  } catch (error) {
    $("#documentTitle").textContent = "자료를 불러올 수 없습니다";
    $("#documentSummary").textContent = "API 서버 실행 상태를 확인해 주세요.";
    showToast(error.message);
  }
}

main();
