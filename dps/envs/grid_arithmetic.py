import tensorflow as tf
import numpy as np
import os
from collections import OrderedDict
import sys

from dps import cfg
from dps.register import RegisterBank
from dps.environment import CompositeEnv, InternalEnv
from dps.supervised import SupervisedDataset, ClassificationEnv, IntegerRegressionEnv
from dps.vision import EMNIST_CONFIG, SALIENCE_CONFIG, OMNIGLOT_CONFIG, OmniglotDataset
from dps.utils.tf import LeNet, MLP, SalienceMap, extract_glimpse_numpy_like
from dps.utils import DataContainer, Param, Config, image_to_string
from dps.updater import DifferentiableUpdater
from dps.rl.policy import EpsilonSoftmax, DiscretePolicy

from mnist_arithmetic import load_emnist, load_omniglot


def sl_build_env():
    train = GridArithmeticDataset(n_examples=cfg.n_train, one_hot=True)
    val = GridArithmeticDataset(n_examples=cfg.n_val, one_hot=True)
    test = GridArithmeticDataset(n_examples=cfg.n_val, one_hot=True)
    return ClassificationEnv(train, val, test, one_hot=True)


def sl_get_updater(env):
    build_model = LeNet(n_units=int(cfg.n_controller_units))
    return DifferentiableUpdater(env, build_model)


def build_env():
    if cfg.ablation == 'omniglot':
        if not cfg.omniglot_classes:
            cfg.omniglot_classes = OmniglotDataset.sample_classes(10)
        internal = OmniglotCounting()
    else:
        internal = GridArithmetic()

    if cfg.ablation == 'omniglot':
        with Config(classes=cfg.omniglot_classes, target_loc=cfg.op_loc):
            train = GridOmniglotDataset(n_examples=cfg.n_train, indices=range(15))
            val = GridOmniglotDataset(n_examples=cfg.n_val, indices=range(15))
            test = GridOmniglotDataset(n_examples=cfg.n_val, indices=range(15, 20))
    else:
        train = GridArithmeticDataset(n_examples=cfg.n_train, one_hot=False)
        val = GridArithmeticDataset(n_examples=cfg.n_val, one_hot=False)
        test = GridArithmeticDataset(n_examples=cfg.n_val, one_hot=False)

    external = IntegerRegressionEnv(train, val, test)

    env = CompositeEnv(external, internal)
    env.obs_is_image = True
    return env


def build_policy(env, **kwargs):
    action_selection = EpsilonSoftmax(env.actions_dim, one_hot=True)
    return DiscretePolicy(action_selection, env.obs_shape, **kwargs)


class _GridArithmeticRenderRollouts(object):
    image_fields = "glimpse salience_input salience".split()

    def _render_rollout(self, env, T, action_names, actions, fields, f):
        if 'glimpse' in fields:
            glimpse = fields['glimpse']
            glimpse = glimpse.reshape((glimpse.shape[0],) + env.internal.sub_image_shape)
        else:
            glimpse = None

        if 'salience_input' in fields:
            salience_input = fields['salience_input']
            salience_input = salience_input.reshape(
                (salience_input.shape[0],) + env.internal.salience_input_shape)
        else:
            salience_input = None

        if 'salience' in fields:
            salience = fields['salience']
            salience = salience.reshape(
                (salience.shape[0],) + env.internal.salience_output_shape)
        else:
            salience = None

        for t in range(T):
            print("t={}".format(t) + "* " * 20, file=f)

            if glimpse is not None:
                print('glimpse', file=f)
                print(image_to_string(glimpse[t]), file=f)
                print("\n", file=f)

            if salience_input is not None:
                print('salience_input', file=f)
                print(image_to_string(salience_input[t]), file=f)
                print("\n", file=f)

            if salience is not None:
                print('salience', file=f)
                print(image_to_string(salience[t]), file=f)
                print("\n", file=f)

            for k, v in fields.items():
                if k not in self.image_fields:
                    print("{}: {}".format(k, v[t]), file=f)

            action_idx = int(np.argmax(actions[t, :env.n_discrete_actions]))
            print("\ndiscrete action={}".format(action_names[action_idx]), file=f)
            print("\nother action={}".format(actions[t, env.n_discrete_actions:]), file=f)

    def __call__(self, env, rollouts):
        self.env = env
        registers = np.concatenate([rollouts.obs, rollouts.hidden], axis=2)
        registers = np.concatenate(
            [registers, rollouts._metadata['final_registers'][np.newaxis, ...]],
            axis=0)

        internal = env.internal

        path = os.path.join(cfg.path, 'rollouts')
        os.makedirs(path, exist_ok=True)

        for i in range(registers.shape[1]):
            fields = internal.rb.as_dict(registers[:, i, :])
            fields = OrderedDict((k, fields[k]) for k in sorted(fields.keys()))

            actions = rollouts.a[:, i, :]

            with open(os.path.join(path, str(i)), 'w') as f:
                print("Start of rollout {}.".format(i), file=f)
                self._render_rollout(env, rollouts.T, internal.action_names, actions, fields, f)


