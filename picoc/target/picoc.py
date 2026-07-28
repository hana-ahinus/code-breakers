#!/usr/bin/env python3
"""A small, self-contained interpreter for the PicoC language subset.

It intentionally uses no C parser or compiler: source is tokenised, parsed and
evaluated here.  The implementation focuses on the C used by PicoC's command
line programs (integer arithmetic, arrays, pointers, functions and stdio).
"""
import math
import re
import struct
import sys


class Cell:
    def __init__(self, value=0, typ="int"):
        self.value, self.typ = value, typ


class Array:
    def __init__(self, n=0, typ="int"):
        self.items = [Cell(default_value(typ), typ) for _ in range(n)]
        self.typ = typ


class Struct:
    def __init__(self, fields):
        self.fields = {name: Cell(default_value(typ), typ) for name, typ in fields}


class UnionByteCell(Cell):
    def __init__(self, buf, offset, typ):
        self.typ = typ
        self.buf = buf
        self.offset = offset

    @property
    def value(self):
        bt = base_type(self.typ)
        if bt in ('char', 'signed char'):
            b = self.buf[self.offset]
            return b - 256 if b >= 128 else b
        if bt == 'unsigned char':
            return self.buf[self.offset]
        if bt in ('short', 'signed short'):
            return struct.unpack_from('<h', self.buf, self.offset)[0]
        if bt == 'unsigned short':
            return struct.unpack_from('<H', self.buf, self.offset)[0]
        if bt in ('int', 'signed int', 'signed', 'long', 'signed long'):
            return struct.unpack_from('<i', self.buf, self.offset)[0]
        if bt in ('unsigned int', 'unsigned', 'unsigned long'):
            return struct.unpack_from('<I', self.buf, self.offset)[0]
        if bt == 'float':
            return struct.unpack_from('<f', self.buf, self.offset)[0]
        if bt == 'double':
            return struct.unpack_from('<d', self.buf, self.offset)[0]
        return self.buf[self.offset]

    @value.setter
    def value(self, val):
        bt = base_type(self.typ)
        if bt in ('char', 'signed char', 'unsigned char'):
            self.buf[self.offset] = int(val) & 0xFF
        elif bt in ('short', 'signed short'):
            struct.pack_into('<h', self.buf, self.offset, int(val))
        elif bt == 'unsigned short':
            struct.pack_into('<H', self.buf, self.offset, int(val) & 0xFFFF)
        elif bt in ('int', 'signed int', 'signed', 'long', 'signed long'):
            struct.pack_into('<i', self.buf, self.offset, int(val))
        elif bt in ('unsigned int', 'unsigned', 'unsigned long'):
            struct.pack_into('<I', self.buf, self.offset, int(val) & 0xFFFFFFFF)
        elif bt == 'float':
            struct.pack_into('<f', self.buf, self.offset, float(val))
        elif bt == 'double':
            struct.pack_into('<d', self.buf, self.offset, float(val))
        elif isinstance(val, (int, float)):
            struct.pack_into('<i', self.buf, self.offset, int(val))


class UnionArray:
    def __init__(self, buf, offset, n, elem_typ):
        elem_sz = type_size(elem_typ)
        self.items = [UnionByteCell(buf, offset + i * elem_sz, elem_typ) for i in range(n)]
        self.typ = elem_typ


class Union:
    def __init__(self, fields, size=0):
        if not size:
            size = max([type_size(t) for _, t in fields] or [4])
        self.buf = bytearray(size)
        self.fields = {}
        for name, typ in fields:
            if typ.endswith(']'):
                m = re.match(r'^(.*?)\[(\d*)\](.*)$', typ)
                n = int(m.group(2) or 0)
                elem_typ = m.group(1) + m.group(3)
                arr = UnionArray(self.buf, 0, n, elem_typ)
                self.fields[name] = Cell(arr, typ)
            else:
                self.fields[name] = UnionByteCell(self.buf, 0, typ)


