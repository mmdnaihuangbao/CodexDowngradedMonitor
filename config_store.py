"""网页和命令行共用的配置；原子替换避免中途退出留下半个 JSON 文件。"""
import json
import math
import os
import re
import tempfile

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
CONFIG_VERSION = '0.1'
DEFAULTS = {'version': CONFIG_VERSION, 'host': 'localhost', 'port': 48766, 'cpp_port': 48778,
            'expect': '', 'min_interval_ms': 0, 'workers': 4}


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError('配置必须是 JSON 对象')
    config = {**DEFAULTS, **data}
    if config['version'] != CONFIG_VERSION:
        raise ValueError(f"不支持的配置版本：{config['version']}，当前支持 {CONFIG_VERSION}")
    if not isinstance(config['expect'], str):
        raise ValueError('预期模型必须是字符串，可留空')
    config['expect'] = config['expect'].strip()
    if not isinstance(config['host'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', config['host']):
        raise ValueError('监听地址须为 IPv4 地址或主机名，例如 localhost 或 0.0.0.0')
    for key, low, high, label in (('min_interval_ms', 0, 60000, '最小采样间隔'),
                                  ('workers', 1, 16, '并行线程数'), ('port', 1, 65535, 'Python 端口'),
                                  ('cpp_port', 1, 65535, 'C++ 端口')):
        raw = config[key]
        try:
            value = float(raw)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f'{label}必须是有效数字') from None
        if isinstance(raw, bool) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{label}须在 {low}～{high} 之间')
        if key != 'min_interval_ms' and not value.is_integer():
            raise ValueError(f'{label}必须是整数')
        config[key] = int(value) if value.is_integer() else value
    return config


def browser_host(host):
    return 'localhost' if host == '0.0.0.0' else host


def load_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8-sig') as stream:
            return validate_config(json.load(stream))
    except FileNotFoundError:
        return dict(DEFAULTS)


def save_config(updates):
    config = validate_config({**load_config(), **updates})
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=os.path.dirname(CONFIG_PATH),
                                         prefix='.config-', suffix='.tmp', delete=False) as stream:
            path = stream.name
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, CONFIG_PATH)
    finally:
        if path and os.path.exists(path):
            os.unlink(path)
    return config
