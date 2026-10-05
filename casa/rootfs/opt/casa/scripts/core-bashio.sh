#!/bin/sh
# #1268: the interpreter of every core s6 script
# (`#!/command/with-contenv /opt/casa/scripts/core-bashio.sh`).
#
# setup-configs.sh puts the plugin tools directory first on the PATH of every
# s6 service, and a plugin publishes executables there under any name. So
# before bash, bashio or anything the bashio library and the script body run by
# bare name is looked up, this drops that directory from PATH, by the rule the
# engagement scripts use: the block below is drivers/workspace.py's
# _root_path_fragment() byte for byte (tests/test_core_s6_interpreter.py pins
# that). It refuses with exit 111 if nothing is left.
#
# The inherited PATH stays in _casa_cli_path, exported so it survives the exec
# below: svc-casa, svc-casa-mcp and svc-ttyd hand it back to the program they
# exec last. bashio is run as the base image ships it, with /bin/bash named
# here because its own first line is `#!/usr/bin/env bash`; casa/Dockerfile
# fails the build if /usr/bin/bashio no longer resolves to /usr/lib/bashio.
_casa_cli_path=$PATH
_casa_root_path=
_casa_rest=$PATH:
while [ -n "$_casa_rest" ]; do
  _casa_e=${_casa_rest%%:*}; _casa_rest=${_casa_rest#*:}
  case $_casa_e in /*) ;; *) continue ;; esac
  if [ "$_casa_e" = /config/tools/bin ] || [ "$_casa_e" -ef /config/tools/bin ]; then continue; fi
  _casa_root_path=${_casa_root_path:+$_casa_root_path:}$_casa_e
done
[ -n "$_casa_root_path" ] || { echo "casa: no PATH entry outside the plugin tools dir; refusing to start" >&2; exit 111; }
PATH=$_casa_root_path
export _casa_cli_path
exec /bin/bash /usr/bin/bashio "$@"
