"""compile_driver.py - the permuter's compile step: real compiler flags, then keep only one function.

Called by decomp-permuter (patched: via compile_native.json; unpatched: via compile.sh) as
    python compile_driver.py <run>/compile_native.json <source.c> -o <output.o>
It compiles the candidate with the unit's own command line (prefix + flags stored in the json by permsetup.py) and then
rewrites the object so that it holds only the named function: with `-inline deferred`, unused helper functions are also
emitted, and the permuter scores the whole .text section. (The full unit is still checked by fcheck at install time.)
"""
import json
import os
import re
import subprocess
import sys
import tempfile


def isolate(obj, func, cfg):
    repo = cfg['repo']
    scratch = os.path.join(repo, 'build', 'scratch')
    os.makedirs(scratch, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='isolate-', dir=scratch) as tmp:
        local = os.path.join(tmp, 'unit.o')
        with open(obj, 'rb') as f, open(local, 'wb') as g:
            g.write(f.read())
        asm = os.path.join(tmp, 'unit.s')
        r = subprocess.run([cfg['dtk'], 'elf', 'disasm', os.path.relpath(local, repo), os.path.relpath(asm, repo)],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode:
            sys.exit('dtk disasm failed: ' + r.stderr[-500:])
        text = open(asm, encoding='utf-8', errors='replace').read()
        # the plain name or its mangled form (name__F...); anonymous-namespace names carry the temp file's name
        sym = re.escape(func) + r'(?:__F\S*)?'
        sym = re.sub(r'\d+@unnamed@[^@]*@', r'\\d+@unnamed@[^@]*@', sym.replace(r'\@', '@'))
        m = re.search(r'^\.fn ("?)(' + sym + r')\1,.*?^\.endfn \1\2\1$', text, re.M | re.S)
        if m is None:
            sys.exit('function not found in the compiled object: ' + func)
        with open(asm, 'w', encoding='utf-8') as f:
            f.write('.include "macros.inc"\n.section .text, "ax"\n.balign 4\n' + m.group(0) + '\n')
        res = os.path.join(tmp, 'function.o')
        r = subprocess.run([cfg['as'], '-mgekko', '-I', os.path.join(repo, 'include'), '-o', res, asm],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode:
            sys.exit('assembler failed: ' + r.stderr[-500:])
        with open(res, 'rb') as f, open(obj, 'wb') as g:
            g.write(f.read())


def main():
    cfg = json.load(open(sys.argv[1], encoding='utf-8'))
    source, output = sys.argv[2], sys.argv[4]
    argv = cfg['prefix'] + cfg['flags'] + ['-c', source, '-o', output]
    p = subprocess.run(argv, cwd=cfg['repo'])
    if p.returncode:
        sys.exit(p.returncode)
    isolate(output, cfg['function'], cfg)


if __name__ == '__main__':
    if len(sys.argv) < 5 or '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        sys.exit(0 if '--help' in sys.argv or '-h' in sys.argv else 2)
    main()
