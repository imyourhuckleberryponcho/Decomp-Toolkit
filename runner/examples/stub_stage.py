"""stub_stage.py - the smallest possible stage tool; shows the contract and is handy for testing a config.

usage (as a stage command): python runner/examples/stub_stage.py {unit} {func} --seconds {t} [--src {src}] [--win]

Prints some progress, sleeps a moment, and ends with the line the runner parses:
    RESULT <unit> <func> <base> <best> <path-or-NONE>
Without --win it reports no improvement (best == base). With --win it copies the source to a scratch file and claims
an exact match (a real tool would write its edited candidate there). A tool must never edit the real source file.
"""
import argparse
import os
import shutil
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument('unit')
ap.add_argument('func')
ap.add_argument('--seconds', type=int, default=5)
ap.add_argument('--src')
ap.add_argument('--win', action='store_true')
a = ap.parse_args()
print('stub working on %s %s' % (a.unit, a.func))
time.sleep(min(a.seconds, 3))
if a.win and a.src:
    out = os.path.abspath(os.path.join(os.getcwd(), 'build', 'scratch', 'stub_candidate_' + os.path.basename(a.src)))
    shutil.copyfile(a.src, out)
    print('RESULT %s %s 3 0 %s' % (a.unit, a.func, out.replace('\\', '/')))
else:
    print('RESULT %s %s 3 3 NONE' % (a.unit, a.func))
