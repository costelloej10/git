#!/usr/bin/env python
#
# Copyright (c) 2012 Felipe Contreras
#

# Inspired by Rocco Rutte's hg-fast-export

# Just copy to your ~/bin, or anywhere in your $PATH.
# Then you can clone with:
# git clone hg::/path/to/mercurial/repo/
#
# For remote repositories a local clone is stored in
# "$GIT_DIR/hg/origin/clone/.hg/".

from mercurial import hg, ui, bookmarks, context, encoding, node, error, extensions, discovery, util

import re
import sys
import os
import json
import shutil
import subprocess
import urllib
import atexit
import urlparse, hashlib
import time as ptime

#
# If you want to see Mercurial revisions as Git commit notes:
# git config core.notesRef refs/notes/hg
#
# If you are not in hg-git-compat mode and want to disable the tracking of
# named branches:
# git config --global remote-hg.track-branches false
#
# If you want the equivalent of hg's clone/pull--insecure option:
# git config --global remote-hg.insecure true
#
# If you want to switch to hg-git compatibility mode:
# git config --global remote-hg.hg-git-compat true
#
# git:
# Sensible defaults for git.
# hg bookmarks are exported as git branches, hg branches are prefixed
# with 'branches/', HEAD is a special case.
#
# hg:
# Emulate hg-git.
# Only hg bookmarks are exported as git branches.
# Commits are modified to preserve hg information and allow bidirectionality.
#

NAME_RE = re.compile('^([^<>]+)')
AUTHOR_RE = re.compile('^([^<>]+?)? ?[<>]([^<>]*)(?:$|>)')
EMAIL_RE = re.compile(r'([^ \t<>]+@[^ \t<>]+)')
AUTHOR_HG_RE = re.compile('^(.*?) ?<(.*?)(?:>(.+)?)?$')
RAW_AUTHOR_RE = re.compile('^(\w+) (?:(.+)? )?<(.*)> (\d+) ([+-]\d+)')

VERSION = 2

def die(msg, *args):
    sys.stderr.write('ERROR: %s\n' % (msg % args))
    sys.exit(1)

def warn(msg, *args):
    sys.stderr.write('WARNING: %s\n' % (msg % args))

def gitmode(flags):
    return 'l' in flags and '120000' or 'x' in flags and '100755' or '100644'

def gittz(tz):
    return '%+03d%02d' % (-tz / 3600, -tz % 3600 / 60)

def hgmode(mode):
    m = { '100755': 'x', '120000': 'l' }
    return m.get(mode, '')

def hghex(n):
    return node.hex(n)

def hgbin(n):
    return node.bin(n)

def hgref(ref):
    return ref.replace('___', ' ')

def gitref(ref):
    return ref.replace(' ', '___')

def check_version(*check):
    if not hg_version:
        return True
    return hg_version >= check

def get_config(config):
    cmd = ['git', 'config', '--get', config]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    output, _ = process.communicate()
    return output

def get_config_bool(config, default=False):
    value = get_config(config).rstrip('\n')
    if value == "true":
        return True
    elif value == "false":
        return False
    else:
        return default

class Marks:

    def __init__(self, path, repo):
        self.path = path
        self.repo = repo
        self.clear()
        self.load()

        if self.version < VERSION:
            if self.version == 1:
                self.upgrade_one()

            # upgraded?
            if self.version < VERSION:
                self.clear()
                self.version = VERSION

    def clear(self):
        self.tips = {}
        self.marks = {}
        self.rev_marks = {}
        self.last_mark = 0
        self.version = 0
        self.last_note = 0

    def load(self):
        if not os.path.exists(self.path):
            return

        tmp = json.load(open(self.path))

        self.tips = tmp['tips']
        self.marks = tmp['marks']
        self.last_mark = tmp['last-mark']
        self.version = tmp.get('version', 1)
        self.last_note = tmp.get('last-note', 0)

        for rev, mark in self.marks.iteritems():
            self.rev_marks[mark] = rev

    def upgrade_one(self):
        def get_id(rev):
            return hghex(self.repo.changelog.node(int(rev)))
        self.tips = dict((name, get_id(rev)) for name, rev in self.tips.iteritems())
        self.marks = dict((get_id(rev), mark) for rev, mark in self.marks.iteritems())
        self.rev_marks = dict((mark, get_id(rev)) for mark, rev in self.rev_marks.iteritems())
        self.version = 2

    def dict(self):
        return { 'tips': self.tips, 'marks': self.marks, 'last-mark' : self.last_mark, 'version' : self.version, 'last-note' : self.last_note }

    def store(self):
        json.dump(self.dict(), open(self.path, 'w'))

    def __str__(self):
        return str(self.dict())

    def from_rev(self, rev):
        return self.marks[rev]

    def to_rev(self, mark):
        return str(self.rev_marks[mark])

    def next_mark(self):
        self.last_mark += 1
        return self.last_mark

    def get_mark(self, rev):
        self.last_mark += 1
        self.marks[rev] = self.last_mark
        return self.last_mark

    def new_mark(self, rev, mark):
        self.marks[rev] = mark
        self.rev_marks[mark] = rev
        self.last_mark = mark

    def is_marked(self, rev):
        return rev in self.marks

    def get_tip(self, branch):
        return str(self.tips[branch])

    def set_tip(self, branch, tip):
        self.tips[branch] = tip