render_rollouts = _GridArithmeticRenderRollouts()


config = Config(
    log_name='grid_arithmetic',
    render_rollouts=render_rollouts,
    build_env=build_env,
    build_policy=build_policy,

    reductions="A:sum,M:prod,X:max,N:min",
    arithmetic_actions="+,*,max,min,+1",

    curriculum=[dict()],
    base=10,
    threshold=0.04,
    T=30,
    min_digits=2,
    max_digits=3,
    final_reward=True,
    parity='both',

    op_loc=(0, 0),  # With respect to draw_shape
    start_loc=(0, 0),  # With respect to env_shape
    env_shape=(2, 2),
    draw_offset=(0, 0),
    draw_shape=(2, 2),
    sub_image_shape=(14, 14),

    n_train=10000,
    n_val=100,
    use_gpu=False,

    show_op=True,
    reward_window=0.4999,
    salience_action=True,
    salience_input_shape=(3*14, 3*14),
    salience_output_shape=(14, 14),
    initial_salience=False,
    visible_glimpse=False,

    ablation='easy',

    build_digit_classifier=lambda: LeNet(128, scope="digit_classifier"),
    build_op_classifier=lambda: LeNet(128, scope="op_classifier"),
    build_omniglot_classifier=lambda: LeNet(128, scope="omniglot_classifier"),

    emnist_config=EMNIST_CONFIG.copy(),
    salience_config=SALIENCE_CONFIG.copy(
        min_digits=0,
        max_digits=4,
        std=0.05,
        n_units=100
    ),
    omniglot_config=OMNIGLOT_CONFIG.copy(),
    omniglot_classes=[
        'Cyrillic,17', 'Mkhedruli_(Georgian),5', 'Bengali,23', 'Mongolian,19',
        'Malayalam,3', 'Ge_ez,15', 'Glagolitic,33', 'Tagalog,11', 'Gujarati,23',
        'Old_Church_Slavonic_(Cyrillic),7'],  # Chosen randomly from set of all omniglot symbols.

    largest_digit=1000,

    n_glimpse_features=128,
)


