# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    ('email_config.example.json', '.'),
]

if os.path.exists('.env.local'):
    datas.append(('.env.local', '.'))
if os.path.exists('.env'):
    datas.append(('.env', '.'))
if os.path.exists('email_config.json'):
    datas.append(('email_config.json', '.'))

binaries = []
hiddenimports = [
    'main',
    'auth',
    'database',
    'models',
    'sender',
    'email_service',
    'python_multipart',
    'multipart',
    'dotenv',
    'openpyxl',
    'xlrd',
    'sqlite3',
    'sqlalchemy.dialects.postgresql',
    'sqlalchemy.dialects.postgresql.psycopg',
    'sqlalchemy.dialects.sqlite',
]

packages_to_collect = [
    'selenium',
    'uvicorn',
    'anyio',
    'dns',
    'fastapi',
    'starlette',
    'pydantic',
    'jinja2',
    'pandas',
    'openpyxl',
    'sqlalchemy',
    'psycopg',
    'psycopg_binary',
    'alembic',
]

for pkg in packages_to_collect:
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hiddenimports
    except Exception as e:
        print(f"Warning collecting {pkg}: {e}")

icon_path = 'app.ico' if os.path.exists('app.ico') else None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='PayrollSender',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