class Parser:

    def __init__(self, repo):
        self.repo = repo
        self.line = self.get_line()

    def get_line(self):
        return sys.stdin.readline().strip()

    def __getitem__(self, i):
        return self.line.split()[i]

    def check(self, word):
        return self.line.startswith(word)

    def each_block(self, separator):
        while self.line != separator:
            yield self.line
            self.line = self.get_line()

    def __iter__(self):
        return self.each_block('')

    def next(self):
        self.line = self.get_line()
        if self.line == 'done':
            self.line = None

    def get_mark(self):
        i = self.line.index(':') + 1
        return int(self.line[i:])

    def get_data(self):
        if not self.check('data'):
            return None
        i = self.line.index(' ') + 1
        size = int(self.line[i:])
        return sys.stdin.read(size)

    def get_author(self):
        ex = None
        m = RAW_AUTHOR_RE.match(self.line)
        if not m:
            return None
        _, name, email, date, tz = m.groups()
        if name and 'ext:' in name:
            m = re.match('^(.+?) ext:\((.+)\)$', name)
            if m:
                name = m.group(1)
                ex = urllib.unquote(m.group(2))

        if email != bad_mail:
            if name:
                user = '%s <%s>' % (name, email)
            else:
                user = '<%s>' % (email)
        else:
            user = name

        if ex:
            user += ex

        tz = int(tz)
        tz = ((tz / 100) * 3600) + ((tz % 100) * 60)
        return (user, int(date), -tz)

def fix_file_path(path):
    path = os.path.normpath(path)
    if not os.path.isabs(path):
        return path
    return os.path.relpath(path, '/')

def export_files(files):
    final = []
    for f in files:
        fid = node.hex(f.filenode())

        if fid in filenodes:
            mark = filenodes[fid]
        else:
            mark = marks.next_mark()
            filenodes[fid] = mark
            d = f.data()

            print "blob"
            print "mark :%u" % mark
            print "data %d" % len(d)
            print d

        path = fix_file_path(f.path())
        final.append((gitmode(f.flags()), mark, path))

    return final

def get_filechanges(repo, ctx, parent):
    modified = set()
    added = set()
    removed = set()

    # load earliest manifest first for caching reasons
    prev = parent.manifest().copy()
    cur = ctx.manifest()

    for fn in cur:
        if fn in prev:
            if (cur.flags(fn) != prev.flags(fn) or cur[fn] != prev[fn]):
                modified.add(fn)
            del prev[fn]
        else:
            added.add(fn)
    removed |= set(prev.keys())

    return added | modified, removed

def fixup_user_git(user):
    name = mail = None
    user = user.replace('"', '')
    m = AUTHOR_RE.match(user)
    if m:
        name = m.group(1)
        mail = m.group(2).strip()
    else:
        m = EMAIL_RE.match(user)
        if m:
            mail = m.group(1)
        else:
            m = NAME_RE.match(user)
            if m:
                name = m.group(1).strip()
    return (name, mail)

