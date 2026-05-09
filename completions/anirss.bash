# bash completion for anirss.
#
# Feed names come from ~/.local/state/anirss/feeds.txt — refreshed on every
# successful qBittorrent login (or `anirss -Sy` to force).

_anirss() {
    local cur prev cache words cword
    _init_completion -n : || return
    cache="${XDG_STATE_HOME:-$HOME/.local/state}/anirss/feeds.txt"

    local ops="-Q -Qj -S -Sy -R -Rs -Rn -Rns --noconfirm --no-seed --config --migrate-config --version -h --help"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$ops" -- "$cur") )
        return
    fi

    case "${words[1]}" in
        -R|-Rs|-Rn|-Rns)
            if [[ -f $cache ]]; then
                local IFS=$'\n'
                COMPREPLY=( $(compgen -W "$(<"$cache")" -- "$cur") )
            fi
            ;;
    esac
}
complete -F _anirss anirss
