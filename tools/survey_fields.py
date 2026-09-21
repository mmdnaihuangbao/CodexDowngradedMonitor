"""只读扫描 Codex JSON 字段；默认只输出字段统计，--examples 可保存本地私有真实样例。"""
import argparse
import ctypes
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collector import ProcessScanner, MEMORY_BASIC_INFORMATION, MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS
from history_store import validation_reason
from process_discovery import enumerate_codex

# 每块拥有 1 MiB 起始地址，额外读取 1 MiB，以覆盖跨块对象；不保存原始内存。
CHUNK = 1 << 20
ANCHOR = re.compile(rb'\{\s*"(?:id|object|type|model|previous_response_id|store|stream|client_metadata)"\s*:')
MARKERS = ('safety_identifier', 'frequency_penalty', 'presence_penalty', 'completed_at', 'max_output_tokens')


class SurveyScanner(ProcessScanner):
    CHUNK = 2 * CHUNK

    def regions(self):
        address = 0
        while True:
            info = MEMORY_BASIC_INFORMATION()
            if not self.k32.VirtualQueryEx(self.h, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)):
                return
            base, size = info.BaseAddress or 0, info.RegionSize
            if not size or base + size <= address:
                return
            if (info.State == MEM_COMMIT and info.Type & self.SCAN_TYPES
                    and info.Protect & self.READABLE_PROTECT and not info.Protect & (PAGE_GUARD | PAGE_NOACCESS)):
                yield base, size
            address = base + size


def paths(value, path='', out=None, samples=None, pointer=''):
    if out is None:
        out = {}
    kind = 'null' if value is None else type(value).__name__
    types, nonempty = out.setdefault(path, (set(), False))
    types.add(kind)
    out[path] = (types, nonempty or (value is not None and value != '' and value != [] and value != {}))
    if samples is not None:
        has_value = value is not None and value != '' and value != [] and value != {}
        size = len(json.dumps(value, ensure_ascii=False))
        old = samples.get(path)
        # 优先非空样例，同等情况下选较短的真实值；不截断 JSON 中的原值。
        if old is None or (has_value, -size) > (old['example_nonempty'], -old['example_size']):
            samples[path] = {'example': value, 'example_pointer': pointer,
                             'example_nonempty': has_value, 'example_size': size}
    if isinstance(value, dict):
        for key, child in value.items():
            # 自定义元数据/Schema 字段名也可能含业务信息，只保留它们的结构。
            name = '*' if path.endswith(('.metadata', '.properties', '.headers')) or path in ('metadata', 'usage.attribution.items') else key
            if re.search(r'[0-9a-f]{24,}|[0-9a-f]{8}-[0-9a-f]{4}-', name, re.I):
                name = '*'
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9-]{0,79}|\*', name):
                name = '*'
            child_pointer = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
            paths(child, path + '.' + name if path else name, out, samples, child_pointer)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths(child, path + '[]', out, samples, pointer + '/' + str(index))
    elif isinstance(value, str) and path == 'client_metadata.x-codex-turn-metadata':
        # 这个固定协议字段是字符串中的 JSON；不把任意正文/工具参数当 JSON 解析。
        try:
            nested = json.loads(value)
        except ValueError:
            nested = None
        if isinstance(nested, dict):
            paths(nested, path + '.$json', out, samples, pointer + '/$json')
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='汇总 JSON 路径；默认不保存字段值，--examples 时附带原值')
    parser.add_argument('--examples', action='store_true', help='保存真实字段样例；输出必须位于 Git 忽略的 _verify 目录')
    args = parser.parse_args()
    if args.examples:
        root = pathlib.Path(__file__).resolve().parents[1]
        output = pathlib.Path(args.output).resolve()
        private = root / '_verify'
        if private not in output.parents:
            parser.error('真实样例只能写到仓库 _verify 目录内')
        relative = output.relative_to(root).as_posix()
        ignored = subprocess.run(['git', 'check-ignore', '-q', '--', relative], cwd=root)
        tracked = subprocess.run(['git', 'ls-files', '--', relative], cwd=root, capture_output=True, text=True)
        if ignored.returncode != 0 or tracked.returncode != 0 or tracked.stdout.strip():
            parser.error('输出必须已被 Git 忽略且未被跟踪')
    report = {'observed_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'processes': [], 'groups': {}}
    seen = set()
    decoder = json.JSONDecoder()
    for candidate in enumerate_codex():
        if candidate['name'] != 'codex.exe':
            continue
        scanner = SurveyScanner(candidate['pid'])
        stats = {'pid': candidate['pid'], 'regions': 0, 'bytes': 0, 'unreadable_chunks': 0, 'json_decode_failures': 0}
        report['processes'].append(stats)
        if not scanner.open():
            stats['denied'] = True
            continue
        started = time.monotonic()
        try:
            for base, size in scanner.regions():
                stats['regions'] += 1
                for off in range(0, size, CHUNK):
                    data = scanner.read(base + off, min(2 * CHUNK, size - off))
                    stats['bytes'] += len(data)
                    if not data:
                        stats['unreadable_chunks'] += 1
                        continue
                    if b'"model"' not in data:
                        continue
                    for match in ANCHOR.finditer(data, 0, min(CHUNK, len(data))):
                        blob = data[match.start():match.start() + CHUNK]
                        # 提前排除普通嵌套对象；请求模型可能位于长 input 后面。
                        if b'resp_' not in blob[:256] and b'response.create' not in blob[:256] and b'"model"' not in blob[:256]:
                            continue
                        try:
                            obj, end = decoder.raw_decode(blob.decode('utf-8', 'replace'))
                        except (ValueError, RecursionError):
                            stats['json_decode_failures'] += 1
                            continue
                        if not isinstance(obj, dict) or not isinstance(obj.get('model'), str):
                            continue
                        group = None
                        if obj.get('type') == 'response.create':
                            group = 'websocket_requests'
                        elif str(obj.get('id', '')).startswith('resp_'):
                            marks = sum(k in obj for k in MARKERS) + (obj.get('object') == 'response')
                            if marks >= 2 and 'client_metadata' not in obj:
                                group = 'suspect_responses' if validation_reason(obj) else 'responses'
                        elif 'input' in obj and ('stream' in obj or 'store' in obj):
                            group = 'request_candidates'
                        if group is None:
                            continue
                        digest = hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).digest()
                        if (group, digest) in seen:
                            continue
                        seen.add((group, digest))
                        dest = report['groups'].setdefault(group, {'objects': 0, 'fields': {}})
                        dest['objects'] += 1
                        samples = {} if args.examples else None
                        for path, (types, nonempty) in paths(obj, samples=samples).items():
                            if not path:
                                continue
                            field = dest['fields'].setdefault(path, {'present': 0, 'nonempty': 0, 'types': []})
                            field['present'] += 1
                            field['nonempty'] += int(nonempty)
                            field['types'] = sorted(set(field['types']) | types)
                            if samples is not None:
                                sample = samples[path]
                                if 'example' not in field or (sample['example_nonempty'], -sample['example_size']) > (field['example_nonempty'], -field['example_size']):
                                    field.update(sample)
                                    field['example_object'] = dest['objects']
                                    field['example_pid'] = candidate['pid']
        finally:
            scanner.close()
        stats['seconds'] = round(time.monotonic() - started, 3)
    pathlib.Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'processes': report['processes'], 'groups': {k: {'objects': v['objects'], 'field_paths': len(v['fields'])} for k, v in report['groups'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
