import gym
from gym_recording.playback import scan_recorded_traces
import numpy as np
import os
import tensorflow as tf
import matplotlib.pyplot as plt
from collections import defaultdict

from dps import cfg
from dps.datasets import ImageDataset, ArrayFeature, ImageFeature
from dps.utils import Param


class RandomAgent(object):
    """The world's simplest agent!"""
    def __init__(self, action_space):
        self.action_space = action_space

    def act(self, observation, reward, done):
        return self.action_space.sample()


def gather_atari_frames(game, policy, n_frames, density=1.0, render=False):
    assert 0 < density <= 1.0

    env = gym.make(game)
    if policy is None:
        policy = RandomAgent(env.action_space)

    if render:
        outdir = '/tmp/random-agent-results'
        env = gym.wrappers.Monitor(env, directory=outdir, force=True)

    env.seed(0)
    np.random.seed(0)

    reward = 0
    done = False
    frames = []

    while len(frames) < n_frames:
        ob = env.reset()
        while True:
            action = policy.act(ob, reward, done)
            ob, reward, done, _ = env.step(action)
            if np.random.binomial(1, density):
                frames.append(ob)
            if done:
                break
            if render:
                env.render()

    env.close()
    return np.array(frames[:n_frames])


def gather_atari_human_frames(game, n_frames, density=1.0):
    assert 0 < density <= 1.0

    human_agent_action = 0
    human_wants_restart = False
    human_sets_pause = False

    def key_press(key, mod):
        nonlocal human_agent_action, human_wants_restart, human_sets_pause
        if key==0xff0d: human_wants_restart = True
        if key==32: human_sets_pause = not human_sets_pause
        a = int(key - ord('0'))
        if a <= 0 or a >= ACTIONS: return
        human_agent_action = a

    def key_release(key, mod):
        nonlocal human_agent_action
        a = int(key - ord('0'))
        if a <= 0 or a >= ACTIONS: return
        if human_agent_action == a:
            human_agent_action = 0

    env = gym.make(game)

    ACTIONS = env.action_space.n
    SKIP_CONTROL = 0

    outdir = '/tmp/random-agent-results'
    env = gym.wrappers.Monitor(env, directory=outdir, force=True)

    env.seed(0)

    env.render()
    env.unwrapped.viewer.window.on_key_press = key_press
    env.unwrapped.viewer.window.on_key_release = key_release

    np.random.seed(0)

    reward = 0
    done = False
    frames = []
    skip = 0

    env.reset()

    while len(frames) < n_frames:
        if not skip:
            action = human_agent_action
            skip = SKIP_CONTROL
        else:
            skip -= 1

        ob, reward, done, _ = env.step(action)

        env.render()

        if np.random.binomial(1, density):
            frames.append(ob)
        print(len(frames))

        if done:
            env.reset()

    env.close()
    return np.array(frames[:n_frames])


