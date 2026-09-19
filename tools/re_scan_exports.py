#!/usr/bin/env python3
"""P0 闸门: 扫描剪映 Windows 引擎 DLL 导出表, 找 jianying-headless 参考地图里的关键符号.

用法: python3 tools/re_scan_exports.py [videoeditor.dll VECreator.dll ...]
不给参数则扫描仓库根的已知引擎 DLL.

判读:
- videoeditor.dll 命中 lvve/lyra/Deserializer/DraftInit/restore/exportStart => API 渲染路线可行, 进 P1
- 全部 miss => 止损, UI 自动化维持现状
"""
import sys
import os
import re
import pefile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# jianying-headless (macOS libvideoeditor.dylib) 参考地图上的关键 API 家族.
# Windows 侧 mangled 名形态: ?restore_draft@Server@lyra@@... 或 ?xxx@lvve@@...
TARGETS = [
    # 家族/类名 (导出符号子串匹配)
    ('lvve::',            r'@lvve@'),
    ('lyra::',            r'@lyra@'),
    ('Deserializer',      r'Deserializer'),
    ('GetDraftFromJson',  r'GetDraftFromJson'),
    ('GetJsonFromDraft',  r'GetJsonFromDraft'),
    ('DraftInit',         r'DraftInit'),
    ('restore_draft',     r'restore_draft'),
    ('openSession',       r'openSession'),
    ('closeSession',      r'closeSession'),
    ('invokeSync',        r'invokeSync'),
    ('ExportService',     r'ExportService'),
    ('exportStart',       r'exportStart'),
    ('ExportClient',      r'ExportClient'),
    ('Server@lyra',       r'Server@lyra'),
    ('ProjectClient',     r'ProjectClient'),
    ('deserialize_persistent_draft', r'deserialize_persistent_draft'),
]

DEFAULT_DLLS = ['videoeditor.dll', 'VECreator.dll', 'VETextTemplateCreator.dll',
                'renderkit_windows.dll', 'VESafeGuard.dll', 'VEConfig.dll']


def scan(path):
    """返回 (命中表 {target: [符号,...]}, 导出总数, 是否有导出表)."""
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
    hits = {label: set() for label, _ in TARGETS}
    total = 0
    has_export = hasattr(pe, 'DIRECTORY_ENTRY_EXPORT')
    if has_export:
        for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            name = sym.name.decode('utf-8', 'replace') if sym.name else ''
            if not name:
                continue
            total += 1
            for label, pat in TARGETS:
                if re.search(pat, name):
                    hits[label].add(name)
    pe.close()
    return hits, total, has_export


def main():
    dlls = sys.argv[1:] or [os.path.join(HERE, d) for d in DEFAULT_DLLS
                            if os.path.exists(os.path.join(HERE, d))]
    if not dlls:
        print('未找到待扫描 DLL; 把剪映引擎 DLL 放到仓库根或传参指定路径')
        return 2
    verdict_gate = False
    for path in dlls:
        name = os.path.basename(path)
        size_mb = os.path.getsize(path) / 1048576
        try:
            hits, total, has_export = scan(path)
        except Exception as e:
            print(f'\n== {name} ({size_mb:.1f} MB) — 解析失败: {e}')
            continue
        print(f'\n== {name} ({size_mb:.1f} MB)  导出符号 {total} 个, 导出表存在: {has_export}')
        if not has_export:
            print('   (无导出表 — 可能 VMProtect 壳或仅序号导出)')
            continue
        for label, _ in TARGETS:
            syms = sorted(hits[label])
            if syms:
                star = ' *闸门*' if label in ('lvve::', 'lyra::', 'DraftInit',
                                              'restore_draft', 'GetDraftFromJson') else ''
                print(f'   [{label}] {len(syms)} 命中{star}')
                for s in syms[:12]:
                    print(f'      {s[:180]}')
                if len(syms) > 12:
                    print(f'      ... 共 {len(syms)} 个')
                if label in ('lvve::', 'lyra::', 'DraftInit', 'restore_draft', 'GetDraftFromJson'):
                    verdict_gate = True
    print('\n---- P0 结论 ----')
    if verdict_gate:
        print('闸门命中: 引擎层草稿装载/导出符号在导出表上可见 => API 渲染路线继续, 进 P1 运行时侦察')
    else:
        print('闸门未命中: 参考地图上的关键符号不在导出表 (需深入 .rdata/反汇编定位), 建议先评估成本')
    return 0


if __name__ == '__main__':
    sys.exit(main())
