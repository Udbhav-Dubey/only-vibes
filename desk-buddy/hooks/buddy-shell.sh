# desk-buddy shell hook (bash + zsh). Add to ~/.bashrc or ~/.zshrc:
#     source /path/to/desk-buddy/hooks/buddy-shell.sh
# After every command it tells Buddy the command line + exit code. Buddy only classifies it
# (git commit / force push, make / cargo / pytest / npm test..., plain errors) and never stores it.
: "${BUDDY_URL:=http://127.0.0.1:3030}"

_buddy_send_cmd() {   # $1 = command line, $2 = exit code, $3 = seconds it took
  command -v curl >/dev/null 2>&1 || return 0
  local line=${1:0:300}
  line=${line//\\/\\\\}; line=${line//\"/\\\"}
  line=${line//$'\t'/ }; line=${line//$'\n'/ }; line=${line//$'\r'/ }
  ( curl -s -m 1 -o /dev/null -H 'Content-Type: application/json' \
      -d "{\"type\":\"cmd\",\"code\":$2,\"secs\":${3:-0},\"line\":\"$line\"}" "$BUDDY_URL/event" >/dev/null 2>&1 & )
}

if [ -n "${ZSH_VERSION:-}" ]; then
  _buddy_preexec() { _buddy_line=$1; _buddy_t0=$SECONDS; }
  _buddy_precmd() {
    local code=$?
    [[ -n ${_buddy_line:-} ]] && _buddy_send_cmd "$_buddy_line" "$code" $(( SECONDS - ${_buddy_t0:-$SECONDS} ))
    _buddy_line=
    return $code
  }
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec _buddy_preexec
  precmd_functions=(_buddy_precmd ${precmd_functions:#_buddy_precmd})   # run first, so $? is intact

elif [ -n "${BASH_VERSION:-}" ]; then
  # A DEBUG trap notices that a command really ran (even when HISTCONTROL=ignoredups hides repeats
  # from history, e.g. running `make` twice). If something else already owns the DEBUG trap we
  # leave it alone and fall back to history only.
  _buddy_dbg() {
    case $BASH_COMMAND in _buddy_*) return 0;; esac
    if [ -n "${_buddy_armed:-}" ]; then _buddy_armed=; _buddy_ran=1; _buddy_cmd=$BASH_COMMAND; _buddy_t0=$SECONDS; fi
  }
  _buddy_arm() { _buddy_armed=1; }
  _buddy_post() {
    local code=$? h n line=
    _buddy_armed=
    h=$(HISTTIMEFORMAT= history 1); read -r n line <<<"$h"
    if [ "$n" = "${_buddy_last_n:-}" ]; then line=; else _buddy_last_n=$n; fi
    if [ -n "${_buddy_ready:-}" ] && { [ -n "${_buddy_ran:-}" ] || [ -n "$line" ]; }; then
      _buddy_send_cmd "${line:-$_buddy_cmd}" "$code" $(( SECONDS - ${_buddy_t0:-$SECONDS} ))
    fi
    _buddy_ran=; _buddy_ready=1
    return $code
  }
  if [ -z "$(trap -p DEBUG)" ]; then trap '_buddy_dbg' DEBUG; fi
  case ";${PROMPT_COMMAND:-};" in
    *";_buddy_post;"*) ;;
    *) PROMPT_COMMAND="_buddy_post${PROMPT_COMMAND:+;$PROMPT_COMMAND};_buddy_arm" ;;
  esac
fi