class Pointer:
    def __init__(self, target=None, index=0):
        self.target, self.index = target, index
    def add(self, n):
        return Pointer(self.target, self.index + int(n))
    def get(self):
        if isinstance(self.target, (Array, UnionArray)): return self.target.items[self.index]
        if isinstance(self.target, list): return self.target[self.index]
        return self.target


def base_type(typ):
    return typ.rstrip('*').strip()


def is_ptr(typ):
    return typ.endswith('*')


def default_value(typ):
    if typ.endswith(']'):
        # The first dimension is the outer C array dimension: int[2][3]
        # means two elements whose type is int[3].
        m = re.match(r'^(.*?)\[(\d*)\](.*)$', typ)
        return Array(int(m.group(2) or 0), m.group(1) + m.group(3))
    if is_ptr(typ): return Pointer()
    if typ.startswith('union '): return Union(TYPE_FIELDS.get(typ, []), type_size(typ))
    if typ.startswith('struct '): return Struct(TYPE_FIELDS.get(typ, []))
    return 0.0 if base_type(typ) in ('float', 'double') else 0


TYPE_FIELDS = {}
TYPEDEFS = {}
TYPE_SIZES = {'char': 1, 'signed char': 1, 'unsigned char': 1, 'short': 2,
              'unsigned short': 2, 'int': 4, 'unsigned': 4, 'unsigned int': 4,
              'long': 8, 'unsigned long': 8, 'float': 4, 'double': 8, 'void': 1}


def type_size(typ):
    typ = TYPEDEFS.get(typ, typ)
    if typ.endswith(']'):
        m = re.match(r'^(.*?)\[(\d+)\](.*)$', typ)
        return int(m.group(2)) * type_size(m.group(1) + m.group(3))
    if is_ptr(typ): return 8
    if typ.startswith('union '): return max([type_size(t) for _, t in TYPE_FIELDS.get(typ, [])] or [0])
    if typ.startswith('struct '):
        offset = 0; align = 1
        for _, t in TYPE_FIELDS.get(typ, []):
            a = min(type_size(t), 8); align = max(align, a)
            offset = (offset + a - 1) // a * a + type_size(t)
        return (offset + align - 1) // align * align
    return TYPE_SIZES.get(typ, 4)


