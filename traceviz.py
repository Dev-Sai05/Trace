#!/usr/bin/env python3
"""
traceviz.py - Streaming visualizer for mainframe / COBOL DBIO trace logs.
Vertical Swimlane Layout with Orthogonal Channel Routing.
"""

import argparse
import html
import json
import re
import sys
import time
from collections import Counter

# ---------------------------------------------------------------------------
# Regexes for line classification
# ---------------------------------------------------------------------------
RE_BATCH   = re.compile(r'^\*\*\*\s*(\d+)\s*\*\*\*$')
RE_HSTART  = re.compile(r'^\*\*\*\s*Start of\s+(\S+)\s+at\s+(.+?)\**$', re.I)
RE_HEND    = re.compile(r'^\*\*\*\s*End\s+of\s+(\S+)\s+at\s+(.+?)(?:-\s*RC\s*=\s*(-?\d+))?\**$', re.I)
RE_DISPLAY = re.compile(r'^-\s*\|(\d{2}:\d{2}:\d{2}\.\d+)\|\s*(.+?)\s*$')
RE_STATUS  = re.compile(r'^([A-Z0-9]+)\((.+?)\):(.+)$')
RE_PARA    = re.compile(r'^[A-Z][A-Z0-9]{0,6}-[A-Z0-9-]+$')
RE_ERRTXT  = re.compile(r'error|abend|fail(?:ed|ure)?|exception', re.I)

DEFAULT_FLUSH_SIZE   = 4000    
DEFAULT_TAIL_KEEP    = 32      
DEFAULT_MAX_CHILDREN = 4000    
DEFAULT_MAX_PERIOD   = 8       
DEFAULT_MAX_DETAILS  = 20      


def is_error_text(s):
    return bool(RE_ERRTXT.search(s))


def shape_of(node):
    t = node['type']
    if t == 'module':
        return 'MOD[%s]{%s}' % (node['name'], '|'.join(shape_of(c) for c in node['children']))
    if t == 'loop':
        return 'LOOP[%d,%d]{%s}' % (node['count'], node['period'],
                                     '|'.join(shape_of(c) for c in node['children']))
    if t == 'step':
        return 'S:' + node['name'] + (':ERR' if node.get('error') else '')
    if t == 'status':
        if node['cls'] == 'error':
            return 'ERR:%s:%s' % (node['para'], node['message'])
        return 'T:' + node['para']
    if t == 'info':
        return 'I'
    if t == 'raw':
        return 'RAW:ERR' if node.get('error') else 'RAW'
    return 'X'