class GridArithmeticDataset(SupervisedDataset):
    reductions = Param()

    env_shape = Param()
    draw_offset = Param(None)
    draw_shape = Param(None)

    min_digits = Param()
    max_digits = Param()
    base = Param()
    op_loc = Param()
    one_hot = Param()
    largest_digit = Param(1000)
    sub_image_shape = Param((14, 14))
    show_op = Param(True)
    parity = Param('both')

    reductions_dict = {
        "sum": sum,
        "prod": np.product,
        "max": max,
        "min": min,
        "len": len,
    }

    def __init__(self, **kwargs):
        if not self.draw_shape:
            self.draw_shape = self.env_shape
        if not self.draw_offset:
            self.draw_offset = (0, 0)

        assert 1 <= self.base <= 10
        assert self.min_digits <= self.max_digits
        assert np.product(self.draw_shape) >= self.max_digits + 1

        if ":" not in self.reductions:
            self.reductions = {'A': self.reductions_dict[self.reductions.strip()]}
            self.show_op = False
        else:
            _reductions = {}
            delim = ',' if ',' in self.reductions else ' '
            for pair in self.reductions.split(delim):
                char, key = pair.split(':')
                _reductions[char] = self.reductions_dict[key]
            self.reductions = _reductions

        op_symbols = sorted(self.reductions)
        emnist_x, emnist_y, symbol_map = load_emnist(
            cfg.data_dir, op_symbols, balance=True, shape=self.sub_image_shape)
        emnist_y = np.squeeze(emnist_y, 1)

        reductions = {symbol_map[k]: v for k, v in self.reductions.items()}

        symbol_reps = DataContainer(emnist_x, emnist_y)

        mnist_classes = list(range(self.base))
        if self.parity == 'even':
            mnist_classes = [c for c in mnist_classes if c % 2 == 0]
        elif self.parity == 'odd':
            mnist_classes = [c for c in mnist_classes if c % 2 == 1]
        elif self.parity == 'both':
            pass
        else:
            raise Exception("NotImplemented")

        mnist_x, mnist_y, classmap = load_emnist(
            cfg.data_dir, mnist_classes, balance=True, shape=self.sub_image_shape)
        mnist_y = np.squeeze(mnist_y, 1)
        inverted_classmap = {v: k for k, v in classmap.items()}
        mnist_y = np.array([inverted_classmap[y] for y in mnist_y])

        digit_reps = DataContainer(mnist_x, mnist_y)
        blank_element = np.zeros(self.sub_image_shape)

        x, y = self.make_dataset(
            self.env_shape, self.min_digits, self.max_digits, self.base,
            blank_element, digit_reps, symbol_reps,
            reductions, self.n_examples, self.op_loc, self.show_op,
            one_hot=self.one_hot, largest_digit=self.largest_digit,
            draw_offset=self.draw_offset, draw_shape=self.draw_shape)

        super(GridArithmeticDataset, self).__init__(x, y)

    @staticmethod
    def make_dataset(
            env_shape, min_digits, max_digits, base, blank_element,
            digit_reps, symbol_reps, functions, n_examples, op_loc, show_op,
            one_hot, largest_digit, draw_offset, draw_shape):

        new_X, new_Y = [], []

        size = np.product(draw_shape)

        m, n = blank_element.shape
        if op_loc is not None:
            _op_loc = np.ravel_multi_index(op_loc, draw_shape)

        for j in range(n_examples):
            nd = np.random.randint(min_digits, max_digits+1)

            indices = np.random.choice(size, nd+1, replace=False)

            if op_loc is not None and show_op:
                indices[indices == _op_loc] = indices[0]
                indices[0] = _op_loc

            env = np.tile(blank_element, draw_shape)
            locs = zip(*np.unravel_index(indices, draw_shape))
            locs = [(slice(i*m, (i+1)*m), slice(j*n, (j+1)*n)) for i, j in locs]
            op_loc, *digit_locs = locs

            symbol_x, symbol_y = symbol_reps.get_random()
            func = functions[int(symbol_y)]

            if show_op:
                env[op_loc] = symbol_x

            ys = []

            for loc in digit_locs:
                x, y = digit_reps.get_random()
                ys.append(y)
                env[loc] = x

            if draw_shape != env_shape:
                full_env = np.tile(blank_element, env_shape)
                start_y = draw_offset[0] * blank_element.shape[0]
                start_x = draw_offset[1] * blank_element.shape[1]
                full_env[start_y:start_y+env.shape[0], start_x:start_x+env.shape[1]] = env
                env = full_env

            new_X.append(env)
            y = func(ys)

            if one_hot:
                _y = np.zeros(largest_digit+2)
                if y > largest_digit:
                    _y[-1] = 1.0
                else:
                    _y[int(y)] = 1.0
                y = _y
            else:
                y = np.minimum(y, largest_digit+1)

            if j % 10000 == 0:
                print(y)
                print(image_to_string(env))
                print("\n")

            new_Y.append(y)

        new_X = np.array(new_X).astype('f')

        if one_hot:
            new_Y = np.array(new_Y).astype('f')
        else:
            new_Y = np.array(new_Y).astype('i').reshape(-1, 1)

        return new_X, new_Y


