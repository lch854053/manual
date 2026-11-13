import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StaticBuildTest(unittest.TestCase):
    def test_catalog_and_assets(self):
        records = json.loads((ROOT / 'site/catalog.json').read_text())
        sources = [p for p in (ROOT / 'documents').rglob('*') if p.is_file() and not p.name.startswith('.')]
        self.assertEqual(len(records), len(sources))
        self.assertEqual(len({d['id'] for d in records}), len(records))
        for item in records:
            self.assertTrue((ROOT / 'site' / item['url']).is_file())
            self.assertEqual((ROOT / 'site' / item['url']).stat().st_size, item['size'])
        manual = next(d for d in records if d['kind'] == '업무매뉴얼')
        self.assertEqual(len(manual['pages']), 73)
        self.assertEqual(len(manual['sections']), 12)
        self.assertIn('출생', manual['pages'][2])
        for section in manual['sections']:
            self.assertTrue(1 <= section['start'] <= section['end'] <= 73)

    def test_no_server_dependencies(self):
        html = (ROOT / 'site/index.html').read_text()
        javascript = (ROOT / 'site/app.js').read_text()
        self.assertNotIn('/api/', javascript)
        self.assertNotIn('/admin', html)
        self.assertNotIn('href="/', html)
        self.assertNotIn('src="/', html)


if __name__ == '__main__':
    unittest.main()
