import numpy as np
import tensorflow as tf
import abc
from contextlib import ExitStack
import time
from collections import defaultdict

from dps import cfg
from dps.utils import Param, Parameterized, shift_fill
from dps.utils.tf import masked_mean, tf_discount_matrix, build_scheduled_value, get_scheduled_values
from dps.updater import Updater


def rl_render_hook(updater):
    if hasattr(updater, 'learners'):
        render_rollouts = getattr(cfg, 'render_rollouts', None)
        for learner in updater.learners:
            with learner:
                updater.env.visualize(
                    policy=learner.pi,
                    n_rollouts=cfg.render_n_rollouts, T=cfg.T, mode='val',
                    render_rollouts=render_rollouts)
    else:
        print("Not rendering.")


class RLObject(object, metaclass=abc.ABCMeta):
    def __new__(cls, *args, **kwargs):
        new = super(RLObject, cls).__new__(cls)

        current_context = get_active_context()
        if current_context is not None:
            current_context.add_rl_object(new)
        return new

    def __init__(self, name=None):
        self.name = name or self.__class__.__name__

    def build_core_signals(self, context):
        # "core" signals are those which are generated before all other signals.
        # We can think of them as "leaves" in a tree where the nodes are signals,
        # edges are ops. They must not depend on signals built by other RLObject
        # instances. Not all leaves have to be created in "build_core_signals",
        # but doing so can can ensure the signal is not built in a weird context
        # (e.g. tf.while or tf.cond) which can cause problems.
        pass

    def generate_signal(self, signal_key, context):
        pass

    def pre_update(self, feed_dict, context):
        pass

    def post_update(self, feed_dict, context):
        pass

    def pre_eval(self, feed_dict, context):
        pass

    def post_eval(self, feed_dict, context):
        pass


class ObjectiveFunctionTerm(RLObject):
    def __init__(self, *, use_weights=False, weight=1.0, name=None):
        self.use_weights = use_weights
        self.weight_schedule = weight
        super(ObjectiveFunctionTerm, self).__init__(name)

    def build_core_signals(self, context):
        self.weight = build_scheduled_value(self.weight_schedule, "{}-weight".format(self.name))

    @abc.abstractmethod
    def build_graph(self, context):
        pass


def get_active_context():
    if RLContext.active_context is None:
        raise Exception("No context is currently active.")
    return RLContext.active_context


