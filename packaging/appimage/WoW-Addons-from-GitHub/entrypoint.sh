#! /bin/bash
# {{ python-executable }} is NOT the bare interpreter: it is the wrapper script
# python-appimage installs at usr/bin, which exports TCL_LIBRARY, TK_LIBRARY and
# TKPATH for the bundled Tcl/Tk before handing over. Calling opt/.../bin/python
# directly would start fine and then fail the moment the window is opened, which
# is the worst possible time to find out. Use the wrapper.
exec {{ python-executable }} -m wowaddons "$@"