# Preprocessing is deliberately small but follows the directives accepted by PicoC.
def preprocess(src):
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'//[^\n]*', '', src)
    macros, out, enabled, stack = {}, [], True, []
    for raw in src.splitlines():
        line = raw.strip()
        if line.startswith('#'):
            parts = line[1:].strip().split(None, 2); op = parts[0] if parts else ''
            arg = parts[1] if len(parts) > 1 else ''
            if op == 'define' and enabled and len(parts) > 2:
                macros[arg] = parts[2]
            elif op in ('ifdef', 'ifndef', 'if'):
                prior = enabled
                if op == 'ifdef': yes = arg in macros
                elif op == 'ifndef': yes = arg not in macros
                else:
                    expr = ' '.join(parts[1:])
                    expr = re.sub(r'defined\s*\((\w+)\)', lambda m: '1' if m.group(1) in macros else '0', expr)
                    for k, v in macros.items(): expr = re.sub(r'\b' + re.escape(k) + r'\b', v, expr)
                    try: yes = bool(eval(expr.replace('&&',' and ').replace('||',' or ').replace('!',' not '), {'__builtins__': {}}, {}))
                    except Exception: yes = False
                stack.append((prior, yes)); enabled = prior and yes
            elif op == 'else' and stack:
                prior, yes = stack[-1]; stack[-1] = (prior, not yes); enabled = prior and not yes
            elif op == 'endif' and stack:
                prior, _ = stack.pop(); enabled = prior
            continue
        if enabled:
            for _ in range(8):
                before = raw
                # C macro identifiers are not expanded inside character or
                # string literals.
                chunks = re.split(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', raw)
                for pos in range(0, len(chunks), 2):
                    for k, v in macros.items():
                        chunks[pos] = re.sub(r'\b' + re.escape(k) + r'\b', v, chunks[pos])
                raw = ''.join(chunks)
                if raw == before: break
            out.append(raw)
    return '\n'.join(out)


TOKEN_RE = re.compile(r'''\s*(?:(0[xX][0-9a-fA-F]+|\d+\.\d*(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+)([uUlLfF]*)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|([A-Za-z_]\w*)|(>>=|<<=|\+\+|--|->|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|%=|<<|>>|&=|\|=|\^=|\.\.\.|.))''', re.S)


def tokens(src):
    ans = []
    for m in TOKEN_RE.finditer(src):
        num, suffix, quoted, name, op = m.groups()
        if num is not None:
            ans.append(('num', num + suffix))
        elif quoted is not None: ans.append(('str', quoted))
        elif name is not None: ans.append(('id', name))
        elif op and not op.isspace(): ans.append(('op', op))
    ans.append(('eof', 'eof')); return ans


TYPE_WORDS = {'void','char','short','int','long','float','double','signed','unsigned','struct','union','enum','const','volatile','static','extern','auto','register'}


class Parser:
    def __init__(self, src): self.ts, self.i = tokens(preprocess(src)), 0
    def text(self): return self.ts[self.i][1]
    def kind(self): return self.ts[self.i][0]
    def pop(self, val=None):
        t = self.ts[self.i]
        if val is not None and t[1] != val: raise SyntaxError('expected ' + val + ', got ' + t[1])
        self.i += 1; return t
    def accept(self, val):
        if self.text() == val: self.i += 1; return True
        return False
    def is_type(self): return self.text() in TYPE_WORDS or self.text() in TYPEDEFS
    def typ(self):
        words = []
        if self.text() in ('struct','union'):
            tagkind = self.pop()[1]; tag = self.pop()[1] if self.kind() == 'id' else ''
            typ = tagkind + ' ' + tag
            if self.accept('{'):
                fields = []
                while not self.accept('}'):
                    ft = self.typ()
                    while True:
                        name, dt = self.declarator(ft)
                        fields.append((name, dt))
                        if not self.accept(','): break
                    self.pop(';')
                TYPE_FIELDS[typ] = fields
            return typ
        while self.text() in TYPE_WORDS or self.text() in TYPEDEFS:
            w = self.pop()[1]
            if w not in ('const','volatile','static','extern','auto','register'): words.append(w)
            if w in ('void','char','short','int','long','float','double','enum'): break
        t = ' '.join(words) or 'int'
        return TYPEDEFS.get(t, t)
    def declarator(self, typ):
        while self.accept('*'): typ += '*'
        name = self.pop()[1]
        while self.accept('['):
            n = self.expr() if self.text() != ']' else ('num','0')
            self.pop(']'); typ += '[' + str(n[1] if n[0] == 'num' else 0) + ']'
        return name, typ
    def program(self):
        global TYPEDEFS, TYPE_FIELDS
        TYPEDEFS, TYPE_FIELDS = {}, {}
        globals_, funcs = [], {}
        while self.kind() != 'eof':
            if self.accept('typedef'):
                t = self.typ(); name, full = self.declarator(t); TYPEDEFS[name] = full; self.pop(';'); continue
            t = self.typ()
            # A tag definition such as `struct S { ... };` is a complete
            # declaration and has no declarator following it.
            if self.accept(';'): continue
            name, full = self.declarator(t)
            if self.accept('('):
                args = []
                if not self.accept(')'):
                    while True:
                        if self.text() == '...': self.pop(); args.append(('__varargs', '...')); break
                        at = self.typ(); an, aft = self.declarator(at); args.append((an, aft))
                        if self.accept(')'): break
                        self.pop(',')
                if self.accept(';'): continue
                funcs[name] = (full, args, self.stmt())
            else:
                init = None
                if self.accept('='): init = self.initializer()
                globals_.append((name, full, init))
                while self.accept(','):
                    n, ft = self.declarator(t); iv = self.initializer() if self.accept('=') else None; globals_.append((n, ft, iv))
                self.pop(';')
        return globals_, funcs
    def initializer(self):
        if self.accept('{'):
            x=[]
            if not self.accept('}'):
                while True:
                    x.append(self.initializer())
                    if self.accept('}'): break
                    self.pop(',')
                    if self.accept('}'): break
            return ('init', x)
        return self.expr()
    def stmt(self):
        if self.accept('{'):
            a=[]
            while not self.accept('}'): a.append(self.stmt())
            return ('block', a)
        if self.accept(';'): return ('empty',)
        if self.accept('if'):
            self.pop('('); c=self.expr(); self.pop(')'); yes=self.stmt(); no=self.stmt() if self.accept('else') else None; return ('if',c,yes,no)
        if self.accept('while'):
            self.pop('('); c=self.expr(); self.pop(')'); return ('while',c,self.stmt())
        if self.accept('do'):
            b=self.stmt(); self.pop('while'); self.pop('('); c=self.expr(); self.pop(')'); self.pop(';'); return ('do',b,c)
        if self.accept('for'):
            self.pop('(')
            if self.is_type(): init = self.declaration(True)
            elif self.accept(';'): init = ('empty',)
            else: init = self.expr(); self.pop(';'); init = ('expr', init)
            cond = None if self.accept(';') else self.expr(); self.pop(';') if cond is not None else None
            inc = None if self.accept(')') else self.expr(); self.pop(')'); return ('for',init,cond,inc,self.stmt())
        if self.accept('switch'):
            self.pop('('); e=self.expr(); self.pop(')'); return ('switch',e,self.stmt())
        for k in ('return','break','continue'):
            if self.accept(k):
                e = None if self.accept(';') else self.expr(); self.pop(';') if e is not None else None; return (k,e)
        if self.accept('goto'):
            n=self.pop()[1]; self.pop(';'); return ('goto',n)
        if self.accept('case'):
            e=self.expr(); self.pop(':'); return ('case',e)
        if self.accept('default'):
            self.pop(':'); return ('default',)
        if self.is_type(): return self.declaration(True)
        if self.kind() == 'id' and self.ts[self.i+1][1] == ':':
            n=self.pop()[1]; self.pop(':'); return ('label',n)
        return self.expr_stmt()
    def declaration(self, needsemi=True):
        static = self.text() == 'static'; t = self.typ(); vals=[]
        while True:
            n, ft = self.declarator(t); v = self.initializer() if self.accept('=') else None; vals.append((n,ft,v,static))
            if not self.accept(','): break
        if needsemi: self.pop(';')
        return ('decl', vals)
    def expr_stmt(self):
        if self.accept(';'): return ('empty',)
        e=self.expr(); self.pop(';'); return ('expr',e)
    PRE = {'=':1,'+=':1,'-=':1,'*=':1,'/=':1,'%=':1,'<<=':1,'>>=':1,'&=':1,'|=':1,'^=':1,
           '?':2,'||':3,'&&':4,'|':5,'^':6,'&':7,'==':8,'!=':8,'<':9,'>':9,'<=':9,'>=':9,
           '<<':10,'>>':10,'+':11,'-':11,'*':12,'/':12,'%':12}
    def expr(self, minp=1):
        left = self.unary()
        while self.text() in self.PRE and self.PRE[self.text()] >= minp:
            op=self.pop()[1]; p=self.PRE[op]
            if op == '?':
                yes=self.expr(); self.pop(':'); no=self.expr(p); left=('?:',left,yes,no); continue
            right=self.expr(p if op.endswith('=') else p+1); left=(op,left,right)
        return left
    def unary(self):
        if self.text() in ('+','-','!','~','*','&','++','--'):
            return ('u'+self.pop()[1], self.unary())
        if self.accept('sizeof'):
            if self.accept('(') and self.is_type():
                t=self.typ()
                while self.accept('*'): t += '*'
                self.pop(')'); return ('sizeof_t',t)
            if self.ts[self.i-1][1] == '(': e=self.expr(); self.pop(')'); return ('sizeof',e)
            return ('sizeof',self.unary())
        if self.accept('('):
            # C casts begin with a type name.
            if self.is_type():
                t=self.typ()
                while self.accept('*'): t += '*'
                self.pop(')'); return ('cast',t,self.unary())
            e=self.expr(); self.pop(')'); return self.postfix(e)
        if self.kind() == 'num':
            s=self.pop()[1]; return ('num',s)
        if self.kind() == 'str': return ('str',self.pop()[1])
        return self.postfix(('var',self.pop()[1]))
    def postfix(self, e):
        while True:
            if self.accept('('):
                args=[]
                if not self.accept(')'):
                    while True:
                        args.append(self.expr())
                        if self.accept(')'): break
                        self.pop(',')
                e=('call',e,args)
            elif self.accept('['): x=self.expr(); self.pop(']'); e=('index',e,x)
            elif self.accept('.') or self.accept('->'):
                e=('field',e,self.pop()[1])
            elif self.text() in ('++','--'): e=('post'+self.pop()[1],e)
            else: break
        return e


class Signal(Exception):
    def __init__(self, kind, value=None): self.kind,self.value=kind,value


class Env:
    def __init__(self, parent=None): self.parent,self.data=parent,{}
    def get(self,n): return self.data[n] if n in self.data else self.parent.get(n)
    def put(self,n,c): self.data[n]=c


class Runtime:
    def __init__(self, tree):
        self.globals, self.funcs = tree; self.globalenv=Env(); self.output=[]; self.static={}; self.strings=[]
        self.globalenv.put('NULL', Cell(Pointer(), 'void*'))
        for n,t,i in self.globals: self.declare(self.globalenv,n,t,i)
    def declare(self, env,n,t,init, static=False):
        key=n if not static else ('static',n)
        if static and key in self.static: env.put(n,self.static[key]); return
        if t.endswith('[0]') and init is not None and init[0] == 'init':
            t = t[:-3] + '[' + str(len(init[1])) + ']'
        c=Cell(default_value(t),t); env.put(n,c)
        if static: self.static[key]=c
        if init is not None: self.init_cell(c,init,env)
    def init_cell(self,c,node,env):
        if node[0] == 'init':
            if isinstance(c.value, Array):
                for i,x in enumerate(node[1]):
                    if i < len(c.value.items): self.init_cell(c.value.items[i],x,env)
            elif isinstance(c.value, Struct):
                for (_, f),x in zip(TYPE_FIELDS.get(c.typ, []), node[1]): self.init_cell(c.value.fields[f],x,env)
            return
        c.value=self.convert(self.value(self.eval(node,env)),c.typ)
    def value(self,x):
        if isinstance(x,Cell):
            if isinstance(x.value,(Array, UnionArray)): return Pointer(x.value)
            return x.value
        return x
    def lval(self,node,env):
        x=self.eval(node,env,True)
        if not isinstance(x,Cell): raise RuntimeError('not an lvalue')
        return x
    def convert(self,v,typ):
        if is_ptr(typ):
            if isinstance(v, Pointer):
                # malloc provides untyped storage. A pointer cast supplies its
                # element type, allowing int arrays to retain full values.
                if isinstance(v.target, (Array, UnionArray)) and v.target.typ == 'char':
                    elem = base_type(typ)
                    v.target.typ = elem
                    for cell in v.target.items: cell.typ = elem
                return v
            return Pointer() if not v else v
        if isinstance(v,Pointer): return 0
        bt = base_type(typ)
        if bt in ('float','double'): return float(v)
        if bt in ('char', 'signed char'):
            x = int(v) & 0xFF
            return x - 256 if x >= 128 else x
        if bt == 'unsigned char':
            return int(v) & 0xFF
        if bt in ('short', 'signed short'):
            x = int(v) & 0xFFFF
            return x - 65536 if x >= 32768 else x
        if bt == 'unsigned short':
            return int(v) & 0xFFFF
        if bt in ('int', 'signed int', 'signed'):
            x = int(v) & 0xFFFFFFFF
            return x - 0x100000000 if x >= 0x80000000 else x
        if bt.startswith('unsigned'):
            return int(v) & ((1 << (8*type_size(typ))) - 1)
        return int(v) if isinstance(v,(int,float,bool)) else v
    def eval(self,n,env,want_lval=False):
        k=n[0]
        if k=='num':
            s=n[1].rstrip('uUlLfF'); return float(s) if any(x in s for x in '.eE') else int(s,0)
        if k=='str':
            if n[1].startswith("'"):
                try: return ord(bytes(n[1][1:-1], 'utf8').decode('unicode_escape')[0])
                except Exception: return 0
            try: s=bytes(n[1][1:-1],'utf8').decode('unicode_escape')
            except Exception: s=n[1][1:-1]
            a=Array(len(s)+1,'char'); [setattr(a.items[i],'value',ord(ch)) for i,ch in enumerate(s)]; self.strings.append(a); return Pointer(a)
        if k=='var': return env.get(n[1])
        if k=='cast': return self.convert(self.value(self.eval(n[2],env)),n[1])
        if k=='sizeof_t': return type_size(n[1])
        if k=='sizeof':
            x=self.eval(n[1],env,True); return type_size(x.typ) if isinstance(x,Cell) else 8
        if k in ('u+','u-','u!','u~'):
            a=self.value(self.eval(n[1],env)); return {'u+':lambda:+a,'u-':lambda:-a,'u!':lambda:int(not a),'u~':lambda:~int(a)}[k]()
        if k=='u&': return Pointer(self.lval(n[1],env))
        if k=='u*':
            p=self.value(self.eval(n[1],env)); return p.get() if isinstance(p,Pointer) else Cell()
        if k in ('u++','u--','post++','post--'):
            c=self.lval(n[1],env); old=self.value(c); c.value=self.binop('+' if k.endswith('++') else '-',old,1); return c if k[0]=='u' else old
        if k=='index':
            p=self.value(self.eval(n[1],env)); idx=self.value(self.eval(n[2],env));
            if isinstance(p,Pointer): return p.add(idx).get()
            if isinstance(p,(Array, UnionArray)): return p.items[int(idx)]
        if k=='field':
            obj=self.value(self.eval(n[1],env));
            if isinstance(obj,Pointer): obj=obj.get().value
            return obj.fields[n[2]]
        if k=='call': return self.call(n[1],n[2],env)
        if k=='?:': return self.eval(n[2] if self.value(self.eval(n[1],env)) else n[3],env)
        if k.endswith('=') and k not in ('==','!=','>=','<='):
            c=self.lval(n[1],env); v=self.value(self.eval(n[2],env)); c.value=self.convert(v if k=='=' else self.binop(k[:-1],self.value(c),v),c.typ); return c
        if k in Parser.PRE:
            if k=='&&':
                a=self.value(self.eval(n[1],env)); return int(bool(a) and bool(self.value(self.eval(n[2],env))))
            if k=='||':
                a=self.value(self.eval(n[1],env)); return int(bool(a) or bool(self.value(self.eval(n[2],env))))
            return self.binop(k,self.value(self.eval(n[1],env)),self.value(self.eval(n[2],env)))
        raise RuntimeError('unknown expression '+k)
    def binop(self,o,a,b):
        if isinstance(a,Pointer) and o in ('+','-'): return a.add(b if o=='+' else -b)
        if isinstance(b,Pointer) and o=='+': return b.add(a)
        if isinstance(a,Pointer) or isinstance(b,Pointer):
            if o in ('==','!='): return int((a.target,a.index)==(b.target,b.index)) if o=='==' else int((a.target,a.index)!=(b.target,b.index))
            if o=='-': return a.index-b.index
            return 0
        ops={'+':lambda:a+b,'-':lambda:a-b,'*':lambda:a*b,'/':lambda:int(a/b) if not isinstance(a,float) and not isinstance(b,float) else a/b,'%':lambda:a% b,
             '<<':lambda:int(a)<<int(b),'>>':lambda:int(a)>>int(b),'&':lambda:int(a)&int(b),'|':lambda:int(a)|int(b),'^':lambda:int(a)^int(b),
             '==':lambda:int(a==b),'!=':lambda:int(a!=b),'<':lambda:int(a<b),'>':lambda:int(a>b),'<=':lambda:int(a<=b),'>=':lambda:int(a>=b)}
        return ops[o]()
    def call(self,fn,args,env):
        name=fn[1] if fn[0]=='var' else ''
        vals=[self.value(self.eval(x,env)) for x in args]
        if name in ('printf','fprintf','sprintf','snprintf'):
            start=1 if name=='printf' else 2; fmt=self.cstr(vals[start-1] if name!='printf' else vals[0]); text=self.format(fmt,vals[start:] if name!='printf' else vals[1:])
            if name=='printf': self.output.append(text)
            elif name in ('sprintf','snprintf'):
                dst=vals[0]; limit=int(vals[1]) if name=='snprintf' else 10**9
                for i,ch in enumerate(text[:max(0,limit-1)]): dst.add(i).get().value=ord(ch)
                dst.add(min(len(text),max(0,limit-1))).get().value=0
            return len(text)
        if name in ('puts','putchar'):
            text=(self.cstr(vals[0])+'\n') if name=='puts' else chr(int(vals[0])&255); self.output.append(text); return len(text)
        if name in ('malloc','calloc','realloc'):
            size=int(vals[0]) * (int(vals[1]) if name=='calloc' else 1); return Pointer(Array(size,'char'))
        if name=='free': return 0
        if name in ('abs','labs'): return abs(int(vals[0]))
        if name in ('atoi','atol'): return int(self.cstr(vals[0]) or 0)
        if name in ('strlen',): return len(self.cstr(vals[0]))
        if name in ('strcmp','strncmp'):
            a,b=self.cstr(vals[0]),self.cstr(vals[1]);
            if name=='strncmp': a,b=a[:int(vals[2])],b[:int(vals[2])]
            return (a>b)-(a<b)
        if name in ('sqrt','pow','sin','cos','tan','floor','ceil'):
            return getattr(math,name)(*vals)
        if name=='exit': raise Signal('return', vals[0] if vals else 0)
        if name not in self.funcs: return 0
        _, params, body=self.funcs[name]; local=Env(self.globalenv)
        for i,(pn,pt) in enumerate(params):
            if pn != '__varargs':
                # Array parameters are pointers in C.
                if '[' in pt: pt = pt.split('[', 1)[0] + '*'
                local.put(pn,Cell(self.convert(vals[i] if i<len(vals) else 0,pt),pt))
        try: self.exec(body,local)
        except Signal as s:
            if s.kind=='return': return s.value or 0
            raise
        return 0
    def cstr(self,p):
        if not isinstance(p,Pointer): return str(p)
        out=[]
        try:
            while p.get().value: out.append(chr(int(p.get().value)&255)); p=p.add(1)
        except Exception: pass
        return ''.join(out)
    def format(self,fmt,args):
        out=[]; i=0; ai=0
        while i<len(fmt):
            if fmt[i]!='%': out.append(fmt[i]); i+=1; continue
            if i+1<len(fmt) and fmt[i+1]=='%': out.append('%'); i+=2; continue
            m=re.match(r'%(?:[-+ #0]*)(?:\d+)?(?:\.\d+)?[hlL]*([diuoxXfFeEgGcsp])',fmt[i:])
            if not m: out.append('%'); i+=1; continue
            spec=m.group(1); raw=m.group(0); v=args[ai] if ai<len(args) else 0; ai+=1
            if spec=='s': z=self.cstr(v)
            elif spec=='c': z=chr(int(v)&255)
            elif spec=='p': z='0x%x' % (id(v.target) if isinstance(v,Pointer) and v.target else 0)
            elif spec in 'fFeEgG': z=(raw[:-1] + spec) % float(v)
            else:
                iv = int(v)
                if spec in 'uoxX': iv = iv & 0xFFFFFFFF
                py={'d':'d','i':'d','u':'d','o':'o','x':'x','X':'X'}[spec]; z=(raw[:-1]+py) % iv
            out.append(z); i+=len(raw)
        return ''.join(out)
    def exec(self,s,env):
        k=s[0]
        if k=='block': return self.exec_block(s[1],env)
        if k=='empty': return
        if k=='expr': self.eval(s[1],env); return
        if k=='decl':
            for n,t,i,static in s[1]: self.declare(env,n,t,i,static)
        elif k=='if':
            if self.value(self.eval(s[1],env)): self.exec(s[2],env)
            elif s[3]: self.exec(s[3],env)
        elif k=='while':
            while self.value(self.eval(s[1],env)):
                try:self.exec(s[2],env)
                except Signal as x:
                    if x.kind=='break':break
                    if x.kind!='continue':raise
        elif k=='do':
            while True:
                try:self.exec(s[1],env)
                except Signal as x:
                    if x.kind=='break':break
                    if x.kind!='continue':raise
                if not self.value(self.eval(s[2],env)):break
        elif k=='for':
            self.exec(s[1],env)
            while s[2] is None or self.value(self.eval(s[2],env)):
                try:self.exec(s[4],env)
                except Signal as x:
                    if x.kind=='break':break
                    if x.kind!='continue':raise
                if s[3] is not None:self.eval(s[3],env)
        elif k=='switch': self.exec_switch(s[2],self.value(self.eval(s[1],env)),env)
        elif k == 'return':
            raise Signal('return', self.value(self.eval(s[1], env)) if s[1] is not None else 0)
        elif k in ('break','continue','goto'):
            raise Signal(k,s[1] if len(s)>1 else None)
    def exec_block(self,items,env):
        labels={x[1]:i for i,x in enumerate(items) if x[0]=='label'}; pc=0
        while pc<len(items):
            try:self.exec(items[pc],env)
            except Signal as x:
                if x.kind=='goto' and x.value in labels: pc=labels[x.value]; continue
                raise
            pc+=1
    def exec_switch(self,body,val,env):
        items=body[1] if body[0]=='block' else [body]; start=None; default=None
        for i,x in enumerate(items):
            if x[0]=='case' and self.value(self.eval(x[1],env))==val: start=i; break
            if x[0]=='default': default=i
        for x in items[start if start is not None else default if default is not None else len(items):]:
            try:
                if x[0] not in ('case','default'): self.exec(x,env)
            except Signal as z:
                if z.kind=='break': return
                raise
    def run(self):
        return self.call(('var','main'),[],self.globalenv)


def main():
    if len(sys.argv)<2: return 0
    try:
        with open(sys.argv[1], encoding='utf8') as f: tree=Parser(f.read()).program()
        r=Runtime(tree); r.run(); sys.stdout.write(''.join(r.output)); return 0
    except Exception as exc:
        # PicoC reports diagnostics to stderr.  Keeping stdout clean is important
        # for the command-line harness and mirrors its observable program output.
        print('picoc:', exc, file=sys.stderr); return 1


if __name__=='__main__': sys.exit(main())
