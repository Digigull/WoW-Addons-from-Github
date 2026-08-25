"""PyInstaller's entry point for the Windows build.

Deliberately not `addons.py`. That file exists to make a checkout runnable and
spends its body putting the repository root on sys.path -- which is exactly the
wrong thing inside a frozen build, where the package is already bundled and
`__file__` points into an unpack directory. This does the one thing the .exe
needs and nothing else.
"""

from wowaddons.__main__ import main

if __name__ == "__main__":
    main()
