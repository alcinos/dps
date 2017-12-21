import argparse

from dps import cfg
from dps.config import DEFAULT_CONFIG
from dps.train import training_loop
from dps.utils import pdb_postmortem
from dps.rl import algorithms as algorithms_module
from dps import envs as envs_module


def parse_env_alg(env, alg=None):
    try:
        env_module = getattr(envs_module, env)
    except AttributeError:
        envs = [e for e in dir(envs_module) if e.startswith(env)]
        assert len(envs) == 1, "Ambiguity in env selection, possibilities are: {}.".format(envs)
        env_module = getattr(envs_module, envs[0])

    env_config = env_module.config

    alg_config = {}

    if alg == "feedforward":
        env_config = env_module.feedforward_config
    else:
        try:
            alg_config = getattr(algorithms_module, alg).config
        except AttributeError:
            algs = [a for a in dir(algorithms_module) if a.startswith(alg)]
            assert len(algs) == 1, "Ambiguity in alg selection, possibilities are: {}.".format(algs)
            alg_config = getattr(algorithms_module, algs[0]).config

    return env_config, alg_config


def run():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('args', nargs='+')
    parser.add_argument('--pdb', action='store_true',
                        help="If supplied, enter post-mortem debugging on error.")
    args, _ = parser.parse_known_args()

    env = args.args[0]
    alg = args.args[1]

    if args.pdb:
        with pdb_postmortem():
            _run(env, alg)
    else:
        _run(env, alg)


def _run(env_str, alg_str, _config=None, **kwargs):
    env_config, alg_config = parse_env_alg(env_str, alg_str)

    config = DEFAULT_CONFIG.copy()
    config.update(alg_config)
    config.update(env_config)

    if _config is not None:
        config.update(_config)
    config.update(kwargs)

    with config:
        cfg.update_from_command_line()

        # Force generator evaluation.
        return list(training_loop())[-1]
