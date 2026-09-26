#!/bin/sh
# Byte-compile once as root; users can't write __pycache__ under /usr/share.
if [ -x /usr/bin/python3 ]; then
    /usr/bin/python3 -m compileall -q /usr/share/momento/momento >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
