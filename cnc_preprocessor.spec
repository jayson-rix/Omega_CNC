# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Omega CNC Processor
# Run on Windows: pyinstaller cnc_preprocessor.spec

import customtkinter
import os

# customtkinter bundles its own theme assets — they must be included
_ctk_path = os.path.dirname(customtkinter.__file__)

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('config.json',       '.'),
        ('tools.json',        '.'),
        ('omega_logo.jpg',    '.'),
        ('footer_banner.jpg', '.'),
        (_ctk_path, 'customtkinter'),
    ],
    hiddenimports=[
        'customtkinter',
        'tkinter',
        'tkinter.filedialog',
        'tkinter.messagebox',
        'xml.etree.ElementTree',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'ezdxf',
        'ezdxf.disassemble',
        'ezdxf.recover',
        'packaging',
        'packaging.version',
        'packaging.specifiers',
    ],
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
    [],
    exclude_binaries=True,
    name='Omega_CNC_Processor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',   # Uncomment and add icon.ico to enable a custom icon
)

smoke_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Omega_CNC_Processor_Smoke',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    smoke_exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Omega_CNC_Processor',
)