def fixup_user_hg(user):
    def sanitize(name):
        # stole this from hg-git
        return re.sub('[<>\n]', '?', name.lstrip('< ').rstrip('> '))

    m = AUTHOR_HG_RE.match(user)
    if m:
        name = sanitize(m.group(1))
        mail = sanitize(m.group(2))
        ex = m.group(3)
        if ex:
            name += ' ext:(' + urllib.quote(ex) + ')'
    else:
        name = sanitize(user)
        if '@' in user:
            mail = name
        else:
            mail = None

    return (name, mail)

def fixup_user(user):
    if mode == 'git':
        name, mail = fixup_user_git(user)
    else:
        name, mail = fixup_user_hg(user)

    if not name:
        name = bad_name
    if not mail:
        mail = bad_mail

    return '%s <%s>' % (name, mail)

def updatebookmarks(repo, peer):
    remotemarks = peer.listkeys('bookmarks')
    localmarks = repo._bookmarks

    if not remotemarks:
        return

    for k, v in remotemarks.iteritems():
        localmarks[k] = hgbin(v)

    if hasattr(localmarks, 'write'):
        localmarks.write()
    else:
        bookmarks.write(repo)

def get_repo(url, alias):
    global peer

    myui = ui.ui()
    myui.setconfig('ui', 'interactive', 'off')
    myui.fout = sys.stderr

    if get_config_bool('remote-hg.insecure'):
        myui.setconfig('web', 'cacerts', '')

    extensions.loadall(myui)

    if hg.islocal(url) and not os.environ.get('GIT_REMOTE_HG_TEST_REMOTE'):
        repo = hg.repository(myui, url)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
    else:
        shared_path = os.path.join(gitdir, 'hg')

        # check and upgrade old organization
        hg_path = os.path.join(shared_path, '.hg')
        if os.path.exists(shared_path) and not os.path.exists(hg_path):
            repos = os.listdir(shared_path)
            for x in repos:
                local_hg = os.path.join(shared_path, x, 'clone', '.hg')
                if not os.path.exists(local_hg):
                    continue
                if not os.path.exists(hg_path):
                    shutil.move(local_hg, hg_path)
                shutil.rmtree(os.path.join(shared_path, x, 'clone'))

        # setup shared repo (if not there)
        try:
            hg.peer(myui, {}, shared_path, create=True)
        except error.RepoError:
            pass

        if not os.path.exists(dirname):
            os.makedirs(dirname)

        local_path = os.path.join(dirname, 'clone')
        if not os.path.exists(local_path):
            hg.share(myui, shared_path, local_path, update=False)
        else:
            # make sure the shared path is always up-to-date
            util.writefile(os.path.join(local_path, '.hg', 'sharedpath'), hg_path)

        repo = hg.repository(myui, local_path)
        try:
            peer = hg.peer(myui, {}, url)
        except:
            die('Repository error')
        repo.pull(peer, heads=None, force=True)

        updatebookmarks(repo, peer)

    return repo

def rev_to_mark(rev):
    return marks.from_rev(rev.hex())

def mark_to_rev(mark):
    return marks.to_rev(mark)

