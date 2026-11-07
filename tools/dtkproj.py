"""dtkproj.py - shared project discovery for the other scripts in this folder.

Importing this module (``from dtkproj import *``) does three things:

  1. finds the project root (the folder that holds ``configure.py``):
       $DECOMP_REPO if set, else the current directory or the nearest parent that has configure.py;
  2. finds the version folder name used under build/ and config/ (``GAMEID`` of the dtk template, e.g. ``GAMEID``):
       $DECOMP_VERSION if set, else the only folder in build/ that contains ``report.json`` or ``asm/``,
       else the only folder in config/;
  3. changes the working directory to the project root, so every script can use the standard relative paths
       build/<VER>/asm/<unit>.s        target disassembly
       build/<VER>/obj/<unit>.o        target object (what the retail game contains)
       build/<VER>/src/<unit>.o        your compiled object
       build/<VER>/report.json         objdiff report
       build/binutils/*, build/tools/* binutils / dtk / objdiff-cli downloaded by the template

Exports: REPO, VER, S(), EXE ('.exe' on Windows, '' elsewhere), HERE (folder of the scripts), ROOT (= REPO),
         unit_command(unit) -> argv list that compiles src/<unit> exactly like ninja does,
         tool(name) -> path of a build/binutils or build/tools program.

Override anything with environment variables: DECOMP_REPO, DECOMP_VERSION, OBJDUMP, DTK, OBJDIFF.
"""
import os
import re
import shlex
import subprocess
import sys

EXE = '.exe' if os.name == 'nt' else ''
HERE = os.path.dirname(os.path.abspath(__file__)).replace('\\', '/')


def _find_repo():
    env = os.environ.get('DECOMP_REPO')
    if env:
        return os.path.abspath(env).replace('\\', '/')
    d = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, 'configure.py')):
            return d.replace('\\', '/')
        up = os.path.dirname(d)
        if up == d:
            sys.exit('dtkproj: no configure.py found in the current folder or its parents; '
                     'run from the project root or set DECOMP_REPO')
        d = up


def _find_version(repo):
    env = os.environ.get('DECOMP_VERSION')
    if env:
        return env
    b = os.path.join(repo, 'build')
    cands = []
    if os.path.isdir(b):
        for n in sorted(os.listdir(b)):
            p = os.path.join(b, n)
            if os.path.isdir(p) and (os.path.exists(os.path.join(p, 'report.json')) or os.path.isdir(os.path.join(p, 'asm'))):
                cands.append(n)
    if len(cands) == 1:
        return cands[0]
    c = os.path.join(repo, 'config')
    if os.path.isdir(c):
        vs = [n for n in sorted(os.listdir(c)) if os.path.isdir(os.path.join(c, n))]
        if len(vs) == 1:
            return vs[0]
        if cands:
            vs = [n for n in vs if n in cands] or vs
        if len(vs) >= 1:
            return vs[0]
    sys.exit('dtkproj: cannot tell the version folder; set DECOMP_VERSION (e.g. GAMEID)')


REPO = _find_repo()
ROOT = REPO
VER = _find_version(REPO)
os.chdir(REPO)
os.makedirs('build/scratch', exist_ok=True)


def S(s):
    """Fill the version folder into a path template: S('build/@V/obj/%s.o') % unit."""
    return s.replace('@V', VER)


def tool(name):
    """Absolute path of a program the template downloads (objdump, nm, objcopy, dtk, objdiff-cli ...)."""
    env = {'objdump': 'OBJDUMP', 'dtk': 'DTK', 'objdiff-cli': 'OBJDIFF'}.get(name)
    if env and os.environ.get(env):
        return os.environ[env]
    for sub in ('build/binutils/powerpc-eabi-' + name, 'build/binutils/' + name, 'build/tools/' + name):
        if os.path.exists(sub + EXE):
            return os.path.abspath(sub + EXE)
    return name   # hope it is on PATH


def unit_command(unit, out_dir=None, src=None):
    """argv that compiles one unit with the flags ninja uses (taken from `ninja -t commands`)."""
    unit = unit.replace('\\', '/')
    r = subprocess.run(['ninja', '-t', 'commands', 'build/%s/src/%s.o' % (VER, unit)],
                       capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    if not lines:
        sys.exit('no ninja command for %s (new file? run: python configure.py)' % unit)
    argv = shlex.split(lines[-1].replace('\\', '/'), posix=True)
    argv = [x for x in argv if x != '-MMD']
    argv[0] = os.path.abspath(argv[0])
    if out_dir:
        argv[argv.index('-o') + 1] = out_dir
    if src:
        k = argv.index('-c')
        argv[k + 1] = src
    return argv
