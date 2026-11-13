let documents = [], kind = '', selected = null;
const $ = (id) => document.getElementById(id);
const normalize = (value) => value.normalize('NFC').toLocaleLowerCase('ko').replace(/\s+/g, '');
function element(tag, text, className) { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; }
function filters() {
  $('types').replaceChildren();
  for (const name of ['', '업무매뉴얼', '권한신청서식', '편람', '기본자료']) {
    const button = element('button', name || '전체 자료'); button.type = 'button'; button.classList.toggle('active', kind === name); button.setAttribute('aria-pressed', String(kind === name));
    button.onclick = () => { kind = name; $('category').value = ''; filters(); render(); }; $('types').append(button);
  }
  const previous = $('category').value;
  $('category').replaceChildren(new Option('전체 분류', ''));
  [...new Set(documents.filter(d => !kind || d.kind === kind).map(d => d.category))].sort().forEach(c => $('category').add(new Option(c, c)));
  $('category').value = previous;
}
function render() {
  $('list').hidden = false; $('detail').hidden = true; $('viewer').replaceChildren();
  const query = normalize($('query').value.trim()), category = $('category').value;
  const matches = documents.filter(d => (!kind || d.kind === kind) && (!category || d.category === category)).map(d => ({d, hits: query ? d.pages.flatMap((text, i) => normalize(text).includes(query) ? [i] : []) : []})).filter(({d, hits}) => !query || normalize(d.title + d.category + d.kind).includes(query) || hits.length);
  $('count').textContent = `${matches.length}건`; $('results').replaceChildren();
  if (!matches.length) $('results').append(element('p', '검색 결과가 없습니다. 다른 검색어나 자료 분류를 선택해 주세요.', 'empty'));
  for (const {d, hits} of matches) {
    const row = element('article', '', 'result'); const info = element('div', '', 'info'); info.append(element('p', `${d.kind} / ${d.category}`, 'eyebrow'));
    const button = element('button', d.title, 'document-link'); button.onclick = () => navigate(d.id, hits[0] + 1 || 1); info.append(button);
    info.append(element('p', `${d.extension} · ${(d.size / 1024 / 1024).toFixed(1)} MB${d.extension === 'PDF' ? ` · ${d.pages.length}쪽` : ''}`, 'filemeta'));
    for (const i of hits.slice(0, 3)) { const hit = element('button', `${i + 1}쪽 · ${d.pages[i].replace(/\s+/g, ' ').slice(Math.max(0, d.pages[i].toLowerCase().indexOf($('query').value.toLowerCase()) - 40), Math.max(0, d.pages[i].toLowerCase().indexOf($('query').value.toLowerCase()) - 40) + 180)}`, 'hit'); hit.onclick = () => navigate(d.id, i + 1); info.append(hit); }
    const download = element('a', '다운로드 ↓', 'download'); download.href = d.url; download.download = d.filename; row.append(info, download); $('results').append(row);
  }
}
function navigate(id, page) { location.hash = new URLSearchParams({document: id, page}); }
function detail(d, page) {
  selected = d; $('list').hidden = true; $('detail').hidden = false; $('title').textContent = d.title; $('meta').textContent = `${d.kind} / ${d.category} · ${d.extension}`;
  if (d.warning) $('meta').textContent += ` · ${d.warning}`;
  $('download').href = d.url; $('download').download = d.filename; $('open').href = d.url + (d.extension === 'PDF' ? `#page=${page}` : '');
  $('sections').replaceChildren();
  for (const s of d.sections || []) { const button = element('button', `${s.title} ${s.start}–${s.end}쪽`); button.onclick = () => navigate(d.id, s.start); $('sections').append(button); }
  $('viewer').replaceChildren();
  if (d.extension === 'PDF') { const frame = document.createElement('iframe'); frame.title = `${d.title} 원문 ${page}쪽`; frame.src = `${d.url}#page=${page}`; $('viewer').append(frame, element('p', '미리보기가 표시되지 않으면 ‘원문 새 창 열기’ 또는 다운로드를 이용하세요.', 'note')); }
  else if (['JPG', 'JPEG', 'PNG'].includes(d.extension)) { const image = document.createElement('img'); image.src = d.url; image.alt = d.title; $('viewer').append(image); }
  else $('viewer').append(element('p', '한글 문서는 원본을 다운로드하여 한글 또는 호환 뷰어에서 열어주세요. 추출 가능한 본문은 아래에서 확인할 수 있습니다.', 'notice'));
  $('pages').replaceChildren(); $('text').hidden = !d.pages.length; $('text').open = d.extension === 'HWPX';
  d.pages.forEach((text, i) => { const section = element('section', '', 'page'); section.append(element('h2', d.extension === 'PDF' ? `${i + 1}쪽` : `본문 ${i + 1}`), element('pre', text || '이 페이지에서 텍스트를 추출하지 못했습니다. 원문을 확인하세요.')); $('pages').append(section); });
  $('back').focus();
}
function route() { const params = new URLSearchParams(location.hash.slice(1)), d = documents.find(d => d.id === params.get('document')); if (d) detail(d, Math.max(1, Math.min(d.pages.length || 1, Number(params.get('page')) || 1))); else render(); }
$('search').onsubmit = event => { event.preventDefault(); if (location.hash) location.hash = ''; else render(); };
$('category').onchange = () => { if (location.hash) location.hash = ''; else render(); };
$('back').onclick = () => { location.hash = ''; };
window.addEventListener('hashchange', route);
fetch('catalog.json').then(response => { if (!response.ok) throw Error(); return response.json(); }).then(data => { documents = data; filters(); route(); }).catch(() => { $('count').textContent = '자료를 불러오지 못했습니다. 새로고침해 주세요.'; });
