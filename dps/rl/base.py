import numpy as np
import tensorflow as tf
import abc

from dps import cfg
from dps.utils import masked_mean, tf_discount_matrix, shift_fill
from dps.utils import build_scheduled_value, Param, Parameterized
from dps.updater import Updater


def rl_render_hook(updater):
    if hasattr(updater, 'learners'):
        render_rollouts = getattr(cfg, 'render_rollouts', None)
        for learner in updater.learners:
            with learner:
                updater.env.visualize(
                    policy=learner.mu,
                    n_rollouts=4, T=cfg.T, mode='train',
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
    """
        truncated_rollouts: bool
            If True, then our rollouts are sub-trajectories of longer trajectories. The consequence
            is that in general we cannot say that the value after final state-action-reward triple
            is 0. This implies that T-step backups (assuming the rollout is given by
            (x_0, a_0, r_0, ..., x_(T-1), a_(T-1), r_(T-1)) will not be valid, since we don't have
            a proper value estimate past the end of the trajectory. When not using truncated trajectories,
            we assume that the value past the end of the trajectory is 0, and hence T-step backups are OK.

    """
    active_context = None

    exploration_schedule = Param()
    test_time_explore = Param()
    updates_per_sample = Param(1)

    def __init__(self, gamma, truncated_rollouts=False, name=None):
        self.mu = None
        self.gamma = gamma
        self.name = name or self.__class__.__name__
        self.terms = []
        self.plugins = []
        self._signals = {}
        self.truncated_rollouts = truncated_rollouts
        self.optimizer = None
        self.train_summaries = []
        self.summaries = []
        self.recorded_values = []
        self.update_batch_size = None
        self.replay_buffer = None
        self.objective_fn_terms = []
        self.rl_objects = []

    def __enter__(self):
        if RLContext.active_context is not None:
            raise Exception("May not have multiple instances of RLContext active at once.")
        RLContext.active_context = self
        return self

    def __exit__(self, type_, value, tb):
        RLContext.active_context = None

    def add_rl_object(self, obj):
        if isinstance(obj, ObjectiveFunctionTerm):
            self.objective_fn_terms.append(obj)
        assert isinstance(obj, RLObject)
        self.rl_objects.append(obj)

    def set_behaviour_policy(self, mu):
        self.mu = mu

    def set_optimizer(self, opt):
        self.optimizer = opt

    def set_replay_buffer(self, update_batch_size, replay_buffer):
        self.update_batch_size = update_batch_size
        self.replay_buffer = replay_buffer

    def add_agent(self, agent):
        self.agents.append(agent)

    def add_train_summary(self, summary):
        """ Add a summary that should only be evaluated during training. """
        self.train_summaries.append(summary)

    def add_summary(self, summary):
        self.summaries.append(summary)

    def add_recorded_value(self, name, tensor):
        self.recorded_values.append((name, tensor))

    def build_graph(self, env, is_training):
        self.env = env
        self.obs_shape = env.obs_shape
        self.actions_dim = env.actions_dim
        self.is_training = is_training

        with tf.name_scope(self.name):
            with self:
                self.build_core_signals()

                objective = None
                for term in self.objective_fn_terms:
                    if objective is None:
                        objective = term.weight * term.build_graph(self)
                    else:
                        objective += term.weight * term.build_graph(self)
                self.objective = objective
                self.loss = -objective

                self.add_recorded_value("objective", self.objective)

                self.optimizer.build_update(self)

                self.train_summary_op = tf.summary.merge(self.train_summaries + self.summaries)
                self.summary_op = tf.summary.merge(self.summaries)

    def build_core_signals(self):
        training_exploration = build_scheduled_value(self.exploration_schedule)
        if isinstance(self.test_time_explore, str) or self.test_time_explore >= 0:
            testing_exploration = build_scheduled_value(self.test_time_explore)
            exploration = tf.cond(self.is_training, lambda: training_exploration, lambda: testing_exploration)
        else:
            exploration = training_exploration
        exploration = tf.identity(exploration)
        self._signals['exploration'] = exploration
        tf.summary.scalar('default_exploration', exploration, collections=['scheduled_value_summaries'])

        self._signals['mask'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_mask")

        self._signals['obs'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None) + self.obs_shape, name="_obs")
        self._signals['actions'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, self.actions_dim), name="_actions")

        self._signals['rewards'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_rewards")
        self._signals['returns'] = tf.cumsum(
            self._signals['rewards'], axis=0, reverse=True, name="_returns")
        self._signals['reward_per_ep'] = tf.reduce_mean(
            tf.reduce_sum(self._signals['rewards'], axis=0), name="_reward_per_ep")

        self.add_recorded_value("loss", -self._signals['reward_per_ep'])

        self._signals['weights'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_weights")

        T = tf.shape(self._signals['mask'])[0]
        discount_matrix = tf_discount_matrix(self.gamma, T)
        discounted_returns = tf.tensordot(
            discount_matrix, self._signals['rewards'], axes=1, name="_discounted_returns")
        self._signals['discounted_returns'] = discounted_returns

        mask = self._signals['mask']
        mean_returns = masked_mean(discounted_returns, mask, axis=1, keep_dims=True)
        mean_returns += tf.zeros_like(discounted_returns)
        self._signals['average_discounted_returns'] = mean_returns

        self.add_summary(tf.summary.scalar("reward_per_ep", self._signals['reward_per_ep']))

        self._signals['gamma'] = tf.constant(self.gamma)
        self._signals['batch_size'] = tf.shape(self._signals['obs'])[1]

        # off-policy
        self._signals['mu_utils'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, self.mu.params_dim), name="_mu_log_probs")
        self._signals['mu_exploration'] = tf.placeholder(
            tf.float32, shape=(None,), name="_mu_exploration")
        self._signals['mu_log_probs'] = tf.placeholder(
            tf.float32, shape=(cfg.T, None, 1), name="_mu_log_probs")

        for obj in self.rl_objects:
            obj.build_core_signals(self)

    def make_feed_dict(self, rollouts, weights=None):
        if weights is None:
            weights = np.ones((rollouts.T, rollouts.batch_size, 1))
        elif weights.ndim == 1:
            weights = np.tile(weights.reshape(1, -1, 1), (rollouts.T, 1, 1))

        feed_dict = {
            self._signals['mask']: (1-shift_fill(rollouts.done, 1)).astype('f'),

            self._signals['obs']: rollouts.o,
            self._signals['actions']: rollouts.a,

            self._signals['rewards']: rollouts.r,

            self._signals['weights']: weights,

            self._signals['mu_log_probs']: rollouts.log_probs,
        }

        if hasattr(rollouts, 'utils'):
            # utils are not always stored in the rollouts as they can occupy a lot of memory
            feed_dict.update({
                self._signals['mu_utils']: rollouts.utils,
                self._signals['mu_exploration']: rollouts.exploration,
            })

        return feed_dict

    def get_signal(self, key, generator=None, gradient=False, masked=True, memoize=True):
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
            key = gen_key + " | " + signal_key

            if memoize:
                signal = self._signals.get(key, None)
                if signal is None:
                    signal = generator.generate_signal(signal_key, self)
                    self._signals[key] = signal
            else:
                signal = generator.generate_signal(signal_key, self)

        maskable = len(signal.shape) >= 2
        if masked and maskable:
            mask = self._signals['mask']
            signal *= mask

        if not gradient:
            signal = tf.stop_gradient(signal)

        return signal

    def update(self, batch_size, collect_summaries):
        assert self.mu is not None, "A behaviour policy must be set using `set_behaviour_policy` before calling `update`."
        assert self.optimizer is not None, "An optimizer must be set using `set_optimizer` before calling `update`."

        with self:
            rollouts = self.env.do_rollouts(self.mu, batch_size, mode='train')

            weights = None
            n_updates = 1
            if self.replay_buffer is not None:
                self.replay_buffer.add_rollouts(rollouts)

                # If we have a replay buffer, we can potentially do multiple updates,
                # each with a different batch, per environment sample
                n_updates = self.updates_per_sample

            train_summaries = b""
            for i in range(n_updates):
                if self.replay_buffer is not None:
                    rollouts, weights = self.replay_buffer.get_batch(self.update_batch_size)

                if rollouts is None:
                    # Most common reason for `rollouts` being None is there not being enough experiences in replay memory.
                    break

                feed_dict = self.make_feed_dict(rollouts, weights)

                for obj in self.rl_objects:
                    obj.pre_update(feed_dict, self)

                self.optimizer.update(feed_dict)

                for obj in self.rl_objects:
                    obj.post_update(feed_dict, self)

                if collect_summaries and i == n_updates-1:
                    train_summaries = tf.get_default_session().run(self.train_summary_op, feed_dict=feed_dict)

            return train_summaries

    def evaluate(self, batch_size, mode):
        assert self.mu is not None, "A behaviour policy must be set using `set_behaviour_policy` before calling `evaluate`."

        with self:
            rollouts = self.env.do_rollouts(self.mu, batch_size, mode=mode)
            feed_dict = self.make_feed_dict(rollouts)

            for obj in self.rl_objects:
                obj.pre_eval(feed_dict, self)

            sess = tf.get_default_session()
            eval_summaries, *values = (
                sess.run(
                    [self.summary_op] + [v for _, v in self.recorded_values],
                    feed_dict=feed_dict))

            for obj in self.rl_objects:
                obj.post_eval(feed_dict, self)

            record = {k: v for v, (k, _) in zip(values, self.recorded_values)}
            return eval_summaries, record


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
    def __init__(self, env, learners=None, loss_func=None, **kwargs):
        self.env = env

        learners = learners or []
        try:
            self.learners = list(learners)
        except:
            self.learners = [learners]

        if loss_func is None:
            loss_func = lambda records: records[0]['loss']
        self.loss_func = loss_func

        super(RLUpdater, self).__init__(env)

    def _build_graph(self):
        for learner in self.learners:
            learner.build_graph(self.env, self.is_training)

    def _update(self, batch_size, collect_summaries):
        summaries = (b'').join(
            learner.update(batch_size, collect_summaries)
            for learner in self.learners)
        return summaries

    def _evaluate(self, batch_size, mode):
        """ Return list of tf summaries and a dictionary of values to be displayed. """

        summaries = b''
        records = []
        record = {}
        for learner in self.learners:
            s, r = learner.evaluate(batch_size, mode)
            summaries += s

            for k, v in r.items():
                record[learner.name + ":" + k] = v
            records.append(r)
        loss = self.loss_func(records)
        return loss, summaries, record
