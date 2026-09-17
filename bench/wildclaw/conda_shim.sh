#!/bin/sh
# A minimal `conda`, installed at /root/miniconda3/bin/conda by
# bench/wildclaw/Dockerfile. It is a SHIM, not conda: there is no solver and
# no channel here, and the `eval` "environment" is a plain venv at
# /root/miniconda3/envs/eval.
#
# Why it exists: three Code Intelligence task prompts describe that path as a
# conda environment, so an agent that reads them reaches for
# `conda activate eval` and gets `sh: conda: not found` — which reads as "this
# environment does not exist" rather than "call its interpreter directly".
# What it answers for real is `conda run -n <env> <cmd>` and the env listings;
# everything else exits non-zero naming the interpreter path, because that is
# the actionable answer and because a shim that pretends to solve dependencies
# would be worse than no shim at all.
#
# `conda activate` failing here is faithful, not a shortcut: in a real
# installation that has not been through `conda init`, `conda activate` fails
# the same way — a subprocess cannot change its parent shell's environment.
set -u

ENVS_DIR=/root/miniconda3/envs

usage() {
    echo "conda: this image ships a shim, not conda (bench/wildclaw/conda_shim.sh)." >&2
    echo "Run the environment interpreter directly:" >&2
    echo "    /root/miniconda3/envs/eval/bin/python" >&2
    echo "    /root/miniconda3/envs/eval/bin/pip" >&2
    echo "or use:  conda run -n eval <command>" >&2
    exit 1
}

[ $# -ge 1 ] || usage

case "$1" in
    run)
        shift
        env_name=base
        while [ $# -gt 0 ]; do
            case "$1" in
                -n|--name)
                    [ $# -ge 2 ] || usage
                    env_name=$2
                    shift 2
                    ;;
                -p|--prefix)
                    [ $# -ge 2 ] || usage
                    ENVS_DIR=$(dirname "$2")
                    env_name=$(basename "$2")
                    shift 2
                    ;;
                --no-capture-output|--live-stream)
                    shift
                    ;;
                *)
                    break
                    ;;
            esac
        done
        [ $# -ge 1 ] || usage
        if [ ! -d "$ENVS_DIR/$env_name" ]; then
            echo "conda: no such environment: $env_name" >&2
            exit 1
        fi
        VIRTUAL_ENV="$ENVS_DIR/$env_name"
        PATH="$VIRTUAL_ENV/bin:$PATH"
        export VIRTUAL_ENV PATH
        exec "$@"
        ;;
    info|env)
        # `conda info --envs` and `conda env list` — the two spellings of the
        # same question, answered with the same two columns conda uses.
        for candidate in "$ENVS_DIR"/*; do
            [ -d "$candidate" ] || continue
            echo "$(basename "$candidate")            $candidate"
        done
        exit 0
        ;;
    *)
        usage
        ;;
esac
