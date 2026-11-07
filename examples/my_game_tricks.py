"""my_game_tricks.py - example trick plug-in: teach tricksweep YOUR game's spellings.

Load it with
    python tools/search/tricksweep.py UNIT FUNC --plugins examples/my_game_tricks.py
or for every run (also inside the runner):
    SEARCH_TRICK_PLUGINS=examples/my_game_tricks.py      (several files: separate like PATH)

A trick gets the text between the braces of ONE function and yields (description, new_body) once per place it can
apply. It must keep every value the same: only the SHAPE the compiler sees may change. Copy this file, delete the
examples you don't need, and fill the tables with your engine's names (see guide/11-customizing-for-your-game.md).
"""
import re

# ---------------------------------------------------------------------------------------------------------------
# 1. Same value, other spelling: your engine's inline accessors / macros.
#    The original programmers often read a field through a tiny inline function or macro. To the compiler that is
#    NOT the same as `obj->field`: the inline's parameter is a new value, which changes register allocation.
#    List the pairs that are really identical in YOUR headers (check the inline's body!).
SPELLINGS = [
    # (name, description, regex on the function body, replacement)
    ('t_acc_model', 'read obj->model through the engine accessor GetModel(obj)',
     r'\b(\w+)->model\b(?!\s*=[^=])', r'GetModel(\1)'),
    ('t_acc_model_back', 'GetModel(obj) -> obj->model (the inverse)',
     r'\bGetModel\((\w+)\)', r'\1->model'),
    # SDK conversion helpers that compute the same value as a cast (only if that is true for your SDK!)
    ('t_sdk_f2u16', '(u16)x -> F32ToU16(x)  (the SDK inline, a paired-single store in the original)',
     r'\(u16\)\s*(\w+(?:->\w+|\.\w+)*)', r'F32ToU16(\1)'),
]


def _spelling_trick(pattern, repl):
    rx = re.compile(pattern)

    def fn(body):
        for m in rx.finditer(body):
            new = body[:m.start()] + m.expand(repl) + body[m.end():]
            yield 'at %d: %s -> %s' % (m.start(), m.group(0), m.expand(repl)), new
    return fn


# ---------------------------------------------------------------------------------------------------------------
# 2. A shape your project keeps meeting. Example: the original wrote `if (x != NULL)` where the draft has `if (x)`.
#    (Identical value; some compilers emit the compare differently for the two forms.)
def t_explicit_null(body):
    """`if (p)` -> `if (p != NULL)` for pointer-looking names"""
    for m in re.finditer(r'\bif \((\w*(?:ptr|Ptr|p|obj|work|data))\)', body):
        yield 'explicit NULL test of %s' % m.group(1), body[:m.start()] + 'if (%s != NULL)' % m.group(1) + body[m.end():]


TRICKS = [(n, d, _spelling_trick(p, r)) for n, d, p, r in SPELLINGS] + [
    ('t_explicit_null', t_explicit_null.__doc__.strip(), t_explicit_null),
]