def collapse_loops(nodes, max_period, stats):
    shapes = [shape_of(n) for n in nodes]
    out = []
    i, n = 0, len(nodes)
    while i < n:
        best_p, best_count = 0, 0
        max_p = min(max_period, (n - i) // 3)
        for p in range(1, max_p + 1):
            count = 1
            while i + count * p + p <= n:
                if shapes[i + count * p:i + count * p + p] != shapes[i:i + p]:
                    break
                count += 1
            if count >= 3 and count * p > best_count * best_p:
                best_p, best_count = p, count
        if best_count >= 3:
            span = nodes[i:i + best_count * best_p]
            sample = nodes[i:i + best_p]
            msgs = Counter()
            for node in span:
                if node['type'] == 'status':
                    msgs[node['message']] += 1
            out.append({
                'type': 'loop', 'count': best_count, 'period': best_p,
                'children': sample,
                'message_summary': msgs.most_common(5),
            })
            stats['loops'] += 1
            stats['iter_saved'] += (best_count - 1) * best_p
            i += best_count * best_p
        else:
            out.append(nodes[i])
            i += 1
    return out


class Frame:
    __slots__ = ('name', 'children', 'buffer')

    def __init__(self, name):
        self.name = name
        self.children = []
        self.buffer = []


def flush_frame(frame, max_period, stats, final=False, tail_keep=DEFAULT_TAIL_KEEP):
    if not frame.buffer:
        return
    if final:
        frame.children.extend(collapse_loops(frame.buffer, max_period, stats))
        frame.buffer = []
    else:
        keep = frame.buffer[-tail_keep:]
        rest = frame.buffer[:-tail_keep]
        if rest:
            frame.children.extend(collapse_loops(rest, max_period, stats))
        frame.buffer = keep


def cap_children(node, max_children):
    if node['type'] not in ('module', 'loop', 'root'):
        return node
    kids = node['children']
    for k in kids:
        cap_children(k, max_children)
    if len(kids) > max_children:
        half = max_children // 2
        head, tail = kids[:half], kids[-half:]
        omitted = kids[half:-half]
        omitted_errors = sum(1 for k in _flatten(omitted) if k.get('type') == 'status' and k.get('cls') == 'error')
        placeholder = {
            'type': 'omitted',
            'count': len(omitted),
            'errors': omitted_errors,
        }
        node['children'] = head + [placeholder] + tail
    return node


def _flatten(nodes):
    for n in nodes:
        yield n
        if n['type'] in ('module', 'loop'):
            yield from _flatten(n['children'])


GLOSSARY = [
    (r'LOGON',                'Connecting / authenticating to the database.'),
    (r'SIGN[-_]?OFF',          'Disconnecting from the database at the end of the job.'),
    (r'FETCH',                 'Reading the next row from an open SQL cursor.'),
    (r'DECLARE',               'Declaring the SQL cursor definition, before it can be opened.'),
    (r'\bOPEN\b',              'Opening a database cursor or file for reading.'),
    (r'\bCLOSE\b',             'Closing a database cursor or file.'),
    (r'COMMIT',                'Committing pending database changes so far.'),
    (r'ROLLBACK',              'Undoing uncommitted database changes.'),
    (r'INITIALI[SZ]E|^INIT',   'One-time setup for this routine (work areas, counters, defaults).'),
    (r'VALIDATE',              'Checking that input values are valid before continuing.'),
    (r'RETRIEVE',              'Pulling a control block or configuration value.'),
    (r'ATTACH-SHMEM',          'Attaching to a shared-memory segment used for cross-process control blocks.'),
    (r'SHMEM',                 'Working with a shared-memory segment.'),
    (r'ENV-VAR',               'Reading an operating-system environment variable.'),
    (r'TRANSLATE-STATUS',      "Converting the database driver's raw return code."),
    (r'EVALUATE-FILE-NAME',    'Working out which physical file or table this request applies to.'),
    (r'PROCESS-RECORD',        'Doing the per-row work on the record that was just fetched.'),
    (r'GET-DATE',              'Reading the current system date/time.'),
    (r'GET-SYSTEM-NO',         'Determining which system/instance number this run is executing under.'),
    (r'SET-APPLICATION-NAME',  'Tagging this DB session with an application name.'),
    (r'SETTXCON|TXCON',        'Setting up transaction/connection context for subsequent SQL calls.'),
    (r'DISPLAY',               'Writing a trace/debug line — diagnostic output, not business logic.'),
    (r'FINALISE|FINALIZE',     'Tearing down / releasing resources this routine acquired.'),
    (r'MAIN[-_]?LINE|^MAIN$',  "This routine's top-level driver paragraph."),
    (r'^BEGIN$',               'Start of the main processing block.'),
    (r'^ENTRY$',               'Marks the start of this paragraph.'),
    (r'EXIT',                  'Marks the end of this paragraph, returning control to the caller.'),
]
GLOSSARY = [(re.compile(pat, re.I), text) for pat, text in GLOSSARY]

MODULE_HINTS = [
    (re.compile(r'^UT\d', re.I), 'Naming suggests a shared utility routine.'),
    (re.compile(r'^DBIO', re.I), 'Naming suggests this is the DBIO layer that mediates SQL calls.'),
    (re.compile(r'^IOMISC', re.I), 'Naming suggests a miscellaneous I/O helper module.'),
]


def explain_text(name, message=None):
    hay = name or ''
    for pat, text in GLOSSARY:
        if pat.search(hay):
            return text
    if message:
        for pat, text in GLOSSARY:
            if pat.search(message):
                return text
    return None


def explain_module(name):
    for pat, text in MODULE_HINTS:
        if pat.search(name):
            return text
    return 'Custom site paragraph logic execution step.'


def explain_error_message(message):
    m = re.search(r'CURSOR FETCH ERROR\.?0*(\d+)', message, re.I)
    if m:
        code = m.group(1)
        return f'A cursor FETCH failed. Underlying SQLCODE/return code: {code}.'
    if re.search(r'ABEND', message, re.I):
        return 'The program terminated abnormally (an ABEND).'
    if re.search(r'TIMEOUT|TIME OUT', message, re.I):
        return 'The operation exceeded its allotted time window.'
    return 'An error condition was reported here.'


def annotate_tree(node):
    t = node['type']
    if t == 'module':
        node['explain'] = explain_module(node['name'])
        for c in node['children']:
            annotate_tree(c)
    elif t == 'loop':
        for c in node['children']:
            annotate_tree(c)
    elif t == 'step':
        node['explain'] = explain_text(node['name'])
    elif t == 'status':
        if node['cls'] == 'error':
            node['explain'] = explain_error_message(node['message'])
        else:
            node['explain'] = explain_text(node['para'], node['message'])
    return node


def _walk_order(node, statuses, loops):
    for c in node['children']:
        if c['type'] == 'status':
            statuses.append(c)
        elif c['type'] == 'loop':
            loops.append(c)
            _walk_order(c, statuses, loops)
        elif c['type'] == 'module':
            _walk_order(c, statuses, loops)


def build_narrative(root, header, stats, error_index):
    statuses, loops = [], []
    _walk_order(root, statuses, loops)
    sentences = []
    prog = header.get('program') or 'The program'
    sentences.append(f"{prog} started at {header.get('start_at') or 'an unknown time'}.")
    if error_index:
        first = error_index[0]
        sentences.append(f"On line {first['line']}, an anomaly occurred: \"{first['message']}\"")
    rc = header.get('rc')
    if rc is not None:
        sentences.append(f"The job completed with return code {rc}.")
    return ' '.join(sentences)


class StreamGraph:
    def __init__(self, max_nodes=1500):
        self.nodes = {}       
        self.edges = {}       
        self.order = {}       
        self.seq = 0
        self.last_key = None
        self.max_nodes = max_nodes
        self.truncated = False

    def _touch(self, key, kind, label, error, message, line_no):
        node = self.nodes.get(key)
        if node is None:
            if len(self.nodes) >= self.max_nodes:
                self.truncated = True
                return None
            explain = explain_error_message(message) if (error and message) else (explain_module(label) if kind == 'module' else explain_text(label, message))
            node = {'kind': kind, 'label': label, 'error': error, 'count': 0,
                     'messages': Counter(), 'first_line': line_no, 'explain': explain}
            self.nodes[key] = node
            self.order[key] = self.seq
            self.seq += 1
        node['count'] += 1
        if error:
            node['error'] = True
        if message:
            node['messages'][message] += 1
        return node

    def add(self, key, kind, label, error=False, message=None, line_no=0, force_edge_kind=None):
        node = self._touch(key, kind, label, error, message, line_no)
        if node is not None and self.last_key is not None and self.last_key in self.nodes:
            ekey = (self.last_key, key)
            edge = self.edges.get(ekey)
            if edge is None:
                if force_edge_kind:
                    ekind = force_edge_kind
                elif key == self.last_key or self.order.get(key, 10 ** 9) < self.order.get(self.last_key, -1):
                    ekind = 'LOOP'
                else:
                    ekind = ''
                edge = {'count': 0, 'kind': ekind}
                self.edges[ekey] = edge
            edge['count'] += 1
            if error and edge['kind'] not in ('LOOP',):
                edge['kind'] = 'ERROR'
        if node is not None:
            self.last_key = key

    def to_json(self):
        nodes = []
        for key, n in self.nodes.items():
            nodes.append({
                'key': key, 'kind': n['kind'], 'label': n['label'], 'error': n['error'],
                'count': n['count'], 'messages': n['messages'].most_common(5),
                'first_line': n['first_line'], 'order': self.order[key], 'explain': n.get('explain'),
            })
        edges = [{'from': a, 'to': b, 'count': e['count'], 'kind': e['kind']}
                  for (a, b), e in self.edges.items()]
        return {'nodes': nodes, 'edges': edges, 'truncated': self.truncated}


def compute_layout(graph_json, step_x=340, step_y=75, box_w=240, box_h=46):
    """
    Computes columns with wide layout channels (step_x) to permit clean
    orthogonal line drops between module swimlanes.
    """
    nodes = {n['key']: n for n in graph_json['nodes']}
    
    for key, n in nodes.items():
        if '->' in key:
            parts = key.split('->', 1)
            group = parts[0].split(':', 1)[-1]
        else:
            group = 'ROOT'
        n['group'] = group

    groups_seen = []
    for n in sorted(nodes.values(), key=lambda x: x['order']):
        g = n['group']
        if g not in groups_seen:
            groups_seen.append(g)
            
    group_cols = {g: idx for idx, g in enumerate(groups_seen)}

    columns = {}
    for k in sorted(nodes, key=lambda k: nodes[k]['order']):
        col = group_cols[nodes[k]['group']]
        columns.setdefault(col, []).append(k)

    for col, keys in columns.items():
        for row, k in enumerate(keys):
            nodes[k]['x'] = col * step_x
            nodes[k]['y'] = row * step_y
            nodes[k]['layer'] = col

    max_x = (max(group_cols.values()) + 1) * step_x if group_cols else 0
    max_y = max((len(v) for v in columns.values()), default=0) * step_y
    return {'nodes': list(nodes.values()), 'edges': graph_json['edges'],
            'truncated': graph_json['truncated'], 'box_w': box_w, 'box_h': box_h,
            'width': max_x, 'height': max_y}


def parse_stream(path, max_period=DEFAULT_MAX_PERIOD, flush_size=DEFAULT_FLUSH_SIZE,
                  tail_keep=DEFAULT_TAIL_KEEP, max_details=DEFAULT_MAX_DETAILS,
                  progress=True):
    root_frame = Frame('ROOT')
    stack = [root_frame]
    mod_stack = ['ROOT'] 
    header = {'program': None, 'start_at': None, 'end_at': None, 'rc': None}
    error_index = []
    stats = {'lines': 0, 'steps': 0, 'status': 0, 'errors': 0, 'modules': 0,
             'loops': 0, 'iter_saved': 0, 'unmatched_end': 0}
    graph = StreamGraph()

    t0 = time.time()
    bytes_read = 0
    file_size = None
    try:
        import os
        file_size = os.path.getsize(path)
    except OSError:
        pass

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line_no, raw in enumerate(f, 1):
            bytes_read += len(raw)
            line = raw.strip()
            stats['lines'] = line_no
            if not line:
                continue

            if progress and line_no % 200000 == 0:
                pct = (bytes_read / file_size * 100) if file_size else 0
                if callable(progress):
                    progress(line_no, pct, time.time() - t0, stats)

            frame = stack[-1]

            if line.startswith('***'):
                m = RE_BATCH.match(line)
                if m: continue
                m = RE_HSTART.match(line)
                if m:
                    header['program'] = header['program'] or m.group(1)
                    header['start_at'] = m.group(2).strip()
                    continue
                m = RE_HEND.match(line)
                if m:
                    header['program'] = header['program'] or m.group(1)
                    header['end_at'] = m.group(2).strip()
                    if m.group(3) is not None:
                        header['rc'] = m.group(3).strip()
                    continue
                continue

            if line.startswith('START OF '):
                name = line[9:].strip()
                stack.append(Frame(name))
                stats['modules'] += 1
                parent = mod_stack[-1]
                mkey = f"M:{parent}->{name}" if parent != 'ROOT' else f"M:{name}"
                mod_stack.append(name)
                graph.add(mkey, 'module', name, line_no=line_no, force_edge_kind='CALL')
                continue

            if line.startswith('END OF '):
                if len(stack) > 1:
                    finished = stack.pop()
                    if len(mod_stack) > 1:
                        mod_stack.pop()
                    flush_frame(finished, max_period, stats, final=True, tail_keep=tail_keep)
                    stack[-1].buffer.append({'type': 'module', 'name': finished.name, 'children': finished.children})
                    if len(stack[-1].buffer) >= flush_size:
                        flush_frame(stack[-1], max_period, stats, final=False, tail_keep=tail_keep)
                else:
                    stats['unmatched_end'] += 1
                continue

            m = RE_DISPLAY.match(line)
            if m:
                sibs = frame.buffer
                last = sibs[-1] if sibs else None
                ts, msg = m.group(1), m.group(2)
                err = is_error_text(msg)
                if last is not None and last['type'] in ('step', 'status'):
                    details = last.setdefault('details', [])
                    if len(details) < max_details:
                        details.append({'ts': ts, 'msg': msg})
                    if err:
                        last['error'] = True
                        error_index.append({'line': line_no, 'para': last.get('name') or last.get('para'), 'message': msg, 'ts': ts})
                        stats['errors'] += 1
                else:
                    node = {'type': 'info', 'details': [{'ts': ts, 'msg': msg}]}
                    if err:
                        node['error'] = True
                        error_index.append({'line': line_no, 'para': None, 'message': msg, 'ts': ts})
                        stats['errors'] += 1
                    sibs.append(node)
                continue

            m = RE_STATUS.match(line)
            if m:
                prog, para, msg = m.group(1), m.group(2), m.group(3)
                err = is_error_text(msg)
                ok = bool(re.search(r'success|good|complet|commit', msg, re.I)) and not err
                frame.buffer.append({'type': 'status', 'program': prog, 'para': para, 'message': msg, 'cls': 'error' if err else ('success' if ok else 'neutral')})
                stats['status'] += 1
                if err:
                    error_index.append({'line': line_no, 'para': para, 'message': msg, 'ts': None})
                    stats['errors'] += 1
                curr_mod = mod_stack[-1]
                graph.add(f"T:{curr_mod}->{para}" + (':ERR' if err else ''), 'status', para, error=err, message=msg, line_no=line_no)
                if len(frame.buffer) >= flush_size:
                    flush_frame(frame, max_period, stats, final=False, tail_keep=tail_keep)
                continue

            if RE_PARA.match(line):
                frame.buffer.append({'type': 'step', 'name': line})
                stats['steps'] += 1
                curr_mod = mod_stack[-1]
                graph.add(f"S:{curr_mod}->{line}", 'step', line, line_no=line_no)
                if len(frame.buffer) >= flush_size:
                    flush_frame(frame, max_period, stats, final=False, tail_keep=tail_keep)
                continue

            err = is_error_text(line)
            node = {'type': 'raw', 'text': line}
            if err:
                node['error'] = True
                error_index.append({'line': line_no, 'para': None, 'message': line, 'ts': None})
                stats['errors'] += 1
            frame.buffer.append(node)
            if len(frame.buffer) >= flush_size:
                flush_frame(frame, max_period, stats, final=False, tail_keep=tail_keep)

    while len(stack) > 1:
        finished = stack.pop()
        flush_frame(finished, max_period, stats, final=True, tail_keep=tail_keep)
        stack[-1].buffer.append({'type': 'module', 'name': finished.name + ' (unclosed)', 'children': finished.children})

    flush_frame(root_frame, max_period, stats, final=True, tail_keep=tail_keep)
    root = {'type': 'root', 'name': 'ROOT', 'children': root_frame.children}
    stats['elapsed'] = time.time() - t0
    stats['bytes'] = bytes_read
    stats['graph_nodes'] = len(graph.nodes)
    stats['graph_edges'] = len(graph.edges)
    return root, header, error_index, stats, graph.to_json()


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>TRACEVIEW // __PROGRAM__</title>
<style>
:root{--bg:#080b08;--panel:#0e140f;--panel2:#101a12;--line:#1c2b1e;--green:#4dff88;--green-dim:#7fb894;
--cyan:#6fd7e8;--amber:#ffb347;--red:#ff5c5c;--gray:#5c6b60;--text:#c9e6d2;}
*{box-sizing:border-box;}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:ui-monospace,"SF Mono",monospace;font-size:13px;}
.wrap{display:flex;min-height:100vh;}
.sidebar{width:340px;flex:0 0 340px;background:var(--panel);border-right:1px solid var(--line);padding:18px;position:sticky;top:0;height:100vh;overflow-y:auto;}
.brand .t2{font-size:20px;color:var(--green);font-weight:700;margin-bottom:14px;}
.stat{display:flex;justify-content:space-between;padding:5px 0;font-size:11px;border-bottom:1px dashed var(--line);}
.stat .k{color:var(--gray);text-transform:uppercase;font-size:10px;}
.errbox{margin-top:16px;border-top:1px solid var(--line);padding-top:10px;}
.main{flex:1;padding:24px 30px;overflow-x:auto;}
.headerbar h1{font-size:15px;margin:0 0 4px;}
.headerbar .sub{font-size:11px;color:var(--gray);}
.node{position:relative;margin:6px 0;}
.children{margin-left:20px;padding-left:18px;border-left:2px solid var(--line);margin-top:6px;}
.row{display:flex;align-items:flex-start;gap:8px;padding:5px 10px;border-radius:4px;}
.row .tag{font-size:9px;padding:1px 5px;border-radius:3px;flex:0 0 auto;}
.step .tag{color:var(--cyan);border:1px solid rgba(111,215,232,.25);}
.status.success .row{color:var(--green);}
.status.error .row,.step.error .row{color:var(--red);background:rgba(255,92,92,.08);border:1px solid rgba(255,92,92,.4);}
.module>.head,.loop>.head{display:flex;align-items:center;gap:8px;cursor:pointer;padding:6px 10px;border-radius:5px;}
.module>.head{color:var(--cyan);background:rgba(111,215,232,.05);border:1px solid rgba(111,215,232,.25);}
.loop>.head{color:var(--amber);background:rgba(255,179,71,.06);border:1px dashed rgba(255,179,71,.45);}
.caret{width:10px;display:inline-block;transition:transform .15s;}
.module.collapsed>.children,.loop.collapsed>.children{display:none;}
.module.collapsed>.head .caret,.loop.collapsed>.head .caret{transform:rotate(-90deg);}
.explain{margin:2px 0 4px 26px;font-size:11px;color:var(--gray);font-style:italic;}
body.hide-explain .explain{display:none;}
.narrative{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--green);border-radius:5px;padding:14px 16px;margin-bottom:16px;}
.tabs{display:flex;gap:6px;margin-bottom:14px;}
.tabbtn{background:var(--panel2);color:var(--gray);border:1px solid var(--line);border-radius:5px 5px 0 0;padding:8px 16px;cursor:pointer;}
.tabbtn.active{color:var(--green);background:var(--panel);border-bottom:2px solid var(--bg);}
.view{display:none;} .view.active{display:block;}
.crumbs{font-size:11px;color:var(--gray);margin-bottom:14px;padding:8px 10px;background:var(--panel);border:1px solid var(--line);border-radius:5px;}

