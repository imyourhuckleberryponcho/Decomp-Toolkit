"""permsetup.py - build a decomp-permuter run folder for ONE function of a dtk-template project.

usage (project root, or set DECOMP_REPO):
  python tools/permuter/permsetup.py <unit> <symbol> [--src FILE] [--regs] [--dirty] [--suffix S] [--mode auto|c|cpp]
                                     [--permuter DIR] [--runs DIR]

  unit    path under src/ without extension, e.g. game/menu
  symbol  the function's symbol in build/<VER>/asm/<unit>.s (mangled for C++: foo__FP3Bar, run__3FooFv)
  --src   take the source from this file instead of src/<unit>.cpp (scratch copy; same flags as the unit)
  --regs  weight the passes that usually fix register-allocation-only diffs (declaration order, temporaries,
          expression expansion)
  --dirty keep the permuter's hack passes (default: CLEAN weights, see pcommon.CLEAN_OFF)
  --mode  c = a C function or a C++ free function (default for symbols that do not demangle to Class::method)
          cpp = a C++ METHOD (default when the symbol demangles to Class::method)

Creates <runs>/<symbol>[suffix]/ with base.c, target.o (that function only), compile.sh + compile_native.json (the
unit's real compiler command, then keep only that function), settings.toml. Then:
  permcheck.py <symbol>      base score sane?        permrun.py <symbol> --seconds 300   run it

How: the unit is preprocessed with its real flags; the function stays AT ITS PLACE inside the otherwise untouched
file (everything else hidden from the permuter's parser with PERM_IGNORE, so the compiler still sees the real file and
inlining behaves as in the build). Methods (cpp mode) become a C-looking surrogate `permX(params)` whose body uses
`self->member`; hidden macros (`#define permX Class::meth`, `#define self this`) turn it back into the real method for
the compiler, so the generated code is the method's. Results are edits of the surrogate: translate them back by hand
or with permcollect.py. Not supported (reported, no setup): constructors/destructors, templates, reference
parameters, new/delete/operator in a method body.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcommon as P   # noqa: E402
from pcommon import REPO, VER, C_WORDS   # noqa: E402


def demangle(sym):
    r = subprocess.run([P.tool('dtk'), 'demangle', sym], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def match_brace(text, i):
    depth = 0
    while i < len(text):
        c = text[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def pretend_globals(func_src, context, names, local_words=()):
    """globals declared at file scope in the hidden context are unknown to the permuter's type-aware passes: return
    pretend declarations `T name;` / `T name[1];` for the identifiers the function uses. Conservative: odd things skipped."""
    used = set(re.findall(r"(?<![\w.>])([A-Za-z_]\w*)\b", func_src)) - set(local_words) - C_WORDS
    own = set(re.findall(r"[(,]\s*(?:const\s+)?[A-Za-z_][\w:]*\s*\**\s*([A-Za-z_]\w*)\s*(?=[,)])", func_src.split("{", 1)[0]))
    own |= set(re.findall(r"(?:^|[;{(])\s*(?:const\s+)?(?:unsigned\s+|signed\s+)?[A-Za-z_][\w:]*\s*\**\s+\**\s*([A-Za-z_]\w*)\s*(?=[=;,\[)])", func_src, re.M))
    used -= own | set(names) | set(re.findall(r"\b(?:struct|class|union|enum)\s+(\w+)", context))
    decls = []
    for name in sorted(used):
        m = re.search(r"^(?:static\s+|extern\s+)?(?:const\s+)?(?:struct\s+|class\s+)?([A-Za-z_]\w*)(?:\s+|\s*(\*+)\s*)"
                      r"(?:[A-Za-z_]\w*::)?" + re.escape(name) + r"\s*(\[[^\]\n]*\])?\s*(?:=[^;]*)?;", context, re.M)
        if not m:
            continue
        ty = m.group(1)
        if ty in C_WORDS and ty not in ("int", "char", "short", "long", "float", "double", "unsigned", "signed", "bool"):
            continue
        if ty in ("return", "goto", "typedef", "using", "namespace", "else", "case", "delete", "new", "throw"):
            continue
        if ty not in C_WORDS:
            names.append(ty)
        decls.append("%s %s%s%s;" % (ty, m.group(2) or "", name, "[1]" if m.group(3) else ""))
    return " ".join(decls)


def hide_typeof(func_src, names):
    """__typeof__(x) -> PERMTYPEOF_x (a pretend type for the parser) + '#define' lines for the compiler"""
    defs = []

    def sub(mm):
        name = "PERMTYPEOF_" + re.sub(r"\W", "_", mm.group(1))
        if name not in names:
            names.append(name)
            defs.append("#define %s __typeof__(%s)" % (name, mm.group(1)))
        return name
    return re.sub(r"\b__typeof__\s*\(\s*([\w.>-]+)\s*\)", sub, func_src), defs


def clean_for_parser(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"__declspec\s*\(\s*\w+\s*\)", "", text)
    text = re.sub(r"\b__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)", "", text)
    return re.sub(r"^#\s*(line\s+)?\d+.*$", "", text, flags=re.M)


def typedef_names(context):
    """names declared by typedefs (the parser gets them as plain `int` typedefs)"""
    names = []
    for tm in re.finditer(r"\btypedef\b", context):
        pos = tm.end()
        brace, semi = context.find("{", pos), context.find(";", pos)
        if brace != -1 and brace < semi:
            j = match_brace(context, brace)
            if j < 0:
                continue
            semi = context.find(";", j)
            decl = context[j + 1:semi]
        else:
            decl = context[pos:semi]
        for part in decl.split(","):
            fp = re.search(r"\(\s*\*\s*(\w+)\s*\)", part)
            if fp:
                names.append(fp.group(1))
                continue
            nm = re.search(r"(\w+)\s*(\[[^\]]*\]\s*)*$", part.strip())
            if nm:
                names.append(nm.group(1))
    return names


def no_asm(t):
    return re.sub(r"\basm\s+(\w[^;]*\))\s*;", r"\1;", t)


def class_members(text, cls, seen=None):
    """field and method names of class cls (and its bases) from preprocessed source"""
    seen = seen or set()
    if cls in seen:
        return set()
    seen.add(cls)
    out = set()
    for m in re.finditer(r'\b(?:class|struct)\s+%s\s*(:[^{;]*)?\{' % re.escape(cls), text):
        j = match_brace(text, m.end() - 1)
        flat, depth = [], 0
        for ch in text[m.end():j]:
            if ch == '{':
                depth += 1
                if depth == 1:
                    flat.append(';')
                continue
            if ch == '}':
                depth -= 1
                continue
            if depth == 0:
                flat.append(ch)
        flat = re.sub(r'\b(public|private|protected)\s*:', ';', ''.join(flat))
        for stmt in flat.split(';'):
            s = stmt.strip()
            if not s or s.startswith(('typedef', 'friend', 'enum', 'using')):
                continue
            if re.fullmatch(r'(?:class|struct|union)\s+\w+', s):   # a nested type, not a member
                continue
            mm = re.search(r'(~?[A-Za-z_]\w*)\s*\(', s)
            if mm and not re.search(r'\(\s*\*', s):
                out.add(mm.group(1))
                continue
            for part in s.split(','):
                mv = re.search(r'([A-Za-z_]\w*)\s*(\[[^\]]*\]\s*)*(:\s*\d+)?\s*$', part.strip())
                if mv:
                    out.add(mv.group(1))
            mf = re.search(r'\(\s*\*\s*(\w+)\s*\)', s)
            if mf:
                out.add(mf.group(1))
        if m.group(1):
            for base in re.findall(r'(?:public|private|protected)?\s*(?:\w+::)*(\w+)', m.group(1)[1:]):
                if base not in ('public', 'private', 'protected', 'virtual'):
                    out |= class_members(text, base, seen)
    return out


def enclosing_namespaces(text, pos):
    ns = []
    for m in re.finditer(r'\bnamespace\s*(\w*)\s*\{', text[:pos]):
        j = match_brace(text, m.end() - 1)
        if j < 0 or j > pos:
            ns.append(m.group(1))
    return ns


def preprocess(prefix, flags, src):
    r = subprocess.run(prefix + flags + ['-E', src], cwd=REPO, capture_output=True)
    if r.returncode:
        sys.exit('preprocess failed:\n' + P.decode(r.stderr)[-2000:])
    return P.decode(r.stdout).replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))

def write_compile_files(d, prefix, flags, function):
    cfg = {'repo': REPO, 'prefix': prefix, 'flags': flags, 'function': function, 'dtk': P.tool('dtk'), 'as': P.tool('as'),
           'driver': P.HERE + '/compile_driver.py'}
    json.dump(cfg, open(os.path.join(d, 'compile_native.json'), 'w', encoding='utf-8'))
    with open(os.path.join(d, 'compile.sh'), 'w', newline='\n') as f:
        f.write('#!/bin/bash\nexec "%s" "%s" "%s" "$1" -o "$3"\n' % (
            sys.executable.replace('\\', '/'), cfg['driver'], os.path.join(d, 'compile_native.json').replace('\\', '/')))


def setup_c(text, cname, flags_text):
    """-> (base.c text, func_src) for a plain function"""
    m = None
    for cand in re.finditer(r"^[^\n;{}#]*\b%s\s*\([^;{]*\)\s*\{" % re.escape(cname), text, re.M):
        if re.match(r"\s*(if|while|for|switch|return|else)\b", cand.group(0)):
            continue
        if cand.group(0)[:1] not in (" ", "\t"):    # definitions start in column 0, calls are indented
            m = cand
            break
    if not m:
        sys.exit("function definition of %s not found in the preprocessed source" % cname)
    start = m.start()
    i = match_brace(text, m.end() - 1)
    func_src = text[start:i + 1]
    had_extern_c = bool(re.match(r'^\s*extern\s*"C"\s*', func_src))
    func_src = re.sub(r'^\s*extern\s*"C"\s*', "", func_src)
    if re.search(r"&\s*\w+\s*[,)]", m.group(0)):
        sys.exit("unsupported: reference parameters (%s)" % m.group(0).strip()[:100])
    before, after = no_asm(text[:start]), no_asm(text[i + 1:])
    context = text[:start] + text[start:m.end() - 1].rstrip() + ";" + text[i + 1:]
    names = typedef_names(context)
    extra_defs = ""
    if "-lang=c++" in flags_text:
        names += re.findall(r"\b(?:struct|class|union|enum)\s+(\w+)", context)
        names.append("bool")
        type_names = set(names)
        quals = {}

        def colon(mm):
            q = mm.group(0)
            name = q.replace("::", "__")
            quals[name] = q
            if q.split("::")[-1] in type_names:
                names.append(name)
            return name
        func_src = re.sub(r"\b(static|reinterpret|const)_cast\s*<([^<>]+)>\s*\(", r"((\2))(", func_src)
        func_src = re.sub(r"(?<!::)\b[A-Za-z_]\w*(?:::[A-Za-z_~]\w*)+\b", colon, func_src)
        if quals:
            extra_defs += "\n" + "\n".join("#define %s %s" % kv for kv in quals.items()) + "\n"
    # C forbids a declaration right after a label (C++ allows it): an empty statement changes no code
    func_src = re.sub(r"((?:\bcase\b[^:;{}]*|\bdefault\s*|^\s*[A-Za-z_]\w*\s*):)(\s*\n\s*(?:const\s+)?[A-Za-z_]\w*\s*\**\s+\**\s*\w+\s*(?:=|;|\[))",
                      r"\1;\2", func_src, flags=re.M)
    func_src, tdefs = hide_typeof(func_src, names)
    if tdefs:
        extra_defs += "\n" + "\n".join(tdefs) + "\n"
    gdecls = pretend_globals(func_src, before, names)
    pretend = " ".join("typedef int %s;" % n for n in dict.fromkeys(names) if n not in C_WORDS) + " " + gdecls
    out = "PERM_IGNORE(" + (before + extra_defs).replace(",", "(,)") + ")\n"
    out += "PERM_PRETEND(" + pretend + ")\n"
    if had_extern_c:
        out += 'PERM_IGNORE(extern "C" {)\n'
    out += "PERM_RANDOMIZE(\n" + func_src + "\n)\n"
    if had_extern_c:
        out += "PERM_IGNORE(})\n"
    out += "PERM_IGNORE(" + after.replace(",", "(,)") + ")\n"
    return out


def setup_method(text, cls, meth, sym):
    """-> base.c text for a C++ method (surrogate permX)"""
    m = re.search(r'^([^\n;{}#]*?)((?:\w+::)*%s::%s)\s*\(([^;{]*)\)\s*(const)?\s*\{' % (re.escape(cls), re.escape(meth)), text, re.M)
    if not m:
        sys.exit('definition of %s::%s not found in the preprocessed source' % (cls, meth))
    ret, params, is_const = m.group(1).strip(), m.group(3).strip(), bool(m.group(4))
    if '&' in params or 'template' in ret:
        sys.exit('unsupported: reference parameters / templates (%s)' % m.group(0).strip()[:120])
    is_static = bool(re.search(r'\bstatic\s+[^;{}]*\b%s\s*\(' % re.escape(meth),
                               ''.join(re.findall(r'\b(?:class|struct)\s+%s\b[^{]*\{[^}]*' % re.escape(cls), text))))
    j = match_brace(text, m.end() - 1)
    body = text[m.end():j]
    if re.search(r'\bnew\b|\bdelete\b|\btemplate\b|\boperator\b', body):
        sys.exit('unsupported: new/delete/templates/operators in the body')
    members = class_members(text, cls)
    members -= C_WORDS | {'bool', 'this'}
    members -= set(re.findall(r'\b(?:class|struct|union|enum)\s+(\w+)', text)) | {cls}
    shadow = set(re.findall(r'(\w+)\s*(?:\[[^\]]*\])?\s*$', ','.join(p.strip() for p in params.split(',')))) if params and params != 'void' else set()
    shadow |= set(re.findall(r'(?:^|[;{(,])\s*(?:const\s+)?(?:unsigned\s+|signed\s+)?[A-Za-z_][\w:]*(?:\s*\*+\s*|\s+)(\w+)\s*(?:[=;,)\[])', body))
    body = re.sub(r'\b(static|reinterpret|const)_cast\s*<([^<>]+)>\s*\(', r'((\2))(', body)
    body = re.sub(r'\bthis\b', 'self', body)
    body = re.sub(r'((?:\bcase\b[^:;{}]*|\bdefault\s*):)(\s*\n\s*(?:const\s+)?[A-Za-z_][\w:]*\s*\**\s+\**\s*\w+\s*(?:=|;|\[))', r'\1;\2', body)
    if not is_static:
        def mem(mm):
            nm = mm.group(2)
            return mm.group(1) + 'self->' + nm if nm in members and nm not in shadow else mm.group(0)
        body = re.sub(r'((?<![\w.>:])(?<!->)\s*)\b([A-Za-z_]\w*)\b(?!\s*::)',
                      lambda mm: mem(mm) if not re.search(r'(\.|->|::)\s*$', body[:mm.start(2)][-3:]) else mm.group(0), body)
    defines = []
    type_names = set(re.findall(r'\b(?:class|struct|union|enum)\s+(\w+)', text)) | set(re.findall(r'\btypedef\b[^;]*?(\w+)\s*;', text))
    pretend_types = []

    def colon(mm):
        q = mm.group(0)
        name = q.replace('::', '__')
        if name not in [x[0] for x in defines]:
            defines.append((name, q))
            if q.split('::')[-1] in type_names:
                pretend_types.append(name)
        return name
    body = re.sub(r'\b[A-Za-z_]\w*(?:::[A-Za-z_~]\w*)+\b', colon, body)
    ret_c = re.sub(r'\b(static|inline|virtual)\b', '', ret).strip() or 'void'
    ret_c = re.sub(r'\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b', colon, ret_c)
    params_c = re.sub(r'\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b', colon, params)
    nparams = 0 if not params or params == 'void' else len(params.split(','))
    if is_const:
        args_ = ', '.join('a%d' % i for i in range(nparams))
        defines.append(('permX(%s)' % args_, '%s::%s(%s) const' % (cls, meth, args_)))
    else:
        defines.append(('permX', '%s::%s' % (cls, meth)))
    if not is_static:
        defines.append(('self', 'this'))
    plist = [params_c] if nparams else []
    func_src = '%s permX(%s) {%s}' % (ret_c, ', '.join(plist) or 'void', body)
    after = no_asm(text[j + 1:])
    after = '\n' + ''.join('#undef %s\n' % dq[0].split('(')[0] for dq in defines) + after
    context = no_asm(text[:m.start()]) + '\n' + '\n'.join('#define %s %s' % dq for dq in defines) + '\n'
    names = re.findall(r'\b(?:struct|class|union|enum)\s+(\w+)', context) + ['bool'] + pretend_types
    for tm in re.finditer(r'\btypedef\b[^;{]*?(\w+)\s*(?:\[[^\]]*\]\s*)*;', context):
        names.append(tm.group(1))
    func_src, tdefs = hide_typeof(func_src, names)
    if tdefs:
        context += '\n'.join(tdefs) + '\n'
    gdecls = pretend_globals(func_src, context, names, ('self',))
    pretend = ' '.join('typedef int %s;' % n for n in dict.fromkeys(names) if n not in C_WORDS) + ' ' + gdecls
    out = 'PERM_IGNORE(' + context.replace(',', '(,)') + ')\n'
    out += 'PERM_PRETEND(' + pretend + ')\n'
    out += 'PERM_RANDOMIZE(\n' + func_src + '\n)\n'
    out += 'PERM_IGNORE(' + after.replace(',', '(,)') + ')\n'
    return out


def setup(unit, sym, src=None, regs=False, dirty=False, suffix='', mode='auto', runs=None):
    """returns the run folder"""
    unit = unit.replace('\\', '/')
    dm = demangle(sym)
    qual = dm.split('(')[0] if dm else sym
    parts = [p for p in qual.split('::') if not p.startswith('@unnamed@')]
    is_method = len(parts) >= 2
    if mode == 'c':
        is_method = False
    elif mode == 'cpp':
        is_method = True
    d = os.path.join(P.runs_dir(runs), P.safe(sym) + suffix)
    os.makedirs(d, exist_ok=True)
    P.assemble_target(unit, sym, d)
    prefix, flags, src_rel, _ = P.compiler_split(unit)
    src_path = os.path.abspath(src) if src else src_rel
    flags_text = ' '.join(flags)
    text = clean_for_parser(preprocess(prefix, flags, src_path))
    if is_method:
        cls, meth = parts[-2], parts[-1]
        if meth == cls or meth.startswith('~') or '<' in qual:
            sys.exit('unsupported: constructor/destructor/template (%s)' % (dm or sym))
        base = setup_method(text, cls, meth, sym)
        func_name, compile_fn = 'permX', sym
        info = 'method %s::%s -> surrogate permX' % (cls, meth)
    else:
        cname = parts[-1] if dm else sym
        base = setup_c(text, cname, flags_text)
        func_name, compile_fn = cname, cname
        info = 'function %s' % cname
    write_compile_files(d, prefix, flags, compile_fn)
    with open(os.path.join(d, 'base.c'), 'w', encoding='utf-8') as f:
        f.write(base)
    with open(os.path.join(d, 'settings.toml'), 'w') as f:
        f.write('func_name = "%s"\ncompiler_type = "mwcc"\n' % func_name)
        f.write(P.weights_section(regs, dirty))
    json.dump({'unit': unit, 'symbol': sym, 'src': src_path, 'method': is_method, 'func_name': func_name,
               'src_name': (parts[-2] + '::' + parts[-1]) if is_method else func_name},
              open(os.path.join(d, 'perm_meta.json'), 'w'))
    print('ready: %s (%s)' % (d, info))
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('unit')
    ap.add_argument('symbol')
    ap.add_argument('--src')
    ap.add_argument('--regs', action='store_true')
    ap.add_argument('--dirty', action='store_true')
    ap.add_argument('--suffix', default='')
    ap.add_argument('--mode', choices=['auto', 'c', 'cpp'], default='auto')
    ap.add_argument('--runs', default=os.environ.get('DECOMP_PERM_RUNS'))
    ap.add_argument('--permuter', default=os.environ.get('DECOMP_PERMUTER'), help='unused here; accepted for symmetry')
    a = ap.parse_args()
    setup(a.unit, a.symbol, a.src, a.regs, a.dirty, a.suffix, a.mode, a.runs)


if __name__ == '__main__':
    main()