class RLContext(Parameterized):
    active_context = None

    replay_updates_per_sample = Param(1)
    on_policy_updates = Param(True)

    def __init__(self, gamma, name=""):
        self.mu = None
        self.gamma = gamma
        self.name = name
        self.terms = []
        self.plugins = []
        self._signals = {}
        self.optimizer = None
        self.train_recorded_values = {}
        self.recorded_values = {}
        self.update_batch_size = None
        self.replay_buffer = None
        self.objective_fn_terms = []
        self.agents = []
        self.rl_objects = []

    def __enter__(self):
        if RLContext.active_context is not None:
            raise Exception("May not have multiple instances of RLContext active at once.")
        RLContext.active_context = self
        return self

    def __exit__(self, type_, value, tb):
        RLContext.active_context = None

    def trainable_variables(self, for_opt):
        return [v for agent in self.agents for v in agent.trainable_variables(for_opt=for_opt)]

    def add_rl_object(self, obj):
        if isinstance(obj, ObjectiveFunctionTerm):
            self.objective_fn_terms.append(obj)
        from dps.rl.agent import Agent
        if isinstance(obj, Agent):
            self.agents.append(obj)
        assert isinstance(obj, RLObject)
        self.rl_objects.append(obj)

    def set_behaviour_policy(self, mu):
        self.mu = mu

    def set_validation_policy(self, pi):
        self.pi = pi

    def set_optimizer(self, opt):
        self.optimizer = opt

    def set_replay_buffer(self, update_batch_size, replay_buffer):
        self.update_batch_size = update_batch_size
        self.replay_buffer = replay_buffer

    def add_agent(self, agent):
        self.agents.append(agent)

    def add_recorded_value(self, name, value, train_only=False):
        if not train_only:
            self.recorded_values[name] = value
        self.train_recorded_values[name] = value

    def add_recorded_values(self, d=None, train_only=False, **kwargs):
        if not train_only:
            self.recorded_values.update(d or {}, **kwargs)
        self.train_recorded_values.update(d or {}, **kwargs)

    def set_mode(self, mode):
        for obj in self.rl_objects:
            if hasattr(obj, 'set_mode'):
                obj.set_mode(mode)

    def build_graph(self, env):
        self.env = env
        self.obs_shape = env.obs_shape
        self.action_shape = env.action_shape

        with ExitStack() as stack:
            if self.name:
                stack.enter_context(tf.name_scope(self.name))

            stack.enter_context(self)

            self.build_core_signals()

            objective = None
            for term in self.objective_fn_terms:
                if objective is None:
                    objective = term.weight * term.build_graph(self)
                else:
                    objective += term.weight * term.build_graph(self)
            self.objective = objective
            self.add_recorded_values(rl_objective=self.objective)

            self.optimizer.build_update(self)

            self.add_recorded_values(get_scheduled_values())

    def build_core_signals(self):
        self._signals['mask'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_mask")
        self._signals['done'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_done")

        self._signals['all_obs'] = tf.placeholder(
            tf.float32, shape=(cfg.T+1 if cfg.T is not None else None, None) + self.obs_shape, name="_all_obs")

        # observations that we learn about
        self._signals['obs'] = tf.identity(self._signals['all_obs'][:-1, ...], name="_obs")

        # observations that we use as targets
        self._signals['target_obs'] = tf.identity(self._signals['all_obs'][1:, ...], name="_target_obs")

        self._signals['actions'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None) + self.action_shape, name="_actions")
        self._signals['gamma'] = tf.constant(self.gamma)
        self._signals['batch_size'] = tf.shape(self._signals['obs'])[1]
        self._signals['batch_size_float'] = tf.cast(self._signals['batch_size'], tf.float32)

        self._signals['rewards'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_rewards")
        self._signals['returns'] = tf.cumsum(
            self._signals['rewards'], axis=0, reverse=True, name="_returns")
        self._signals['reward_per_ep'] = tf.reduce_mean(
            tf.reduce_sum(self._signals['rewards'], axis=0), name="_reward_per_ep")

        self.add_recorded_values(reward_per_ep=self._signals['reward_per_ep'])

        self._signals['mode'] = tf.placeholder(tf.string, ())

        self._signals['weights'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_weights")

        T = tf.shape(self._signals['mask'])[0]
        discount_matrix = tf_discount_matrix(self.gamma, T)
        discounted_returns = tf.tensordot(
            discount_matrix, self._signals['rewards'], axes=1, name="_discounted_returns")
        self._signals['discounted_returns'] = discounted_returns

        mean_returns = masked_mean(discounted_returns, self._signals['mask'], axis=1, keepdims=True)
        mean_returns += tf.zeros_like(discounted_returns)
        self._signals['average_discounted_returns'] = mean_returns

        # off-policy
        self._signals['mu_utils'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None,) + self.mu.param_shape, name="_mu_log_probs")
        self._signals['mu_exploration'] = tf.placeholder(
            tf.float32, shape=(None,), name="_mu_exploration")
        self._signals['mu_log_probs'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_mu_log_probs")

        for obj in self.rl_objects:
            obj.build_core_signals(self)

    @staticmethod
    def at_least_3d(array):
        array = np.array(array)
        if array.ndim < 2:
            raise Exception("Array has shape {}".format(array.shape))
        if array.ndim == 2:
            array = array[..., None]
        return array

    def make_feed_dict(self, rollouts, mode, weights=None):
        if weights is None:
            weights = np.ones((rollouts.T, rollouts.batch_size, 1))
        elif weights.ndim == 1:
            weights = np.tile(weights.reshape(1, -1, 1), (rollouts.T, 1, 1))

        feed_dict = {
            self._signals['done']: self.at_least_3d(rollouts['done']),
            self._signals['mask']: self.at_least_3d((1-shift_fill(rollouts['done'], 1)).astype('f')),
            self._signals['all_obs']: self.at_least_3d(rollouts.o),
            self._signals['actions']: self.at_least_3d(rollouts.a),
            self._signals['rewards']: self.at_least_3d(rollouts.r),
            self._signals['weights']: self.at_least_3d(weights),
            self._signals['mu_log_probs']: self.at_least_3d(rollouts.log_probs),
            self._signals['mode']: mode,
        }

        if hasattr(rollouts, 'utils'):
            # utils are not always stored in the rollouts as they can occupy a lot of memory
            feed_dict.update({
                self._signals['mu_utils']: self.at_least_3d(rollouts.utils),
                self._signals['mu_exploration']: rollouts.get_static('exploration')
            })

        return feed_dict

    def get_signal(self, key, generator=None, gradient=False, masked=True, memoize=True, **kwargs):
        """ Memoized signal retrieval and generation. """
        if generator is None:
            signal = self._signals[key]
        else:
            try:
                gen_key = hash(generator)
            except TypeError:
                gen_key = id(generator)
            gen_key = str(gen_key)
            signal_key = key
            key = [gen_key, signal_key]

            for k in sorted(kwargs):
                key.append("{}={}".format(k, kwargs[k]))

            key = '|'.join(key)

            if memoize:
                signal = self._signals.get(key, None)
                if signal is None:
                    signal = generator.generate_signal(signal_key, self, **kwargs)
                    self._signals[key] = signal
            else:
                signal = generator.generate_signal(signal_key, self, **kwargs)

        maskable = len(signal.shape) >= 2
        if masked and maskable:
            mask = self._signals['mask']
            diff = len(signal.shape) - len(mask.shape)
            if diff > 0:
                new_shape = tf.concat([tf.shape(mask), [1] * diff], axis=0)
                mask = tf.reshape(mask, new_shape)
            signal *= mask

        if not gradient:
            signal = tf.stop_gradient(signal)

        return signal

    def _run_and_record(self, rollouts, mode, weights, do_update):
        sess = tf.get_default_session()
        feed_dict = self.make_feed_dict(rollouts, mode, weights)
        self.set_mode(mode)

        for obj in self.rl_objects:
            if do_update:
                obj.pre_update(feed_dict, self)
            else:
                obj.pre_eval(feed_dict, self)

        if do_update:
            recorded_values = self.optimizer.update(rollouts.batch_size, feed_dict, self.train_recorded_values)
        else:
            recorded_values = sess.run(self.recorded_values, feed_dict=feed_dict)

        for obj in self.rl_objects:
            if do_update:
                obj.post_update(feed_dict, self)
            else:
                obj.post_eval(feed_dict, self)

        return recorded_values

    def update(self, batch_size):
        assert self.mu is not None, "A behaviour policy must be set using `set_behaviour_policy` before calling `update`."
        assert self.optimizer is not None, "An optimizer must be set using `set_optimizer` before calling `update`."

        with self:
            start = time.time()
            rollouts = self.env.do_rollouts(self.mu, n_rollouts=batch_size, T=cfg.T, mode='train')
            train_rollout_duration = time.time() - start

            train_record = {}

            start = time.time()
            do_update = self.replay_buffer is None or self.on_policy_updates
            train_record = self._run_and_record(rollouts, mode='train', weights=None, do_update=do_update)

            train_step_duration = time.time() - start
            train_record.update(step_duration=train_step_duration, rollout_duration=train_rollout_duration)

            off_policy_record = {}

            if self.replay_buffer is not None:
                start = time.time()

                self.replay_buffer.add_rollouts(rollouts)
                for i in range(self.replay_updates_per_sample):
                    off_policy_rollouts, weights = self.replay_buffer.get_batch(self.update_batch_size)
                    if off_policy_rollouts is None:
                        # Most common reason for `rollouts` being None
                        # is there not being enough experiences in replay memory.
                        break

                    off_policy_record = self._run_and_record(
                        off_policy_rollouts, mode='off_policy', weights=weights, do_update=True)

                off_policy_duration = time.time() - start
                off_policy_record['step_duration'] = off_policy_duration

            return train_record, off_policy_record

    def evaluate(self, batch_size, mode):
        assert self.pi is not None, "A validation policy must be set using `set_validation_policy` before calling `evaluate`."

        with self:
            start = time.time()
            rollouts = self.env.do_rollouts(self.pi, n_rollouts=batch_size, T=cfg.T, mode=mode)
            eval_rollout_duration = time.time() - start

            start = time.time()
            eval_record = self._run_and_record(rollouts, mode=mode, weights=None, do_update=False)
            eval_duration = time.time() - start

            eval_record.update(
                eval_duration=eval_duration, rollout_duration=eval_rollout_duration)

        return eval_record


class RLUpdater(Updater):
    """ Update parameters of objects (mainly policies and value functions)
        based on sequences of interactions between a behaviour policy and
        an environment.

    Must be used in context of a default graph, session and config.

    Parameters
    ----------
    env: gym Env
        The environment we're trying to learn about.
    learners: RLContext instance or list thereof
        Objects that learn from the trajectories.

    """
    stopping_criteria = "reward_per_ep,max"

    def __init__(self, env, learners=None, **kwargs):
        self.env = env

        learners = learners or []
        try:
            self.learners = list(learners)
        except (TypeError, ValueError):
            self.learners = [learners]

        learner_names = [l.name for l in self.learners]
        assert len(learner_names) == len(set(learner_names)), (
            "Learners must have unique names. Names are: {}".format(learner_names))

        super(RLUpdater, self).__init__(env, **kwargs)

    def trainable_variables(self, for_opt):
        return [v for learner in self.learners for v in learner.trainable_variables(for_opt=for_opt)]

    def _build_graph(self):
        for learner in self.learners:
            learner.build_graph(self.env)

    def _update(self, batch_size):
        train_record, off_policy_record = {}, {}

        for learner in self.learners:
            _train_record, _off_policy_record = learner.update(batch_size)

            for k, v in _train_record.items():
                key = (learner.name + ":" if learner.name else "") + k
                train_record[key] = v
            for k, v in _off_policy_record.items():
                key = (learner.name + ":" if learner.name else "") + k
                off_policy_record[key] = v

        return dict(train=train_record, off_policy=off_policy_record)

    def _evaluate(self, batch_size, mode):
        n_rollouts = cfg.n_val_rollouts
        record = defaultdict(float)
        n_iters = int(np.ceil(n_rollouts / batch_size))

        for it in range(n_iters):
            n_remaining = n_rollouts - it * batch_size
            _batch_size = min(batch_size, n_remaining)

            for learner in self.learners:
                _record = learner.evaluate(_batch_size, mode)

                for k, v in _record.items():
                    key = (learner.name + ":" if learner.name else "") + k
                    record[key] += _batch_size * v

        record = {k: v / n_rollouts for k, v in record.items()}
        return record
