import hashlib,json,zipfile
from pathlib import Path
root=Path(__file__).resolve().parent
cc=(root/'predictions-180.jsonl').exists()
pred_name='predictions-180.jsonl' if cc else 'dynamic180-predictions.jsonl'
score_name='cc-full-rescored.json' if cc else 'dynamic180-rescored.json'
manifest_name='source-manifest.json' if cc else 'source-manifest-180.json'
archive_name='raw-cc-runs.zip' if cc else 'raw-dynamic-selected-runs.zip'
def read(name): return json.loads((root/name).read_text(encoding='utf-8-sig'))
pred=[json.loads(l) for l in (root/pred_name).read_text(encoding='utf-8-sig').splitlines() if l.strip()]
scores=read(score_name); manifest=read(manifest_name)
assert len(pred)==180 and {p['idx'] for p in pred}==set(range(1,181))
assert len(scores['per_index'])==180 and {p['idx'] for p in scores['per_index']}==set(range(1,181))
assert sum(p['final_pass'] for p in scores['per_index'])==scores['summary']['final']['passed']
assert sum(bool(p.get('plan')) for p in pred)==scores['summary']['delivery']['passed']
byidx={p['idx']:p for p in pred}
with zipfile.ZipFile(root/archive_name) as z:
 assert z.testzip() is None
 assert len(z.namelist())==len(manifest['files'])
 for f in manifest['files']:
  data=z.read(f['path']); assert hashlib.sha256(data).hexdigest()==f['sha256'],f['path']
 for p in manifest['per_index']:
  name='attempts.jsonl' if cc else p['source']; line=p['source_line'] if cc else p['line']
  r=json.loads(z.read(name).decode('utf-8-sig').splitlines()[line-1])
  assert r['idx']==p['idx'] and r.get('plan')==byidx[p['idx']].get('plan'),p['idx']
for name,sha in read('SHA256SUMS.json').items():
 assert hashlib.sha256((root/name).read_bytes()).hexdigest()==sha,name
print('Verified 180 unique indices, score totals, source plans, archive CRC and all published hashes.')