def export_ref(repo, name, kind, head):
    ename = '%s/%s' % (kind, name)
    try:
        tip = marks.get_tip(ename)
        tip = repo[tip].rev()
    except:
        tip = 0

    revs = xrange(tip, head.rev() + 1)
    total = len(revs)

    for rev in revs:

        c = repo[rev]
        node = c.node()

        if marks.is_marked(c.hex()):
            continue

        (manifest, user, (time, tz), files, desc, extra) = repo.changelog.read(node)
        rev_branch = extra['branch']

        author = "%s %d %s" % (fixup_user(user), time, gittz(tz))
        if 'committer' in extra:
            try:
                cuser, ctime, ctz = extra['committer'].rsplit(' ', 2)
                committer = "%s %s %s" % (cuser, ctime, gittz(int(ctz)))
            except ValueError:
                cuser = extra['committer']
                committer = "%s %d %s" % (fixup_user(cuser), time, gittz(tz))
        else:
            committer = author

        parents = [repo[p] for p in repo.changelog.parentrevs(rev) if p >= 0]

        if len(parents) == 0:
            modified = c.manifest().keys()
            removed = []
        else:
            modified, removed = get_filechanges(repo, c, parents[0])

        desc += '\n'

        if mode == 'hg':
            extra_msg = ''

            if rev_branch != 'default':
                extra_msg += 'branch : %s\n' % rev_branch

            renames = []
            for f in c.files():
                if f not in c.manifest():
                    continue
                rename = c.filectx(f).renamed()
                if rename:
                    renames.append((rename[0], f))

            for e in renames:
                extra_msg += "rename : %s => %s\n" % e

            for key, value in extra.iteritems():
                if key in ('author', 'committer', 'encoding', 'message', 'branch', 'hg-git'):
                    continue
                else:
                    extra_msg += "extra : %s : %s\n" % (key, urllib.quote(value))

            if extra_msg:
                desc += '\n--HG--\n' + extra_msg

        if len(parents) == 0 and rev:
            print 'reset %s/%s' % (prefix, ename)

        modified_final = export_files(c.filectx(f) for f in modified)

        print "commit %s/%s" % (prefix, ename)
        print "mark :%d" % (marks.get_mark(c.hex()))
        print "author %s" % (author)
        print "committer %s" % (committer)
        print "data %d" % (len(desc))
        print desc

        if len(parents) > 0:
            print "from :%s" % (rev_to_mark(parents[0]))
            if len(parents) > 1:
                print "merge :%s" % (rev_to_mark(parents[1]))

        for f in removed:
            print "D %s" % (fix_file_path(f))
        for f in modified_final:
            print "M %s :%u %s" % f
        print

        progress = (rev - tip)
        if (progress % 100 == 0):
            print "progress revision %d '%s' (%d/%d)" % (rev, name, progress, total)

    # make sure the ref is updated
    print "reset %s/%s" % (prefix, ename)
    print "from :%u" % rev_to_mark(head)
    print

    pending_revs = set(revs) - notes
    if pending_revs:
        note_mark = marks.next_mark()
        ref = "refs/notes/hg"

        print "commit %s" % ref
        print "mark :%d" % (note_mark)
        print "committer remote-hg <> %d %s" % (ptime.time(), gittz(ptime.timezone))
        desc = "Notes for %s\n" % (name)
        print "data %d" % (len(desc))
        print desc
        if marks.last_note:
            print "from :%u" % marks.last_note

        for rev in pending_revs:
            notes.add(rev)
            c = repo[rev]
            print "N inline :%u" % rev_to_mark(c)
            msg = c.hex()
            print "data %d" % (len(msg))
            print msg
        print

        marks.last_note = note_mark

    marks.set_tip(ename, head.hex())

def export_tag(repo, tag):
    export_ref(repo, tag, 'tags', repo[hgref(tag)])

def export_bookmark(repo, bmark):
    head = bmarks[hgref(bmark)]
    export_ref(repo, bmark, 'bookmarks', head)

def export_branch(repo, branch):
    tip = get_branch_tip(repo, branch)
    head = repo[tip]
    export_ref(repo, branch, 'branches', head)

def export_head(repo):
    export_ref(repo, g_head[0], 'bookmarks', g_head[1])

def do_capabilities(parser):
    print "import"
    print "export"
    print "refspec refs/heads/branches/*:%s/branches/*" % prefix
    print "refspec refs/heads/*:%s/bookmarks/*" % prefix
    print "refspec refs/tags/*:%s/tags/*" % prefix

    path = os.path.join(dirname, 'marks-git')

    if os.path.exists(path):
        print "*import-marks %s" % path
    print "*export-marks %s" % path
    print "option"

    print

def branch_tip(branch):
    return branches[branch][-1]

def get_branch_tip(repo, branch):
    heads = branches.get(hgref(branch), None)
    if not heads:
        return None

    # verify there's only one head
    if (len(heads) > 1):
        warn("Branch '%s' has more than one head, consider merging" % branch)
        return branch_tip(hgref(branch))

    return heads[0]

def list_head(repo, cur):
    global g_head, fake_bmark

    if 'default' not in branches:
        # empty repo
        return

    node = repo[branch_tip('default')]
    head = 'master' if not 'master' in bmarks else 'default'
    fake_bmark = head
    bmarks[head] = node

    head = gitref(head)
    print "@refs/heads/%s HEAD" % head
    g_head = (head, node)