class GridOmniglotDataset(SupervisedDataset):
    min_digits = Param()
    max_digits = Param()
    classes = Param()
    indices = Param()
    target_loc = Param()
    one_hot = Param(False)
    sub_image_shape = Param((14, 14))

    env_shape = Param()
    draw_offset = Param(None)
    draw_shape = Param(None)

    def __init__(self, **kwargs):
        if not self.draw_shape:
            self.draw_shape = self.env_shape
        assert self.min_digits <= self.max_digits
        assert np.product(self.draw_shape) >= self.max_digits + 1

        omniglot_x, omniglot_y, symbol_map = load_omniglot(
            cfg.data_dir, self.classes, one_hot=False,
            indices=list(range(17, 20)), shape=self.sub_image_shape
        )
        omniglot_y = np.squeeze(omniglot_y, 1)
        symbol_reps = DataContainer(omniglot_x, omniglot_y)

        blank_element = np.zeros(self.sub_image_shape)

        x, y = self.make_dataset(
            self.env_shape, self.min_digits, self.max_digits,
            blank_element, symbol_reps,
            self.n_examples, self.target_loc,
            one_hot=self.one_hot,
            draw_offset=self.draw_offset, draw_shape=self.draw_shape)

        super(GridOmniglotDataset, self).__init__(x, y)

    @staticmethod
    def make_dataset(
            env_shape, min_digits, max_digits, blank_element,
            symbol_reps, n_examples, target_loc,
            one_hot, draw_offset, draw_shape):

        new_X, new_Y = [], []

        size = np.product(draw_shape)

        m, n = blank_element.draw_shape
        _target_loc = np.ravel_multi_index(target_loc, draw_shape)

        # min_digits, max_digits do NOT include target digit.
        for j in range(n_examples):
            # Random number of other digits
            nd = np.random.randint(min_digits, max_digits+1)
            indices = np.random.choice(size, nd+1, replace=False)

            indices[indices == _target_loc] = indices[0]
            indices[0] = _target_loc

            env = np.tile(blank_element, draw_shape)
            locs = zip(*np.unravel_index(indices, draw_shape))
            target_loc, *other_locs = [(slice(i*m, (i+1)*m), slice(j*n, (j+1)*n)) for i, j in locs]

            target_x, target_y = symbol_reps.get_random()
            env[target_loc] = target_x

            n_target_repeats = np.random.randint(0, nd)

            for k in range(nd):
                if k < n_target_repeats:
                    _x, _y = symbol_reps.get_random_with_label(target_y)
                else:
                    _x, _y = symbol_reps.get_random_without_label(target_y)
                env[other_locs[k]] = _x

            if draw_shape != env_shape:
                full_env = np.tile(blank_element, env_shape)
                start_y = draw_offset[0] * blank_element.shape[0]
                start_x = draw_offset[1] * blank_element.shape[1]
                full_env[start_y:start_y+env.shape[0], start_x:start_x+env.shape[1]] = env
                env = full_env

            new_X.append(env)
            y = n_target_repeats + 1

            if j % 10000 == 0:
                print(y)
                print(image_to_string(env))
                print("\n")

            if one_hot:
                _y = np.zeros(size)
                _y[int(y)] = 1.0
                y = _y

            new_Y.append(y)

        new_X = np.array(new_X).astype('f')

        if one_hot:
            new_Y = np.array(new_Y).astype('f')
        else:
            new_Y = np.array(new_Y).astype('i').reshape(-1, 1)

        return new_X, new_Y


def classifier_head(x):
    base = int(x.shape[-1])
    x = tf.stop_gradient(x)
    x = tf.argmax(x, -1)[..., None]
    x = tf.where(tf.equal(x, base), -1*tf.ones_like(x), x)
    x = tf.to_float(x)
    return x


