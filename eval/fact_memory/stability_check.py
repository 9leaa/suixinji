"""Repeat FM12 on separate spaces/output trees; never change the full-round score."""
from pathlib import Path
import subprocess,sys,shutil,json
ROOT=Path(__file__).resolve().parents[2]
OUT=Path('/Users/zhangchengjie/Projects/suixinji/workspace/outputs/fact-memory-20261020')
base=OUT/'stability-fm12';base.mkdir(exist_ok=True);rows=[]
for i in range(1,4):
 dest=base/f'trial-{i}';dest.mkdir(exist_ok=True)
 for name in ['cases.json','checkpoints.json']:shutil.copy2(OUT/name,dest/name)
 code=f"import sys;from pathlib import Path;sys.path.insert(0,'eval/fact_memory');import run_independent as r;r.OUT=Path({str(dest)!r});sys.argv=['run_independent.py','FM12'];r.main()"
 proc=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
 (dest/'run.log').write_text(proc.stdout+proc.stderr)
 if proc.returncode:row={'trial':i,'status':'framework_error','error':proc.stderr}
 else:
  result=json.loads((dest/'results.json').read_text());r=result['results'][0];row={'trial':i,'status':r['status'],'reason':r['reason'],'space':r['space'],'metadata':result['metadata'],'trace':str(next((dest/'traces').glob('FM12-*.json'))),'stages':r.get('stages',{})}
 rows.append(row);(OUT/'stability-fm12-summary.json').write_text(json.dumps({'separate_from_full_round_score':True,'trials':rows},ensure_ascii=False,indent=2));print(json.dumps(row,ensure_ascii=False),flush=True)