def do_list(parser):
    repo = parser.repo
    for bmark, node in bookmarks.listbookmarks(repo).iteritems():
        bmarks[bmark] = repo[node]

    cur = repo.dirstate.branch()
    orig = peer if peer else repo

    for branch, heads in orig.branchmap().iteritems():
        # only open heads
        heads = [h for h in heads if 'close' not in repo.changelog.read(h)[5]]
        if heads:
            branches[branch] = heads

    list_head(repo, cur)

    if track_branches:
        for branch in branches:
            print "? refs/heads/branches/%s" % gitref(branch)

    for bmark in bmarks:
        if  bmarks[bmark].hex() == '0000000000000000000000000000000000000000':
            warn("Ignoring invalid bookmark '%s'", bmark)
        else:
            print "? refs/heads/%s" % gitref(bmark)

    for tag, node in repo.tagslist():
        if tag == 'tip':
            continue
        print "? refs/tags/%s" % gitref(tag)

    print

def do_import(parser):
    repo = parser.repo

    path = os.path.join(dirname, 'marks-git')

    print "feature done"
    if os.path.exists(path):
        print "feature import-marks=%s" % path
    print "feature export-marks=%s" % path
    print "feature force"
    sys.stdout.flush()

    tmp = encoding.encoding
    encoding.encoding = 'utf-8'

    # lets get all the import lines
    while parser.check('import'):
        ref = parser[1]

        if (ref == 'HEAD'):
            export_head(repo)
        elif ref.startswith('refs/heads/branches/'):
            branch = ref[len('refs/heads/branches/'):]
            export_branch(repo, branch)
        elif ref.startswith('refs/heads/'):
            bmark = ref[len('refs/heads/'):]
            export_bookmark(repo, bmark)
        elif ref.startswith('refs/tags/'):
            tag = ref[len('refs/tags/'):]
            export_tag(repo, tag)

        parser.next()

    encoding.encoding = tmp

    print 'done'

def parse_blob(parser):
    parser.next()
    mark = parser.get_mark()
    parser.next()
    data = parser.get_data()
    blob_marks[mark] = data
    parser.next()

def get_merge_files(repo, p1, p2, files):
    for e in repo[p1].files():
        if e not in files:
            if e not in repo[p1].manifest():
                continue
            f = { 'ctx' : repo[p1][e] }
            files[e] = f

def c_style_unescape(string):
    if string[0] == string[-1] == '"':
        return string.decode('string-escape')[1:-1]
    return string

def parse_commit(parser):
    from_mark = merge_mark = None

    ref = parser[1]
    parser.next()

    commit_mark = parser.get_mark()
    parser.next()
    author = parser.get_author()
    parser.next()
    committer = parser.get_author()
    parser.next()
    data = parser.get_data()
    parser.next()
    if parser.check('from'):
        from_mark = parser.get_mark()
        parser.next()
    if parser.check('merge'):
        merge_mark = parser.get_mark()
        parser.next()
        if parser.check('merge'):
            die('octopus merges are not supported yet')

    # fast-export adds an extra newline
    if data[-1] == '\n':
        data = data[:-1]

    files = {}

    for line in parser:
        if parser.check('M'):
            t, m, mark_ref, path = line.split(' ', 3)
            mark = int(mark_ref[1:])
            f = { 'mode' : hgmode(m), 'data' : blob_marks[mark] }
        elif parser.check('D'):
            t, path = line.split(' ', 1)
            f = { 'deleted' : True }
        else:
            die('Unknown file command: %s' % line)
        path = c_style_unescape(path)
        files[path] = f

    # only export the commits if we are on an internal proxy repo
    if dry_run and not peer:
        parsed_refs[ref] = None
        return

    def getfilectx(repo, memctx, f):
        of = files[f]
        if 'deleted' in of:
            raise IOError
        if 'ctx' in of:
            return of['ctx']
        is_exec = of['mode'] == 'x'
        is_link = of['mode'] == 'l'
        rename = of.get('rename', None)
        return context.memfilectx(f, of['data'],
                is_link, is_exec, rename)

    repo = parser.repo

    user, date, tz = author
    extra = {}

    if committer != author:
        extra['committer'] = "%s %u %u" % committer

    if from_mark:
        p1 = mark_to_rev(from_mark)
    else:
        p1 = '0' * 40

    if merge_mark:
        p2 = mark_to_rev(merge_mark)
    else:
        p2 = '0' * 40

    #
    # If files changed from any of the parents, hg wants to know, but in git if
    # nothing changed from the first parent, nothing changed.
    #
    if merge_mark:
        get_merge_files(repo, p1, p2, files)

    # Check if the ref is supposed to be a named branch
    if ref.startswith('refs/heads/branches/'):
        branch = ref[len('refs/heads/branches/'):]
        extra['branch'] = hgref(branch)

    if mode == 'hg':
        i = data.find('\n--HG--\n')
        if i >= 0:
            tmp = data[i + len('\n--HG--\n'):].strip()
            for k, v in [e.split(' : ', 1) for e in tmp.split('\n')]:
                if k == 'rename':
                    old, new = v.split(' => ', 1)
                    files[new]['rename'] = old
                elif k == 'branch':
                    extra[k] = v
                elif k == 'extra':
                    ek, ev = v.split(' : ', 1)
                    extra[ek] = urllib.unquote(ev)
            data = data[:i]

    ctx = context.memctx(repo, (p1, p2), data,
            files.keys(), getfilectx,
            user, (date, tz), extra)

    tmp = encoding.encoding
    encoding.encoding = 'utf-8'

    node = hghex(repo.commitctx(ctx))

    encoding.encoding = tmp

    parsed_refs[ref] = node
    marks.new_mark(node, commit_mark)