.flowcols{display:flex;gap:20px;align-items:flex-start;}
.flowdiagram{flex:1;min-width:0;}
.canvas-toolbar{display:flex;align-items:center;gap:10px;margin-bottom:10px;font-size:11px;}
.flow-canvas-wrap{position:relative;height:75vh;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--bg);cursor:grab;}
.flow-canvas-wrap.dragging{cursor:grabbing;}
.flow-canvas{position:absolute;top:0;left:0;transform-origin:0 0;}
.connectors{position:absolute;top:0;left:0;pointer-events:none;overflow:visible;}
.flowbox{position:absolute;border:1.5px solid var(--line);border-radius:6px;padding:8px 12px;cursor:pointer;background:var(--panel2);display:flex;align-items:center;gap:8px;box-sizing:border-box;}
.flowbox:hover{box-shadow:0 0 0 1px var(--green);z-index:5;}
.flowbox .lbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;}
.flowbox.t-module{border-color:rgba(111,215,232,.5);color:var(--cyan);background:rgba(111,215,232,.05);}
.flowbox.t-status-success{color:var(--green);border-color:rgba(77,255,136,.35);}
.flowbox.t-error{color:var(--red);border-color:var(--red);box-shadow:0 0 10px rgba(255,92,92,.15);}
.logpreview{flex:0 0 380px;position:sticky;top:18px;background:#050705;border:1px solid var(--line);border-radius:6px;padding:12px;font-size:11px;max-height:80vh;overflow:auto;}
.logpreview h4{margin:0 0 8px;color:var(--green);text-transform:uppercase;}
</style></head>
<body>
<div class="wrap">
  <div class="sidebar">
    <div class="brand"><div class="t2">TRACEVIEW REPORT</div></div>
    <div id="stats"></div>
    <div class="errbox" id="errbox"></div>
  </div>
  <div class="main">
    <div class="headerbar"><h1 id="ht"></h1><div class="sub" id="hs"></div></div>
    <div class="narrative" id="narrative"><h3>Execution Narrative</h3><div id="narrativeText"></div></div>
    <div class="tabs">
      <button class="tabbtn active" id="tabTreeBtn">Tree View</button>
      <button class="tabbtn" id="tabFlowBtn">Vertical Flow Diagram</button>
    </div>

    <div class="flow" id="flow"></div>

    <div class="view" id="viewFlow">
      <div class="crumbs" id="crumbs"></div>
      <div class="flowcols">
        <div class="flowdiagram">
          <div class="canvas-toolbar">
            <button id="zoomOutBtn">\u2212</button>
            <span id="zoomLabel">100%</span>
            <button id="zoomInBtn">+</button>
            <button id="zoomResetBtn">Reset view</button>
          </div>
          <div class="flow-canvas-wrap" id="flowWrap">
            <div class="flow-canvas" id="flowCanvas">
              <svg class="connectors" id="flowSvg"></svg>
            </div>
          </div>
        </div>
        <div class="logpreview" id="logPreview">
          <h4>Execution Context</h4>
          <div id="lp-body">Hover or select a vertical pipeline box to parse log fragments.</div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
const DATA = __DATA_JSON__;
function esc(s){return (s+'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function el(tag,cls,html){const e=document.createElement(tag); if(cls)e.className=cls; if(html!==undefined)e.innerHTML=html; return e;}

function renderNode(node){
  if(node.type==='module'){
    const wrap = el('div','node module collapsed');
    const head = el('div','head', `<span>\u25be CALL \u203a ${esc(node.name)}</span>`);
    head.addEventListener('click',()=>wrap.classList.toggle('collapsed'));
    const kids = el('div','children');
    node.children.forEach(c=>kids.appendChild(renderNode(c)));
    wrap.appendChild(head); wrap.appendChild(kids); return wrap;
  }
  if(node.type==='step'){
    const wrap = el('div','node step'+(node.error?' error':''));
    wrap.appendChild(el('div','row',`<span class="tag">PARA</span><span>${esc(node.name)}</span>`));
    return wrap;
  }
  if(node.type==='status'){
    const wrap = el('div','node status '+node.cls);
    wrap.appendChild(el('div','row',`<span class="tag">STATUS</span><span>${esc(node.program)}(${esc(node.para)}) — ${esc(node.message)}</span>`));
    return wrap;
  }
  return el('div','node raw',esc(node.text||''));
}

document.getElementById('ht').textContent = (DATA.header.program? 'PROGRAM '+DATA.header.program : 'TRACE LOG EXECUTION');
document.getElementById('hs').textContent = (DATA.header.start_at||'?') + '  \u2192  ' + (DATA.header.end_at||'?');
document.getElementById('narrativeText').textContent = DATA.narrative || '';

const GRAPH = DATA.graph || {nodes:[], edges:[], width:0, height:0, box_w:240, box_h:46};
const OFF_X = 60, OFF_Y = 50;
const nodeByKey = {};
GRAPH.nodes.forEach(n => nodeByKey[n.key] = n);

function labelFor(n){ return (n.kind==='module' ? 'CALL \u203a ' : '') + n.label; }

function showLogPreview(n){
  const body = document.getElementById('lp-body');
  body.innerHTML = `<strong>${esc(labelFor(n))}</strong><br><br>First invoked at log line: ${n.first_line}<br>Total executions: ${n.count}<br><br><em>${esc(n.explain||'')}</em>`;
}

function edgeColor(kind){
  if(kind==='ERROR') return '#ff5c5c';
  if(kind==='CALL') return '#6fd7e8';
  if(kind==='LOOP') return '#ffb347';
  return '#5b7a63';
}

function drawEdges(){
  const svg = document.getElementById('flowSvg');
  svg.innerHTML = '';
  const w = GRAPH.width + OFF_X*2 + GRAPH.box_w, h = GRAPH.height + OFF_Y*2 + GRAPH.box_h + 100;
  svg.setAttribute('width', w); svg.setAttribute('height', h);
  const NS = 'http://www.w3.org/2000/svg';
  
  const defs = document.createElementNS(NS,'defs');
  ['ERROR','CALL','LOOP','NEXT'].forEach(kind=>{
    const color = kind==='NEXT' ? edgeColor('') : edgeColor(kind);
    // Orient horizontal marker arrows safely for clean side entries
    defs.innerHTML += `<marker id="arrow-${kind}" markerWidth="9" markerHeight="9" refX="5" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 Z" fill="${color}"/></marker>`;
  });
  svg.appendChild(defs);

  const bw = GRAPH.box_w, bh = GRAPH.box_h;
  GRAPH.edges.forEach(e=>{
    const a = nodeByKey[e.from], b = nodeByKey[e.to];
    if(!a || !b) return;
    const color = edgeColor(e.kind);
    const path = document.createElementNS(NS,'path');
    let d;

    if(a.key === b.key){
      // Self repeating loops
      const x = a.x+OFF_X+bw, y = a.y+OFF_Y+bh/2;
      d = `M ${x} ${y-8} C ${x+24} ${y-15}, ${x+24} ${y+15}, ${x} ${y+8}`;
    } else if(a.layer === b.layer){
      // Straight sequence steps inside the exact same column
      const x = a.x+OFF_X+bw/2;
      d = `M ${x} ${a.y+OFF_Y+bh} L ${x} ${b.y+OFF_Y}`;
    } else {
      // Orthogonal Routing: Exits box horizontally, drops through channel space, enters target horizontally
      const xStart = (b.layer > a.layer) ? (a.x + OFF_X + bw) : (a.x + OFF_X);
      const yStart = a.y + OFF_Y + bh / 2;
      const xEnd = (b.layer > a.layer) ? (b.x + OFF_X) : (b.x + OFF_X + bw);
      const yEnd = b.y + OFF_Y + bh / 2;
      
      // Determine vertical drop mid-point alignment inside the lane gap space
      const midX = xStart + (xEnd - xStart) * 0.45;
      
      d = `M ${xStart} ${yStart} H ${midX} V ${yEnd} H ${xEnd}`;
    }
    path.setAttribute('d', d);
    path.setAttribute('fill','none');
    path.setAttribute('stroke', color);
    path.setAttribute('stroke-width', e.kind==='ERROR' ? 2.2 : 1.5);
    path.setAttribute('marker-end', `url(#arrow-${e.kind || 'NEXT'})`);
    svg.appendChild(path);
  });
}

function renderFlowDiagram(){
  const canvas = document.getElementById('flowCanvas');
  canvas.querySelectorAll('.flowbox').forEach(b=>b.remove());
  const w = GRAPH.width + OFF_X*2 + GRAPH.box_w, h = GRAPH.height + OFF_Y*2 + GRAPH.box_h + 100;
  canvas.style.width = w+'px'; canvas.style.height = h+'px';

  GRAPH.nodes.forEach(n=>{
    const box = el('div', 'flowbox ' + (n.error?'t-error':(n.kind==='module'?'t-module':'')));
    box.style.left = (n.x+OFF_X)+'px'; box.style.top = (n.y+OFF_Y)+'px';
    box.style.width = GRAPH.box_w+'px'; box.style.height = GRAPH.box_h+'px';
    box.appendChild(el('span','lbl', esc(labelFor(n))));
    box.addEventListener('mouseenter', ()=> { showLogPreview(n); });
    canvas.appendChild(box);
  });
  drawEdges();
  resetView();
}

const view = {scale:1, x:20, y:20};
function applyView(){
  const canvas = document.getElementById('flowCanvas');
  canvas.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.scale})`;
  document.getElementById('zoomLabel').textContent = Math.round(view.scale*100) + '%';
}
function resetView(){
  view.scale = 0.8; view.x = 40; view.y = 40; applyView();
}
function clampScale(s){ return Math.min(3, Math.max(0.15, s)); }

(function setupPanZoom(){
  const wrap = document.getElementById('flowWrap');
  wrap.addEventListener('wheel', (e)=>{
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const newScale = clampScale(view.scale * (e.deltaY < 0 ? 1.1 : 0.9));
    view.x = mx - (mx - view.x) * (newScale/view.scale);
    view.y = my - (my - view.y) * (newScale/view.scale);
    view.scale = newScale;
    applyView();
  }, {passive:false});

  let dragging = false, startX=0, startY=0, origX=0, origY=0;
  wrap.addEventListener('mousedown', (e)=>{
    if(e.target.closest('.flowbox')) return;
    dragging = true; startX = e.clientX; startY = e.clientY; origX = view.x; origY = view.y;
  });
  window.addEventListener('mousemove', (e)=>{
    if(!dragging) return;
    view.x = origX + (e.clientX - startX); view.y = origY + (e.clientY - startY); applyView();
  });
  window.addEventListener('mouseup', ()=>{ dragging=false; });
  document.getElementById('zoomInBtn').addEventListener('click', ()=>{ view.scale = clampScale(view.scale*1.2); applyView(); });
  document.getElementById('zoomOutBtn').addEventListener('click', ()=>{ view.scale = clampScale(view.scale*0.8); applyView(); });
  document.getElementById('zoomResetBtn').addEventListener('click', resetView);
})();

function switchTab(which){
  document.getElementById('tabTreeBtn').classList.toggle('active', which==='tree');
  document.getElementById('tabFlowBtn').classList.toggle('active', which==='flow');
  document.getElementById('viewTree').classList.toggle('active', which==='tree');
  document.getElementById('viewFlow').classList.toggle('active', which==='flow');
  if(which==='flow') renderFlowDiagram();
}
document.getElementById('tabTreeBtn').addEventListener('click', ()=>switchTab('tree'));
document.getElementById('tabFlowBtn').addEventListener('click', ()=>switchTab('flow'));

const s = DATA.stats;
const statsEl = document.getElementById('stats');
[['Lines parsed', s.lines], ['Errors', s.errors], ['Execution Steps', s.steps]].forEach(([k,v])=>statsEl.appendChild(el('div','stat',`<span class="k">${k}</span><span class="v">${v}</span>`)));

const flow = document.getElementById('flow');
DATA.root.children.forEach(c=>flow.appendChild(renderNode(c)));
</script>
</body></html>
'''


def render_html(root, header, error_index, stats, graph_json, out_path):
    annotate_tree(root)
    narrative = build_narrative(root, header, stats, error_index)
    layout = compute_layout(graph_json)
    data = {
        'root': root,
        'header': header,
        'errors': error_index,
        'narrative': narrative,
        'graph': layout,
        'stats': {k: stats.get(k, 0) for k in ('lines', 'steps', 'status', 'modules', 'errors', 'loops', 'iter_saved', 'elapsed', 'bytes')},
    }
    payload = json.dumps(data, separators=(',', ':'))
    out = HTML_TEMPLATE.replace('__DATA_JSON__', payload).replace('__PROGRAM__', html.escape(header.get('program') or 'TRACE'))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(out)
    return narrative


def write_error_file(error_index, path):
    with open(path, 'w', encoding='utf-8') as f:
        for e in error_index:
            f.write(f"[line {e['line']}] {e['message']}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input')
    ap.add_argument('-o', '--output', default=None)
    ap.add_argument('--errors', default=None)
    ap.add_argument('--max-children', type=int, default=DEFAULT_MAX_CHILDREN)
    ap.add_argument('--flush-size', type=int, default=DEFAULT_FLUSH_SIZE)
    ap.add_argument('--max-period', type=int, default=DEFAULT_MAX_PERIOD)
    ap.add_argument('--stats-only', action='store_true')
    args = ap.parse_args()

    out_html = args.output or (args.input + '.report.html')
    out_err = args.errors or (args.input + '.errors.txt')

    root, header, error_index, stats, graph_json = parse_stream(args.input, max_period=args.max_period, flush_size=args.flush_size, progress=False)

    if args.stats_only: return
    cap_children(root, args.max_children)
    render_html(root, header, error_index, stats, graph_json, out_html)
    write_error_file(error_index, out_err)


if __name__ == '__main__':
    main()
