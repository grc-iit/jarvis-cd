#!/opt/biobb-env/bin/python
"""Five-step MD setup for a single PDB. Exit 0 on full success."""
import os, sys, shutil, time
pdb_in = sys.argv[1]
workdir = sys.argv[2]
os.makedirs(workdir, exist_ok=True)
os.chdir(workdir)
code = os.path.splitext(os.path.basename(pdb_in))[0]
t0 = time.time()
results = {}

local = os.path.join(workdir, code + '.pdb')
if not os.path.exists(local):
    shutil.copy(pdb_in, local)
results['1_stage'] = 'PASS'

from biobb_model.model.fix_side_chain import fix_side_chain
from biobb_gromacs.gromacs.pdb2gmx   import pdb2gmx
from biobb_gromacs.gromacs.editconf  import editconf
from biobb_gromacs.gromacs.solvate   import solvate

def ok(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0

fixed = os.path.join(workdir, code + '_fixed.pdb')
try:
    fix_side_chain(input_pdb_path=local, output_pdb_path=fixed)
    results['2_fix_side_chain'] = 'PASS' if ok(fixed) else 'FAIL'
except Exception as e:
    results['2_fix_side_chain'] = f'FAIL: {e}'

gro  = os.path.join(workdir, code + '_pdb2gmx.gro')
topz = os.path.join(workdir, code + '_pdb2gmx_top.zip')
if results['2_fix_side_chain'] == 'PASS':
    try:
        pdb2gmx(input_pdb_path=fixed, output_gro_path=gro, output_top_zip_path=topz)
        results['3_pdb2gmx'] = 'PASS' if ok(gro) else 'FAIL'
    except Exception as e:
        results['3_pdb2gmx'] = f'FAIL: {e}'

    if results.get('3_pdb2gmx') == 'PASS':
        box = os.path.join(workdir, code + '_editconf.gro')
        try:
            editconf(input_gro_path=gro, output_gro_path=box,
                     properties={'box_type': 'cubic', 'distance_to_molecule': 1.0})
            results['4_editconf'] = 'PASS' if ok(box) else 'FAIL'
        except Exception as e:
            results['4_editconf'] = f'FAIL: {e}'

        if results.get('4_editconf') == 'PASS':
            sol    = os.path.join(workdir, code + '_solvate.gro')
            soltop = os.path.join(workdir, code + '_solvate_top.zip')
            try:
                solvate(input_solute_gro_path=box, output_gro_path=sol,
                        input_top_zip_path=topz, output_top_zip_path=soltop)
                results['5_solvate'] = 'PASS' if ok(sol) else 'FAIL'
            except Exception as e:
                results['5_solvate'] = f'FAIL: {e}'

print(f"\n=== {code} results ({time.time()-t0:.1f}s) ===")
for k, v in results.items():
    print(f"  {k}: {v}")
sys.exit(0 if all(v == 'PASS' for v in results.values()) else 1)
