#!/usr/bin/env python3
"""从 deploy-static/index.html（真源）生成根 index.html（SCF 同源版）。

转换规则：
  1. 删除 `const API_BASE = '...';` 这一行
  2. 将 `API_BASE + '/api/` 替换为 `'/api/`

根 index.html 仅用于 SCF 直连（同源，已废弃）；sandbox 与 gh-pages 直接用 deploy-static。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "deploy-static", "index.html")
DST = os.path.join(ROOT, "index.html")

with open(SRC, "r", encoding="utf-8") as f:
    html = f.read()

html = re.sub(r"\nconst API_BASE = '[^']*';", "\n", html)
html = html.replace("API_BASE + '/api/", "'/api/")

if "API_BASE" in html:
    print("WARNING: 仍有未处理的 API_BASE 引用")

with open(DST, "w", encoding="utf-8") as f:
    f.write(html)

print("已生成", os.path.relpath(DST, ROOT))