def parse_reset(parser):
    ref = parser[1]
    parser.next()
    # ugh
    if parser.check('commit'):
        parse_commit(parser)
        return
    if not parser.check('from'):
        return
    from_mark = parser.get_mark()
    parser.next()

    try:
        rev = mark_to_rev(from_mark)
    except KeyError:
        rev = None
    parsed_refs[ref] = rev

def parse_tag(parser):
    name = parser[1]
    parser.next()
    from_mark = parser.get_mark()
    parser.next()
    tagger = parser.get_author()
    parser.next()
    data = parser.get_data()
    parser.next()

    parsed_tags[name] = (tagger, data)

def write_tag(repo, tag, node, msg, author):
    branch = repo[node].branch()
    tip = branch_tip(branch)
    tip = repo[tip]

    def getfilectx(repo, memctx, f):
        try:
            fctx = tip.filectx(f)
            data = fctx.data()
        except error.ManifestLookupError:
            data = ""
        content = data + "%s %s\n" % (node, tag)
        return context.memfilectx(f, content, False, False, None)

    p1 = tip.hex()
    p2 = '0' * 40
    if author:
        user, date, tz = author
        date_tz = (date, tz)
    else:
        cmd = ['git', 'var', 'GIT_COMMITTER_IDENT']
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        output, _ = process.communicate()
        m = re.match('^.* <.*>', output)
        if m:
            user = m.group(0)
        else:
            user = repo.ui.username()
        date_tz = None

    ctx = context.memctx(repo, (p1, p2), msg,
            ['.hgtags'], getfilectx,
            user, date_tz, {'branch' : branch})

    tmp = encoding.encoding
    encoding.encoding = 'utf-8'

    tagnode = repo.commitctx(ctx)

    encoding.encoding = tmp

    return (tagnode, branch)

def checkheads_bmark(repo, ref, ctx):
    bmark = ref[len('refs/heads/'):]
    if not bmark in bmarks:
        # new bmark
        return True

    ctx_old = bmarks[bmark]
    ctx_new = ctx

    if not ctx.rev():
        print "error %s unknown" % ref
        return False

    if not repo.changelog.descendant(ctx_old.rev(), ctx_new.rev()):
        if force_push:
            print "ok %s forced update" % ref
        else:
            print "error %s non-fast forward" % ref
            return False

    return True