class GridArithmetic(InternalEnv):
    _action_names = ['>', '<', 'v', '^', 'classify_digit', 'classify_op', 'update_salience']

    @property
    def input_shape(self):
        return tuple(es*s for es, s in zip(self.env_shape, self.sub_image_shape))

    @property
    def n_discrete_actions(self):
        return len(self.action_names)

    arithmetic_actions = Param()
    env_shape = Param()
    base = Param()
    start_loc = Param()
    sub_image_shape = Param()
    visible_glimpse = Param()
    salience_action = Param()
    salience_input_shape = Param()
    salience_output_shape = Param()
    initial_salience = Param()

    op_classes = [chr(i + ord('A')) for i in range(26)]

    arithmetic_actions_dict = {
        '+': lambda acc, digit: acc + digit,
        '-': lambda acc, digit: acc - digit,
        '*': lambda acc, digit: acc * digit,
        '/': lambda acc, digit: acc / digit,
        'max': lambda acc, digit: tf.maximum(acc, digit),
        'min': lambda acc, digit: tf.minimum(acc, digit),
        '+1': lambda acc, digit: acc + 1,
        '-1': lambda acc, digit: acc - 1,
    }

    def __init__(self, **kwargs):
        self.sub_image_size = np.product(self.sub_image_shape)
        self.salience_input_size = np.product(self.salience_input_shape)
        self.salience_output_size = np.product(self.salience_output_shape)

        _arithmetic_actions = {}
        delim = ',' if ',' in self.arithmetic_actions else ' '
        for key in self.arithmetic_actions.split(delim):
            _arithmetic_actions[key] = self.arithmetic_actions_dict[key]
        self.arithmetic_actions = _arithmetic_actions

        self.action_names = (
            self._action_names +
            sorted(self.arithmetic_actions.keys())
        )

        self.actions_dim = len(self.action_names)
        self.action_sizes = [1] * self.actions_dim
        self._init_networks()
        self._init_rb()

        super(GridArithmetic, self).__init__()

    def _init_rb(self):
        values = (
            [0., 0., -1., 0., 0., -1.] +
            [np.zeros(self.salience_output_size, dtype='f')] +
            [np.zeros(self.sub_image_size, dtype='f')] +
            [np.zeros(self.salience_input_size, dtype='f')]
        )

        if self.visible_glimpse:
            self.rb = RegisterBank(
                'GridArithmeticRB',
                'digit op acc fovea_x fovea_y prev_action salience glimpse', 'salience_input', values=values,
                output_names='acc', no_display='glimpse salience salience_input',
            )
        else:
            self.rb = RegisterBank(
                'GridArithmeticRB',
                'digit op acc fovea_x fovea_y prev_action salience', 'glimpse salience_input', values=values,
                output_names='acc', no_display='glimpse salience salience_input',
            )

    def _init_networks(self):
        digit_config = cfg.emnist_config.copy(
            classes=list(range(self.base)),
            build_function=cfg.build_digit_classifier
        )

        self.digit_classifier = cfg.build_digit_classifier()
        self.digit_classifier.set_pretraining_params(
            digit_config, name_params='classes include_blank shape n_controller_units',
            directory=cfg.model_dir + '/emnist_pretrained'
        )

        op_config = cfg.emnist_config.copy(
            classes=list(self.op_classes),
            build_function=cfg.build_op_classifier
        )

        self.op_classifier = cfg.build_op_classifier()
        self.op_classifier.set_pretraining_params(
            op_config, name_params='classes include_blank shape n_controller_units',
            directory=cfg.model_dir + '/emnist_pretrained',
        )

        self.classifier_head = classifier_head

        self.maybe_build_salience_detector()

    def maybe_build_salience_detector(self):
        if self.salience_action:
            def _build_salience_detector(output_shape=self.salience_output_shape):
                return SalienceMap(
                    2 * cfg.max_digits,
                    MLP([cfg.n_units, cfg.n_units, cfg.n_units], scope="salience_detector"),
                    output_shape, std=cfg.std, flatten_output=True
                )

            salience_config = cfg.salience_config.copy(
                output_shape=self.salience_output_shape,
                image_shape=self.salience_input_shape,
                sub_image_shape=self.sub_image_shape,
                build_function=_build_salience_detector,
            )

            with salience_config:
                self.salience_detector = _build_salience_detector()

            self.salience_detector.set_pretraining_params(
                salience_config,
                name_params='classes std min_digits max_digits n_units '
                            'sub_image_shape image_shape output_shape',
                directory=cfg.model_dir + '/salience_pretrained'
            )
        else:
            self.salience_detector = None

    def _build_update_glimpse(self, fovea_y, fovea_x):
        top_left = tf.concat([fovea_y, fovea_x], axis=-1) * self.sub_image_shape

        inp = self.input_ph[..., None]

        glimpse = extract_glimpse_numpy_like(
            inp, self.sub_image_shape, top_left, fill_value=0.0)
        glimpse = tf.reshape(glimpse, (-1, self.sub_image_size), name="glimpse")
        return glimpse

    def _build_update_salience(self, update_salience, salience, salience_input, fovea_y, fovea_x):
        top_left = tf.concat([fovea_y, fovea_x], axis=-1) * self.sub_image_shape
        top_left -= (np.array(self.salience_input_shape) - np.array(self.sub_image_shape)) / 2.0

        inp = self.input_ph[..., None]

        glimpse = extract_glimpse_numpy_like(
            inp, self.salience_input_shape, top_left, fill_value=0.0)

        new_salience = self.salience_detector(glimpse, self.salience_output_shape, False)
        new_salience = tf.reshape(new_salience, (-1, self.salience_output_size))

        new_salience_input = tf.reshape(glimpse, (-1, self.salience_input_size))

        salience = (1 - update_salience) * salience + update_salience * new_salience
        salience_input = (1 - update_salience) * salience_input + update_salience * new_salience_input
        return salience, salience_input

    def _build_update_storage(self, glimpse, prev_digit, classify_digit, prev_op, classify_op):
        digit = self.classifier_head(self.digit_classifier(glimpse, self.base + 1, False))
        new_digit = (1 - classify_digit) * prev_digit + classify_digit * digit

        op = self.classifier_head(self.op_classifier(glimpse, len(self.op_classes) + 1, False))
        new_op = (1 - classify_op) * prev_op + classify_op * op

        return new_digit, new_op

    def _build_update_fovea(self, right, left, down, up, fovea_y, fovea_x):
        fovea_x = (1 - right - left) * fovea_x + \
            right * (fovea_x + 1) + \
            left * (fovea_x - 1)
        fovea_y = (1 - down - up) * fovea_y + \
            down * (fovea_y + 1) + \
            up * (fovea_y - 1)
        fovea_y = tf.clip_by_value(fovea_y, 0, self.env_shape[0]-1)
        fovea_x = tf.clip_by_value(fovea_x, 0, self.env_shape[1]-1)
        return fovea_y, fovea_x

    def _build_return_values(self, registers, actions):
        new_registers = self.rb.wrap(*registers)
        reward = self.build_reward(new_registers, actions)
        done = tf.zeros(tf.shape(new_registers)[:-1])[..., None]
        return done, reward, new_registers

    def build_init(self, r):
        self.maybe_build_placeholders()

        (_digit, _op, _acc, _fovea_x, _fovea_y, _prev_action,
            _salience, _glimpse, _salience_input) = self.rb.as_tuple(r)
        batch_size = tf.shape(self.input_ph)[0]

        # init fovea
        if self.start_loc is not None:
            fovea_y = tf.fill((batch_size, 1), self.start_loc[0])
            fovea_x = tf.fill((batch_size, 1), self.start_loc[1])
        else:
            fovea_y = tf.random_uniform(
                tf.shape(fovea_y), 0, self.env_shape[0], dtype=tf.int32)
            fovea_x = tf.random_uniform(
                tf.shape(fovea_x), 0, self.env_shape[1], dtype=tf.int32)

        fovea_y = tf.cast(fovea_y, tf.float32)
        fovea_x = tf.cast(fovea_x, tf.float32)

        glimpse = self._build_update_glimpse(fovea_y, fovea_x)

        salience = _salience
        salience_input = _salience_input
        if self.initial_salience:
            salience, salience_input = self._build_update_salience(
                1.0, _salience, _salience_input, _fovea_y, _fovea_x)

        digit = -1 * tf.ones((batch_size, 1), dtype=tf.float32)
        op = -1 * tf.ones((batch_size, 1), dtype=tf.float32)
        acc = -1 * tf.ones((batch_size, 1), dtype=tf.float32)

        return self.rb.wrap(
            digit, op, acc, fovea_x, fovea_y,
            _prev_action, salience, glimpse, salience_input)

    def build_step(self, t, r, a):
        _digit, _op, _acc, _fovea_x, _fovea_y, _prev_action, _salience, _glimpse, _salience_input = self.rb.as_tuple(r)

        actions = self.unpack_actions(a)
        (right, left, down, up, classify_digit, classify_op,
            update_salience, *arithmetic_actions) = actions

        salience = _salience
        salience_input = _salience_input
        if self.salience_action:
            salience, salience_input = self._build_update_salience(
                update_salience, _salience, _salience_input, _fovea_y, _fovea_x)

        digit = tf.zeros_like(_digit)
        acc = tf.zeros_like(_acc)

        original_factor = tf.ones_like(right)
        for key, action in zip(sorted(self.arithmetic_actions), arithmetic_actions):
            original_factor -= action
            acc += action * self.arithmetic_actions[key](_acc, _digit)
        acc += original_factor * _acc

        acc = tf.clip_by_value(acc, -1000.0, 1000.0)

        digit, op = self._build_update_storage(_glimpse, _digit, classify_digit, _op, classify_op)
        fovea_y, fovea_x = self._build_update_fovea(right, left, down, up, _fovea_y, _fovea_x)
        glimpse = self._build_update_glimpse(fovea_y, fovea_x)

        prev_action = tf.argmax(a, axis=-1)[..., None]
        prev_action = tf.to_float(prev_action)

        return self._build_return_values(
            [digit, op, acc, fovea_x, fovea_y, prev_action, salience, glimpse, salience_input],
            actions)


