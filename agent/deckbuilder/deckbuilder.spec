# PyInstaller spec for the theme builder.
#
#     pip install pyinstaller
#     pyinstaller agent/deckbuilder/deckbuilder.spec --noconfirm
#
# Output lands in dist/deckbuilder/, with the intermediate tree in dist/pyi-work/. Neither goes
# anywhere near build/, which is the Arduino toolchain's — a shared name there is a trap for
# whoever next runs --clean.
#
# Nothing is bundled as data, which is a genuinely nice property to have kept: wallpapers are
# resolved from whichever deck.json is open (DeckConfig.asset_root), and the icon font is a
# Windows system font referenced by path. So the exe is code and nothing else.
#
# One-dir, not one-file. --onefile re-extracts about 40MB of Pillow and Tcl/Tk into %TEMP% on
# every launch, which turns a sub-second start into several seconds — and this is a tool you
# open, nudge a colour in, and close. Zip the folder to hand it to someone.

import sys
from pathlib import Path

# SPECPATH is the directory holding this file, not the file itself: agent/deckbuilder -> repo.
REPO = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))


def _icon() -> str | None:
    """Draws the application icon at build time from the tray's own mark.

    Generated rather than checked in, so the builder and the agent cannot end up showing two
    slightly different versions of the same thing in the taskbar.
    """
    target = Path(SPECPATH) / "deckbuilder.ico"
    try:
        from deckhost.tray import make_image

        make_image(True, 256).save(
            target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]
        )
    except Exception as exc:  # a missing icon must not stop a build
        print(f"[spec] no icon ({exc})")
        return None
    return str(target)


ICON = _icon()

a = Analysis(
    [str(REPO / "agent" / "deckbuilder" / "__main__.py")],
    pathex=[str(REPO / "agent")],
    binaries=[],
    datas=[],
    # deckhost is imported lazily in places, and PyInstaller's static analysis does not always
    # follow a `from deckhost import x` inside a function.
    hiddenimports=[
        "deckhost.config",
        "deckhost.protocol",
        "deckhost.assets",
        "deckhost.mdi1",
        "deckhost.images",
        "PIL.ImageTk",
        # deckhost.images defers these to call time so the module stays importable without
        # them; PyInstaller only follows top-level imports, so they are named here instead.
        "PIL.ImageOps",
        "PIL.ImageFilter",
    ],
    hookspath=[],
    runtime_hooks=[],
    # The builder never opens a serial port and never reads a sensor — that is the agent's job,
    # and keeping the two apart is what makes this exe small. deckhost/__init__.py imports no
    # submodules, so excluding the agent's own modules prunes cleanly.
    excludes=[
        "serial",
        "psutil",
        "pystray",
        "pynvml",
        "ahk",
        "numpy",
        "deckhost.main",
        "deckhost.link",
        "deckhost.stats",
        "deckhost.actions",
        "deckhost.power",
        "deckhost.tray",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="deckbuilder",
    debug=False,
    strip=False,
    upx=False,
    # Windowed: there is no console to print to, which is why the app logs to a file and puts
    # everything else in the status banner.
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="deckbuilder",
)