class ReinforcementLearningDataset(ImageDataset):
    rl_data_location = Param()
    max_episodes = Param(None)
    max_episode_length = Param(None)

    history_length = Param(1)

    image_shape = Param()

    action_dim = Param(1)
    reward_dim = Param(1)

    store_o = Param(True)
    store_a = Param(True)
    store_r = Param(True)

    store_next_o = Param(True)
    depth = 3

    def _write_example(self, **kwargs):
        image = None

        o_size = 0
        if self.store_o:
            image = kwargs['o']
            o_size = image.shape[-1]

        if self.store_next_o:
            if image is None:
                image = kwargs['next_o']
            else:
                image = np.concatenate([image, kwargs['next_o']], axis=3)

        if self.postprocessing == "tile":
            images, _ = self._tile_postprocess(image, [])
        elif self.postprocessing == "random":
            images, _ = self._random_postprocess(image, [])
        else:
            images = [image]

        for img in images:
            _kwargs = {}

            _kwargs['a'] = kwargs.get('a', None)
            _kwargs['r'] = kwargs.get('r', None)

            o, next_o = np.split(img, [o_size], axis=-1)

            _kwargs['o'] = o
            _kwargs['next_o'] = next_o

            super(ReinforcementLearningDataset, self)._write_example(**_kwargs)

    @property
    def features(self):
        if self._features is not None:
            return self._features

        _features = []

        if self.store_o:
            obs_shape = (self.obs_shape[0], self.obs_shape[1], self.obs_shape[2] * self.history_length)
            _features.append(ImageFeature("o", obs_shape))

        if self.store_a:
            action_dim = self.action_dim * self.history_length
            _features.append(ArrayFeature("a", (action_dim,)))

        if self.store_r:
            reward_dim = self.reward_dim * self.history_length
            _features.append(ArrayFeature("r", (reward_dim,)))

        if self.store_next_o:
            _features.append(ImageFeature("next_o", self.obs_shape))

        self._features = _features

        return _features

    def _make(self):
        scan_recorded_traces(self.rl_data_location, self._callback, self.max_episodes)

    def _callback(self, o, a, r):
        if o[0].dtype == np.uint8:
            o = list((np.array(o) / 255.).astype('f'))

        episode_length = len(o)

        if self.max_episode_length is not None:
            indices = np.random.choice(episode_length - self.history_length, size=self.max_episode_length, replace=False)
            indices += self.history_length
        else:
            indices = np.arange(self.history_length, episode_length)

        for idx in indices:
            if self.store_o:
                _o = list(o[idx-self.history_length:idx])
                _o = np.concatenate(_o, axis=2)

            if self.store_a:
                _a = np.array(a[idx-self.history_length:idx]).flatten()

            if self.store_r:
                _r = np.array(r[idx-self.history_length:idx]).flatten()

            if self.store_next_o:
                _next_o = o[idx]

            self._write_example(o=_o, a=_a, r=_r, next_o=_next_o)

    def visualize(self):
        N = 16
        dset = tf.data.TFRecordDataset(self.filename)
        dset = dset.batch(N).map(self.parse_example_batch)

        iterator = dset.make_one_shot_iterator()

        sess = tf.get_default_session()

        o, a, r, next_o = None, None, None, None
        result = sess.run(iterator.get_next())

        idx = 0

        if self.store_o:
            o = result[idx]
            idx += 1

        if self.store_a:
            a = result[idx]
            idx += 1

        if self.store_r:
            r = result[idx]
            idx += 1

        if self.store_next_o:
            next_o = result[idx]
            idx += 1

        stride = self.obs_shape[2]

        sqrt_N = int(np.ceil(np.sqrt(N)))
        fig, axes = plt.subplots(sqrt_N, sqrt_N * (self.history_length + 1), figsize=(20, 20))
        axes = np.array(axes).reshape(sqrt_N, 2*sqrt_N)

        for n in range(N):
            i = int(n / sqrt_N)
            j = int(n % sqrt_N)

            for t in range(self.history_length):
                ax = axes[i, j * (self.history_length + 1) + t]
                ax.set_aspect("equal")

                if self.store_o:
                    ax.imshow(np.squeeze(o[n, :, :, t*stride:(t+1)*stride]))

                str_a = str(a[n, t * self.action_dim: (t+1)*self.action_dim]) if self.store_a else ""
                str_r = str(r[n, t * self.reward_dim: (t+1)*self.reward_dim]) if self.store_r else ""

                ax.set_title("a={}, r={}".format(str_a, str_r))

            ax = axes[i, j * (self.history_length + 1) + self.history_length]
            ax.set_title("Next Obs")
            ax.set_aspect("equal")
            if self.store_next_o:
                ax.imshow(np.squeeze(next_o[n]))
            plt.subplots_adjust(top=0.95, bottom=0, left=0, right=1, wspace=0.1, hspace=0.1)
        plt.show()


