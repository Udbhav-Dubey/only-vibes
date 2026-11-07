# desk-buddy shell hook (bash + zsh). Add to ~/.bashrc or ~/.zshrc:
#     source ~/dev/only-vibes/desk-buddy/terminal-hook.sh
# After every command it tells Buddy the exit code and how long it ran. Build/test commands
# (make, cargo, gcc, pytest, npm test ...) become pass/fail reactions; other non-zero exits make it flinch.
# Only the command's first 60 characters go to the local server, which classifies it and does not
# forward it to the phone.
_buddy_url="${BUDDY_URL:-http://127.0.0.1:3030}"
_buddy_send() {   # line code secs
  local l="${1:0:60}"
  l="${l//\\/\\\\}"; l="${l//\"/\\\"}"; l="${l//$'\n'/ }"; l="${l//$'\t'/ }"
  ( curl -s -m 1 -X POST -H 'Content-Type: application/json' \
      -d "{\"type\":\"cmd\",\"line\":\"$l\",\"code\":$2,\"secs\":$3}" "$_buddy_url/event" >/dev/null 2>&1 & )
}
if [ -n "${ZSH_VERSION:-}" ]; then
  _buddy_preexec() { _bl="$1"; _bt=$SECONDS; }
  _buddy_precmd()  { local c=$?; [[ -n ${_bl:-} ]] && _buddy_send "$_bl" "$c" "$((SECONDS - _bt))"; _bl=; return $c; }
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec _buddy_preexec
  precmd_functions=(_buddy_precmd ${precmd_functions:#_buddy_precmd})   # first, so $? is intact
elif [ -n "${BASH_VERSION:-}" ]; then
  _buddy_dbg() {
    [[ -n ${_bl:-} || $BASH_COMMAND == _buddy_* || $BASH_COMMAND == *PROMPT_COMMAND* ]] && return
    _bl=$BASH_COMMAND; _bt=$SECONDS
  }
  _buddy_prompt() { local c=$?; [[ -n ${_bl:-} ]] && _buddy_send "$_bl" "$c" "$((SECONDS - _bt))"; _bl=; return $c; }
  trap '_buddy_dbg' DEBUG
  PROMPT_COMMAND="_buddy_prompt${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
fi
