# bash completion for anirss.
#
# Feed names come from ~/.local/state/anirss/feeds.txt — refreshed on every
# successful qBittorrent login (or `anirss -Sy` to force).

_anirss() {
    local cur prev cache words cword
    _init_completion -n : || return
    cache="${XDG_STATE_HOME:-$HOME/.local/state}/anirss/feeds.txt"

    local ops="-e --endpoint -Q -Qj -S -Sh -Sy -R -Rs -Rn -Rns -Rh -Rsh -Rnh -Rnsh -T -Th --noconfirm --no-seed --config --migrate-config --version -h --help"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "$ops" -- "$cur") )
        return
    fi

    case "${words[1]}" in
        -R|-Rs|-Rn|-Rns|-Rh|-Rsh|-Rnh|-Rnsh)
            if [[ -f $cache ]]; then
                local IFS=$'\n'
                COMPREPLY=( $(compgen -W "$(<"$cache")" -- "$cur") )
            fi
            ;;
        -T|-Th)
            # _filedir is a bash-completion helper that handles paths,
            # spaces, ~, and quoting. Falls back to compgen if missing.
            if declare -F _filedir >/dev/null; then
                _filedir 'torrent'
            else
                COMPREPLY=( $(compgen -f -X '!*.torrent' -- "$cur") )
            fi
            ;;
    esac
}
complete -F _anirss anirss