class RewardClassificationDataset(ReinforcementLearningDataset):
    """ Note that in general, the data returned by gym_recording will contain
        one more observation than the number of rewards/actions. """
    classes = Param()
    one_hot = Param(True)
    max_examples_per_class = Param(None)
    balanced = Param()

    store_o = True
    store_a = True
    store_r = True
    store_next_o = False

    @property
    def reward_dim(self):
        return len(self.classes) if self.one_hot else 1

    @property
    def features(self):
        if self._features is not None:
            return self._features

        _features = []

        _features.append(ImageFeature("o", self.obs_shape))
        _features.append(ArrayFeature("a", (self.action_dim,)))
        _features.append(ArrayFeature("r", (1,)))

        self._features = _features

        return _features

    def _make(self):
        self.examples = defaultdict(list)
        scan_recorded_traces(self.rl_data_location, self._callback, self.max_episodes)

        if self.balanced:
            lengths = [len(v) for v in self.examples.values()]
            max_length = max(lengths)

            if self.max_examples_per_class is not None:
                max_length = min(max_length, self.max_examples_per_class)

            balanced_examples = []

            for r, _examples in self.examples.items():
                balanced = []

                while len(balanced) < max_length:
                    balanced.extend(_examples)
                balanced = balanced[:max_length]

                balanced = [(o, a, np.array([r])) for o, a in balanced]
                balanced_examples.extend(balanced)

            balanced_examples = np.random.permutation(balanced_examples)

            for o, a, r in balanced_examples:
                self._write_example(o=o, a=a, r=r)

    def _callback(self, o, a, r):
        if o[0].dtype == np.uint8:
            o = list((np.array(o) / 255.).astype('f'))

        episode_length = len(o)-1

        if not episode_length:
            # Only one observation, and no actions or rewards
            return

        if self.max_episode_length is not None:
            indices = np.random.choice(episode_length, size=self.max_episode_length, replace=False)
        else:
            indices = np.arange(episode_length)

        for idx in indices:
            _o = list(o[idx:idx+1])
            _o = np.concatenate(_o, axis=2)
            _a = np.array(a[idx:idx+1]).flatten()
            _r = int(r[idx])

            if self.balanced:
                if _r in self.classes:
                    # Store examples instead of writing them, so that we can balance the classes later.
                    self.examples[_r].append((_o, _a))
            else:
                self._write_example(o=_o, a=_a, r=np.array([_r]))

    def parse_example_batch(self, example_proto):
        o, a, r = super(RewardClassificationDataset, self).parse_example_batch(example_proto)

        if self.one_hot:
            r = tf.argmin(tf.abs(r - self.classes), axis=1)
            r = tf.one_hot(r, len(self.classes))
        else:
            r = tf.cast(r, tf.int32)

        return o, a, r


class StaticAtariDataset(ReinforcementLearningDataset):
    game = Param(aliases="atari_game")
    image_shape = Param(None)
    after_warp = Param(False)

    action_dim = 1
    reward_dim = 1
    rl_data_location = None

    @property
    def obs_shape(self):
        if self.after_warp:
            return (84, 84, 1)
        else:
            return (210, 160, 3)

    def _make(self):
        directory = os.path.join(cfg.data_dir, "atari_data")
        dirs = os.listdir(directory)
        starts_with = "atari_data_env={}.datetime=".format(self.game)
        dirs = [d for d in dirs if d.startswith(starts_with)]
        assert len(dirs) == 1

        directory = os.path.join(directory, dirs[0])

        if self.after_warp:
            directory = os.path.join(directory, "after_warp_recording")
        else:
            directory = os.path.join(directory, "before_warp_recording")
        scan_recorded_traces(directory, self._callback, self.max_episodes)


if __name__ == "__main__":
    # game = "AsteroidsNoFrameskip-v4"
    # dset = AtariAutoencodeDataset(game=game, policy=None, n_examples=100, density=0.01, atari_render=False)
    # show_frames(dset.x[:10])
    # dset = AtariAutoencodeDataset(
    #     game=game, policy=None, n_examples=100, samples_per_frame=2, image_shape=(50, 50))
    # show_frames(dset.x[:100])
    # dset = AtariAutoencodeDataset(
    #     game=game, policy=None, n_examples=100, samples_per_frame=0, image_shape=(30, 40))

    # game = "IceHockeyNoFrameskip-v4"
    # dset = StaticAtariDataset(game=game, history_length=2, max_episodes=3, max_episode_length=7, after_warp=False, store_next_o=True)

    xo_dir = "/media/data/Dropbox/projects/PyDSRL/train"
    # xo_dir = "/media/data/Dropbox/projects/PyDSRL/log"

    dset = RewardClassificationDataset(
        rl_data_location=xo_dir, image_shape=(100, 100),
        classes=[-2, -1, 0, 1, 2], max_examples_per_class=99, postprocessing="random",
        n_samples_per_image=3, tile_shape=(48, 48))

    sess = tf.Session()
    with sess.as_default():
        dset.visualize()