def checkheads(repo, remote, p_revs):

    remotemap = remote.branchmap()
    if not remotemap:
        # empty repo
        return True

    new = {}
    ret = True

    for node, ref in p_revs.iteritems():
        ctx = repo[node]
        branch = ctx.branch()
        if not branch in remotemap:
            # new branch
            continue
        if not ref.startswith('refs/heads/branches'):
            if ref.startswith('refs/heads/'):
                if not checkheads_bmark(repo, ref, ctx):
                    ret = False

            # only check branches
            continue
        new.setdefault(branch, []).append(ctx.rev())

    for branch, heads in new.iteritems():
        old = [repo.changelog.rev(x) for x in remotemap[branch]]
        for rev in heads:
            if check_version(2, 3):
                ancestors = repo.changelog.ancestors([rev], stoprev=min(old))
            else:
                ancestors = repo.changelog.ancestors(rev)
            found = False

            for x in old:
                if x in ancestors:
                    found = True
                    break

            if found:
                continue

            node = repo.changelog.node(rev)
            ref = p_revs[node]
            if force_push:
                print "ok %s forced update" % ref
            else:
                print "error %s non-fast forward" % ref
                ret = False

    return ret

def push_unsafe(repo, remote, parsed_refs, p_revs):

    force = force_push

    fci = discovery.findcommonincoming
    commoninc = fci(repo, remote, force=force)
    common, _, remoteheads = commoninc

    if not checkheads(repo, remote, p_revs):
        return None

    cg = repo.getbundle('push', heads=list(p_revs), common=common)

    unbundle = remote.capable('unbundle')
    if unbundle:
        if force:
            remoteheads = ['force']
        ret = remote.unbundle(cg, remoteheads, 'push')
    else:
        ret = remote.addchangegroup(cg, 'push', repo.url())

    phases = remote.listkeys('phases')
    if phases:
        for head in p_revs:
            # update to public
            remote.pushkey('phases', hghex(head), '1', '0')

    return ret

def push(repo, remote, parsed_refs, p_revs):
    if hasattr(remote, 'canpush') and not remote.canpush():
        print "error cannot push"

    if not p_revs:
        # nothing to push
        return

    lock = None
    unbundle = remote.capable('unbundle')
    if not unbundle:
        lock = remote.lock()
    try:
        ret = push_unsafe(repo, remote, parsed_refs, p_revs)
    finally:
        if lock is not None:
            lock.release()

    return ret

def check_tip(ref, kind, name, heads):
    try:
        ename = '%s/%s' % (kind, name)
        tip = marks.get_tip(ename)
    except KeyError:
        return True
    else:
        return tip in heads

def do_export(parser):
    p_bmarks = []
    p_revs = {}

    parser.next()

    for line in parser.each_block('done'):
        if parser.check('blob'):
            parse_blob(parser)
        elif parser.check('commit'):
            parse_commit(parser)
        elif parser.check('reset'):
            parse_reset(parser)
        elif parser.check('tag'):
            parse_tag(parser)
        elif parser.check('feature'):
            pass
        else:
            die('unhandled export command: %s' % line)

    need_fetch = False

    for ref, node in parsed_refs.iteritems():
        bnode = hgbin(node) if node else None
        if ref.startswith('refs/heads/branches'):
            branch = ref[len('refs/heads/branches/'):]
            if branch in branches and bnode in branches[branch]:
                # up to date
                continue

            if peer:
                remotemap = peer.branchmap()
                if remotemap and branch in remotemap:
                    heads = [hghex(e) for e in remotemap[branch]]
                    if not check_tip(ref, 'branches', branch, heads):
                        print "error %s fetch first" % ref
                        need_fetch = True
                        continue

            p_revs[bnode] = ref
            print "ok %s" % ref
        elif ref.startswith('refs/heads/'):
            bmark = ref[len('refs/heads/'):]
            new = node
            old = bmarks[bmark].hex() if bmark in bmarks else ''

            if old == new:
                continue

            print "ok %s" % ref
            if bmark != fake_bmark and \
                    not (bmark == 'master' and bmark not in parser.repo._bookmarks):
                p_bmarks.append((ref, bmark, old, new))

            if peer:
                remote_old = peer.listkeys('bookmarks').get(bmark)
                if remote_old:
                    if not check_tip(ref, 'bookmarks', bmark, remote_old):
                        print "error %s fetch first" % ref
                        need_fetch = True
                        continue

            p_revs[bnode] = ref
        elif ref.startswith('refs/tags/'):
            if dry_run:
                print "ok %s" % ref
                continue
            tag = ref[len('refs/tags/'):]
            tag = hgref(tag)
            author, msg = parsed_tags.get(tag, (None, None))
            if mode == 'git':
                if not msg:
                    msg = 'Added tag %s for changeset %s' % (tag, node[:12])
                tagnode, branch = write_tag(parser.repo, tag, node, msg, author)
                p_revs[tagnode] = 'refs/heads/branches/' + gitref(branch)
            else:
                fp = parser.repo.opener('localtags', 'a')
                fp.write('%s %s\n' % (node, tag))
                fp.close()
            p_revs[bnode] = ref
            print "ok %s" % ref
        else:
            # transport-helper/fast-export bugs
            continue

    if need_fetch:
        print
        return

    if dry_run:
        if peer and not force_push:
            checkheads(parser.repo, peer, p_revs)
        print
        return

    if peer:
        if not push(parser.repo, peer, parsed_refs, p_revs):
            # do not update bookmarks
            print
            return

        # update remote bookmarks
        remote_bmarks = peer.listkeys('bookmarks')
        for ref, bmark, old, new in p_bmarks:
            if force_push:
                old = remote_bmarks.get(bmark, '')
            if not peer.pushkey('bookmarks', bmark, old, new):
                print "error %s" % ref
    else:
        # update local bookmarks
        for ref, bmark, old, new in p_bmarks:
            if not bookmarks.pushbookmark(parser.repo, bmark, old, new):
                print "error %s" % ref

    print

