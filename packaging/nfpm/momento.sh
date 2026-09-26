#!/bin/sh
# Momento launcher for distro packages (.deb/.rpm/.pkg.tar.zst built by nfpm).
#
# The Python package lives in /usr/share/momento rather than site-packages:
#  * one noarch package works with any python3 >= 3.11 (no python3.X path),
#  * it cannot collide with the unrelated "momento" SDK on PyPI, whose import
#    name is also `momento`.
# /usr/bin/python3 is used on purpose: it is the interpreter that sees the
# distro's PyGObject, dbus-python and PySide6.
PYTHONPATH="/usr/share/momento${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec /usr/bin/python3 -m momento "$@"