class GridArithmeticEasy(GridArithmetic):
    def build_step(self, t, r, a):
        _digit, _op, _acc, _fovea_x, _fovea_y, _prev_action, _salience, _glimpse, _salience_input = self.rb.as_tuple(r)

        actions = self.unpack_actions(a)
        (right, left, down, up, classify_digit, classify_op,
            update_salience, *arithmetic_actions) = actions

        salience = _salience
        salience_input = _salience_input
        if self.salience_action:
            salience, salience_input = self._build_update_salience(
                update_salience, _salience, _salience_input, _fovea_y, _fovea_x)

        op = self.classifier_head(self.op_classifier(_glimpse, len(self.op_classes) + 1, False))
        op = (1 - classify_op) * _op + classify_op * op

        new_digit_factor = classify_digit
        for action in arithmetic_actions:
            new_digit_factor += action

        digit = self.classifier_head(self.digit_classifier(_glimpse, self.base + 1, False))
        digit = (1 - new_digit_factor) * _digit + new_digit_factor * digit

        new_acc_factor = tf.zeros_like(right)
        acc = tf.zeros_like(_acc)
        for key, action in zip(sorted(self.arithmetic_actions), arithmetic_actions):
            new_acc_factor += action
            # Its crucial that we use `digit` here and not `_digit`
            acc += action * self.arithmetic_actions[key](_acc, digit)
        acc += (1 - new_acc_factor) * _acc

        acc = tf.clip_by_value(acc, -1000.0, 1000.0)

        fovea_y, fovea_x = self._build_update_fovea(right, left, down, up, _fovea_y, _fovea_x)
        glimpse = self._build_update_glimpse(fovea_y, fovea_x)

        prev_action = tf.argmax(a, axis=-1)[..., None]
        prev_action = tf.to_float(prev_action)

        return self._build_return_values(
            [digit, op, acc, fovea_x, fovea_y, prev_action, salience, glimpse, salience_input],
            actions)


