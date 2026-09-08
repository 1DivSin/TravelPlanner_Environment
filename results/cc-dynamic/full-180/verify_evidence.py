import hashlib,json,sys,zipfile
from pathlib import Path
ROOT=Path(sys.argv[1])
s=json.loads((ROOT/'coverage.json').read_text()); assert s['indices']==180 and s['prediction_records']==180
for n in ['coverage.json','redaction-report.json']:
 assert (ROOT/n).is_file(), n
assert any((ROOT/n).is_file() for n in ['source-manifest-180.json','source-manifest.json'])
for p in ROOT.glob('*.zip'):
 with zipfile.ZipFile(p) as z: assert z.testzip() is None
print('verified',ROOT,s['indices'],s.get('archive_files'))
