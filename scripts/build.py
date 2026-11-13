"""Build a read-only GitHub Pages site from locally maintained documents."""
import hashlib
import json
from pathlib import Path
import shutil
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'documents'
OUT = ROOT / 'site'
RANGES = [('출생·사망', 3, 10), ('가족관계시스템', 11, 14), ('주민등록시스템', 15, 47), ('부동산종합공부', 48, 51), ('토지이용계획', 52, 52), ('건축행정시스템', 53, 55), ('차세대지방세', 56, 61), ('부동산공시가격', 62, 62), ('부동산거래관리', 63, 64), ('외국인정보공동이용', 65, 68), ('민원처리운영창구', 69, 72), ('자동차등록원부', 73, 73)]


def extract(path):
    if path.suffix.lower() == '.pdf':
        return [(page.extract_text() or '').strip() for page in PdfReader(path).pages]
    if path.suffix.lower() == '.hwpx':
        if not zipfile.is_zipfile(path):
            print(f'Warning: HWPX 확장자와 내부 형식 불일치, 다운로드만 제공: {path.name}')
            return []
        with zipfile.ZipFile(path) as archive:
            return ['\n'.join(' '.join(node.itertext()) for node in ET.fromstring(archive.read(name)).iter() if node.tag.endswith('}p')) for name in sorted(archive.namelist()) if name.startswith('Contents/section') and name.endswith('.xml')]
    return []


def build():
    OUT.mkdir(exist_ok=True)
    (OUT / 'files').mkdir(exist_ok=True)
    for path in (ROOT / 'web').iterdir():
        shutil.copy2(path, OUT / path.name)
    records = []
    for path in sorted(SOURCE.rglob('*')):
        if not path.is_file() or path.name.startswith('.'):
            continue
        relative = path.relative_to(SOURCE)
        identifier = hashlib.sha256(str(relative).encode()).hexdigest()[:16]
        suffix = path.suffix.lower()
        destination = f'files/{identifier}{suffix}'
        shutil.copy2(path, OUT / destination)
        parts = relative.parts
        kind = '권한신청서식' if '통합민원대 권한신청서' in parts else '편람' if '통합민원대 업무편람' in parts else '기본자료'
        pages = [text.encode('utf-8', errors='replace').decode('utf-8') for text in extract(path)]
        item = dict(id=identifier, title=unicodedata.normalize('NFC', path.stem), filename=path.name, category=unicodedata.normalize('NFC', parts[-2]) if len(parts) > 1 else '통합민원', kind=kind, url=destination, extension=suffix[1:].upper(), size=path.stat().st_size, pages=pages)
        if suffix == '.hwpx' and not pages:
            item['warning'] = '파일 확장자와 내부 형식이 달라 본문을 추출하지 못했습니다. 원본을 한글 프로그램에서 확인하세요.'
        if path.name.startswith('AI-') and suffix == '.pdf':
            item['kind'] = '업무매뉴얼'
            item['sections'] = [dict(title=title, start=start, end=end) for title, start, end in RANGES]
            if len(pages) != 73:
                raise ValueError('원본 쪽수가 변경되었습니다. 업무별 쪽수 범위를 검토하세요.')
        records.append(item)
    if not records:
        raise ValueError('documents/에 원본 자료를 넣어주세요.')
    (OUT / 'catalog.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    (OUT / '.nojekyll').touch()
    print(f'Built {len(records)} documents, {sum(len(r["pages"]) for r in records)} searchable pages')


if __name__ == '__main__':
    build()
