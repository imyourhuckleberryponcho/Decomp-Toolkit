"""unit_overlay.py - lets refport.py add and remove units without editing the unit list in your configure.py.

Copy this file into your project's tools/ folder (commit it with the project) and add two lines to configure.py, after
config.libs is complete:

    from tools.unit_overlay import apply_overlay
    apply_overlay(config, Object, NonMatching)

It reads config/<VER>/units_overlay.json, written by refport.py:
  {"units":   {"ref/<repo>/<path>.c": {"mw_version": "GC/1.2.5n", "cflags": [...]},   imported: its own compiler + flags
               "game/menu_80012340.c": {"like": "game/menu.c"}},                      the rest of a unit it was cut from
   "removed": ["game/old.c"]}                                                         units that no longer exist
Only data is read from that file. Without the file nothing changes.
"""
import json
from pathlib import Path


def apply_overlay(config, Object, status, path=None):
    p = Path(path) if path else Path('config') / config.version / 'units_overlay.json'
    if not p.exists():
        return
    data = json.loads(p.read_text(encoding='utf-8'))
    removed, units = set(data.get('removed', [])), data.get('units', {})
    home = {o.name: (lib, o) for lib in config.libs for o in lib['objects']}   # removed ones too: 'like' may name them
    for lib in config.libs:
        lib['objects'] = [o for o in lib['objects'] if o.name not in removed]

    def find(name, seen=()):
        if name in home:
            return home[name]
        like = units.get(name, {}).get('like')
        return find(like, seen + (name,)) if like and like not in seen else None

    own = []
    for name, opt in units.items():
        if name in home:
            continue
        if 'mw_version' in opt:
            own.append(Object(status, name, mw_version=opt['mw_version'], cflags=list(opt['cflags']),
                              shift_jis=opt.get('shift_jis', True)))
            continue
        h = find(opt.get('like', ''))
        if h is None:
            print('units_overlay.json: %s: no unit to copy the settings from' % name)
            continue
        lib, o = h
        new = Object(status, name)
        new.options.update({k: v for k, v in o.options.items() if k != 'source' and v is not None})
        lib['objects'].append(new)
    if own:
        lib = {'lib': 'imported', 'mw_version': config.linker_version, 'cflags': [], 'objects': own}
        cat = next((x.get('progress_category') for x in config.libs if x.get('progress_category')), None)
        if cat:
            lib['progress_category'] = cat
        config.libs.append(lib)