def do_option(parser):
    global dry_run, force_push
    _, key, value = parser.line.split(' ')
    if key == 'dry-run':
        dry_run = (value == 'true')
        print 'ok'
    elif key == 'force':
        force_push = (value == 'true')
        print 'ok'
    else:
        print 'unsupported'

def fix_path(alias, repo, orig_url):
    url = urlparse.urlparse(orig_url, 'file')
    if url.scheme != 'file' or os.path.isabs(os.path.expanduser(url.path)):
        return
    abs_url = urlparse.urljoin("%s/" % os.getcwd(), orig_url)
    cmd = ['git', 'config', 'remote.%s.url' % alias, "hg::%s" % abs_url]
    subprocess.call(cmd)

def main(args):
    global prefix, gitdir, dirname, branches, bmarks
    global marks, blob_marks, parsed_refs
    global peer, mode, bad_mail, bad_name
    global track_branches, force_push, is_tmp
    global parsed_tags
    global filenodes
    global fake_bmark, hg_version
    global dry_run
    global notes, alias

    marks = None
    is_tmp = False
    gitdir = os.environ.get('GIT_DIR', None)

    if len(args) < 3:
        die('Not enough arguments.')

    if not gitdir:
        die('GIT_DIR not set')

    alias = args[1]
    url = args[2]
    peer = None

    hg_git_compat = get_config_bool('remote-hg.hg-git-compat')
    track_branches = get_config_bool('remote-hg.track-branches', True)
    force_push = False

    if hg_git_compat:
        mode = 'hg'
        bad_mail = 'none@none'
        bad_name = ''
    else:
        mode = 'git'
        bad_mail = 'unknown'
        bad_name = 'Unknown'

    if alias[4:] == url:
        is_tmp = True
        alias = hashlib.sha1(alias).hexdigest()

    dirname = os.path.join(gitdir, 'hg', alias)
    branches = {}
    bmarks = {}
    blob_marks = {}
    parsed_refs = {}
    parsed_tags = {}
    filenodes = {}
    fake_bmark = None
    try:
        hg_version = tuple(int(e) for e in util.version().split('.'))
    except:
        hg_version = None
    dry_run = False
    notes = set()

    repo = get_repo(url, alias)
    prefix = 'refs/hg/%s' % alias

    if not is_tmp:
        fix_path(alias, peer or repo, url)

    marks_path = os.path.join(dirname, 'marks-hg')
    marks = Marks(marks_path, repo)

    if sys.platform == 'win32':
        import msvcrt
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    parser = Parser(repo)
    for line in parser:
        if parser.check('capabilities'):
            do_capabilities(parser)
        elif parser.check('list'):
            do_list(parser)
        elif parser.check('import'):
            do_import(parser)
        elif parser.check('export'):
            do_export(parser)
        elif parser.check('option'):
            do_option(parser)
        else:
            die('unhandled command: %s' % line)
        sys.stdout.flush()

    marks.store()

def bye():
    if is_tmp:
        shutil.rmtree(dirname)

atexit.register(bye)
sys.exit(main(sys.argv))