class OmniglotCounting(GridArithmeticEasy):
    _action_names = GridArithmeticEasy._action_names + ['classify_omniglot']

    omniglot_classes = Param()

    def _init_rb(self):
        values = (
            [0., 0., 0., -1., 0., 0., -1.] +
            [np.zeros(self.salience_output_size, dtype='f')] +
            [np.zeros(self.sub_image_size, dtype='f')] +
            [np.zeros(self.salience_input_size, dtype='f')]
        )

        if self.visible_glimpse:
            self.rb = RegisterBank(
                'GridArithmeticRB',
                'omniglot digit op acc fovea_x fovea_y prev_action salience glimpse', 'salience_input', values=values,
                output_names='acc', no_display='glimpse salience salience_input',
            )
        else:
            self.rb = RegisterBank(
                'GridArithmeticRB',
                'omniglot digit op acc fovea_x fovea_y prev_action salience', 'glimpse salience_input', values=values,
                output_names='acc', no_display='glimpse salience salience_input',
            )

    def build_step(self, t, r, a):
        _omniglot, _digit, _op, _acc, _fovea_x, _fovea_y, _prev_action, _salience, _glimpse, _salience_input = self.rb.as_tuple(r)

        actions = self.unpack_actions(a)
        (right, left, down, up, classify_digit, classify_op, classify_omniglot,
            update_salience, *arithmetic_actions) = actions

        salience = _salience
        salience_input = _salience_input
        if self.salience_action:
            salience, salience_input = self._build_update_salience(
                update_salience, _salience, _salience_input, _fovea_y, _fovea_x)

        omniglot = self.classifier_head(self.omniglot_classifier(_glimpse, len(self.omniglot_classes) + 1, False))
        omniglot = (1 - classify_omniglot) * _omniglot + classify_omniglot * omniglot

        op = self.classifier_head(self.op_classifier(_glimpse, len(self.op_classes) + 1, False))
        op = (1 - classify_op) * _op + classify_op * op

        new_digit_factor = classify_digit
        for action in arithmetic_actions:
            new_digit_factor += action

        digit = self.classifier_head(self.digit_classifier(_glimpse, self.base + 1, False))
        digit = (1 - new_digit_factor) * _digit + new_digit_factor * digit

        new_acc_factor = tf.zeros_like(right)
        acc = tf.zeros_like(_acc)
        for key, action in zip(sorted(self.arithmetic_actions), arithmetic_actions):
            new_acc_factor += action
            # Its crucial that we use `digit` here and not `_digit`
            acc += action * self.arithmetic_actions[key](_acc, digit)
        acc += (1 - new_acc_factor) * _acc

        acc = tf.clip_by_value(acc, -1000.0, 1000.0)

        fovea_y, fovea_x = self._build_update_fovea(right, left, down, up, _fovea_y, _fovea_x)
        glimpse = self._build_update_glimpse(fovea_y, fovea_x)

        prev_action = tf.argmax(a, axis=-1)[..., None]
        prev_action = tf.to_float(prev_action)

        return self._build_return_values(
            [omniglot, digit, op, acc, fovea_x, fovea_y, prev_action, salience, glimpse, salience_input], actions)

    def _init_networks(self):
        super(OmniglotCounting, self)._init_networks()
        omniglot_config = cfg.omniglot_config.copy(
            classes=self.omniglot_classes,
            build_function=cfg.build_omniglot_classifier,
        )

        self.omniglot_classifier = cfg.build_omniglot_classifier()
        self.omniglot_classifier.set_pretraining_params(
            omniglot_config,
            name_params='classes include_blank shape n_controller_units',
            directory=cfg.model_dir + '/omniglot_pretrained',
        )

    def build_init(self, r):
        self.maybe_build_placeholders()

        (_omniglot, _digit, _op, _acc, _fovea_x, _fovea_y,
            _prev_action, _salience, _glimpse, _salience_input) = self.rb.as_tuple(r)

        batch_size = tf.shape(self.input_ph)[0]

        # init fovea
        if self.start_loc is not None:
            fovea_y = tf.fill((batch_size, 1), self.start_loc[0])
            fovea_x = tf.fill((batch_size, 1), self.start_loc[1])
        else:
            fovea_y = tf.random_uniform(
                tf.shape(fovea_y), 0, self.env_shape[0], dtype=tf.int32)
            fovea_x = tf.random_uniform(
                tf.shape(fovea_x), 0, self.env_shape[1], dtype=tf.int32)

        fovea_y = tf.cast(fovea_y, tf.float32)
        fovea_x = tf.cast(fovea_x, tf.float32)

        glimpse = self._build_update_glimpse(fovea_y, fovea_x)

        salience = _salience
        salience_input = _salience_input
        if self.initial_salience:
            salience, salience_input = self._build_update_salience(
                1.0, _salience, _salience_input, fovea_y, fovea_x)

        omniglot = -1 * tf.ones((batch_size, 1), dtype=tf.float32)
        digit = -1 * tf.ones((batch_size, 1), dtype=tf.float32)
        op = -1 * tf.ones((batch_size, 1), dtype=tf.float32)
        acc = -1 * tf.ones((batch_size, 1), dtype=tf.float32)

        return self.rb.wrap(
            omniglot, digit, op, acc, fovea_x, fovea_y,
            _prev_action, salience, glimpse, salience_input)
