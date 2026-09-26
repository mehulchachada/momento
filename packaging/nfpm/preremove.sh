#!/bin/sh
# Remove the bytecode written by postinstall — but not during an upgrade, where
# (rpm: $1=1, deb: "upgrade") the new version's postinstall has already run.
case "${1:-}" in
    1|upgrade|failed-upgrade) exit 0 ;;
esac
rm -rf /usr/share/momento/momento/__pycache__
exit 0
