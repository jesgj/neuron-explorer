import re, sys, os, secrets, urllib.parse, concurrent.futures as cf, urllib.request
BASE = "https://already-clean.goodfire.com/"
OUT = sys.argv[1]
TEXT_EXT = ('.html','.css','.js','.mjs','.json','.md','.svg','.txt')
ref_re = re.compile(r'''(?:src|href|data-src|poster|srcset)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)["']?\s*\)|["'`]((?:\.{0,2}/)?(?:assets|data|microscope|img|images|media)/[^"'`\s<>]+)["'`]|["'`]([\w./-]+\.(?:js|mjs|css|json|png|jpe?g|webp|gif|svg|mp4|webm|md|bin|npy|txt))["'`]''')
seen, fails = set(), []
def allowed(u):
    p = urllib.parse.urlparse(u)
    base = urllib.parse.urlparse(BASE)
    return p.scheme == base.scheme and p.netloc == base.netloc

class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            fp.close()
            raise ValueError('redirect outside base origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = urllib.request.build_opener(SameHostRedirect())
def norm(ref, parent):
    ref = ref.strip().split('#')[0]
    if not ref or ref.startswith(('data:','mailto:','javascript:','blob:')) or '${' in ref or '{' in ref: return None
    u = urllib.parse.urljoin(parent, ref)
    p = urllib.parse.urlparse(u)
    if not allowed(u): return None
    return urllib.parse.urlunparse((p.scheme,p.netloc,p.path,'','',''))
def local(u):
    path = urllib.parse.urlparse(u).path.lstrip('/') or 'index.html'
    if path.endswith('/'): path += 'index.html'
    path = urllib.parse.unquote(path)
    if (os.path.isabs(path) or '\\' in path or '\x00' in path or
            any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('unsafe URL path')
    return os.path.join(OUT, path)

def local_parent(lp):
    """Open the parent directory beneath OUT without following symlinks."""
    os.makedirs(OUT, exist_ok=True)
    parts = os.path.relpath(lp, OUT).split(os.sep)
    fd = os.open(OUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise

def open_local(lp, flags):
    fd, name = local_parent(lp)
    try:
        return os.open(name, flags | os.O_NOFOLLOW, 0o666, dir_fd=fd)
    finally:
        os.close(fd)

def publish_local(lp, data):
    """Publish a complete file without replacing an existing destination."""
    parent, name = local_parent(lp)
    temp = None
    try:
        while True:
            candidate = '.mirror-' + secrets.token_hex(16)
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o666, dir_fd=parent)
                temp = candidate
                break
            except FileExistsError:
                continue
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        try:
            os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        try:
            if temp is not None:
                os.unlink(temp, dir_fd=parent)
        finally:
            os.close(parent)
def fetch(u):
    try:
        if not allowed(u):
            raise ValueError('URL outside base origin')
        lp = local(u)
        try:
            fd = open_local(lp, os.O_RDONLY)
        except FileNotFoundError:
            req = urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
            with opener.open(req, timeout=60) as response:
                data = response.read()
            publish_local(lp, data)
            fd = open_local(lp, os.O_RDONLY)
        with os.fdopen(fd, 'rb') as source:
            data = source.read()
        if lp.endswith(TEXT_EXT) or lp.endswith('index.html'):
            txt = data.decode('utf-8', errors='ignore')
            refs = set()
            for m in ref_re.finditer(txt):
                for g in m.groups():
                    if not g: continue
                    for part in g.split(','):  # srcset
                        for par in ((u, BASE) if lp.endswith(('.js','.mjs','.json')) else (u,)):
                            r = norm(part.strip().split(' ')[0], par)
                            if r: refs.add(r)
            return u, refs, None
        return u, set(), None
    except Exception as e:
        return u, set(), str(e)
frontier = {BASE}
with cf.ThreadPoolExecutor(24) as ex:
    while frontier:
        seen |= frontier
        nxt = set()
        for u, refs, err in ex.map(fetch, frontier):
            if err: fails.append((u, err))
            nxt |= refs - seen
        print(f"fetched {len(frontier)}, total {len(seen)}, next {len(nxt)}, fails {len(fails)}", flush=True)
        frontier = nxt
root_fd = os.open(OUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    try:
        os.unlink('_fails.txt', dir_fd=root_fd)
    except FileNotFoundError:
        pass
    fd = os.open('_fails.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=root_fd)
    with os.fdopen(fd, 'w') as report:
        report.write('\n'.join(f"{u}\t{e}" for u,e in fails))
finally:
    os.close(root_fd)
