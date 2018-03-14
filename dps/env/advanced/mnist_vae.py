import tensorflow as tf
import numpy as np
import os

from dps import cfg
from dps.updater import DifferentiableUpdater
from dps.env.supervised import BernoulliSigmoid
from dps.datasets import EMNIST_ObjectDetection
from dps.utils import Config, Param
from dps.utils.tf import FullyConvolutional, ScopedFunction


def build_env():
    train = EMNIST_ObjectDetection(n_examples=int(cfg.n_train))
    val = EMNIST_ObjectDetection(n_examples=int(cfg.n_val))
    test = EMNIST_ObjectDetection(n_examples=int(cfg.n_val))

    return VAE_Env(train, val, test)


def get_updater(env):
    model = VAE()
    return DifferentiableUpdater(env, model)


class Encoder(FullyConvolutional):
    def __init__(self):
        layout = [
            dict(filters=128, kernel_size=3, strides=2, padding="SAME"),
            dict(filters=256, kernel_size=3, strides=2, padding="SAME"),
            dict(filters=256, kernel_size=4, strides=1, padding="VALID"),
            dict(filters=256, kernel_size=7, strides=1, padding="SAME"),
            dict(filters=4, kernel_size=7, strides=1, padding="SAME"),
        ]
        super(Encoder, self).__init__(layout, check_output_shape=True)

    def _call(self, inp, output_size, is_training):
        mean = super(Encoder, self)._call(inp, output_size, is_training)
        second_last = self.volumes[-2]
        std = self._apply_layer(second_last, self.layout[-1], len(self.layout), True)
        return mean, std


class Decoder(FullyConvolutional):
    def __init__(self):
        layout = [
            dict(filters=256, kernel_size=7, strides=1, padding="SAME", transpose=True),
            dict(filters=256, kernel_size=7, strides=1, padding="SAME", transpose=True),
            dict(filters=256, kernel_size=4, strides=1, padding="VALID", transpose=True),
            dict(filters=256, kernel_size=3, strides=2, padding="SAME", transpose=True),
            dict(filters=3, kernel_size=3, strides=2, padding="SAME", transpose=True),
        ]
        super(Decoder, self).__init__(layout, check_output_shape=True)


class VAE_Env(BernoulliSigmoid):
    xent_loss = Param()
    beta = Param()

    def __init__(self, train, val, test=None, **kwargs):
        super(VAE_Env, self).__init__(train, val, test, **kwargs)

    def make_feed_dict(self, batch_size, mode, evaluate):
        x, *_ = self.datasets[mode].next_batch(batch_size=batch_size, advance=not evaluate)
        return {self.x: x, self.is_training: not evaluate}

    def _build_placeholders(self):
        self.x = tf.placeholder(tf.float32, (None,) + self.obs_shape, name="x")
        self.is_training = tf.placeholder(tf.bool, (), name="is_training")

    def _build(self):
        self.logits = self.prediction
        self.sigmoids = tf.nn.sigmoid(self.logits)

        recorded_tensors = {
            name: tf.reduce_mean(getattr(self, 'build_' + name)(self.prediction, self.x))
            for name in ['xent_loss', '2norm_loss', '1norm_loss']
        }

        batch_size = tf.shape(self.x)[0]
        _kl_loss = 0.5 * (self.f.mean**2 + self.f.std**2 - 2. * tf.log(self.f.std) - 1)
        _kl_loss = tf.reshape(_kl_loss, (batch_size, -1))
        recorded_tensors['kl_loss'] = tf.reduce_mean(tf.reduce_sum(_kl_loss, axis=1))

        recorded_tensors['std'] = tf.reduce_mean(self.f.std)
        recorded_tensors['mean'] = tf.reduce_mean(self.f.mean)

        loss_key = 'xent_loss' if self.xent_loss else '2norm_loss'
        recorded_tensors['loss'] = recorded_tensors[loss_key]
        if self.beta > 0.0:
            recorded_tensors['loss'] += self.beta * recorded_tensors['kl_loss']
        return recorded_tensors

    def build_xent_loss(self, logits, targets):
        batch_size = tf.shape(self.x)[0]
        targets = tf.reshape(targets, (batch_size, -1))
        logits = tf.reshape(logits, (batch_size, -1))
        return tf.reduce_sum(
            tf.nn.sigmoid_cross_entropy_with_logits(labels=targets, logits=logits),
            keep_dims=True, axis=1
        )

    def build_2norm_loss(self, logits, targets):
        actions = tf.sigmoid(logits)

        batch_size = tf.shape(self.x)[0]
        targets = tf.reshape(targets, (batch_size, -1))
        actions = tf.reshape(actions, (batch_size, -1))

        return tf.reduce_mean((actions - targets)**2, keep_dims=True, axis=1)

    def build_1norm_loss(self, logits, targets):
        actions = tf.sigmoid(logits)

        batch_size = tf.shape(self.x)[0]
        targets = tf.reshape(targets, (batch_size, -1))
        actions = tf.reshape(actions, (batch_size, -1))

        return tf.reduce_mean(tf.abs(actions - targets), keep_dims=True, axis=1)


class VAE(ScopedFunction):
    code_shape = Param()
    _encoder = None
    _decoder = None

    def __call__(self, inp, output_size, is_training):
        if self._encoder is None:
            self._encoder = cfg.build_encoder()

        if self._decoder is None:
            self._decoder = cfg.build_decoder()

        self.mean, _std = self._encoder(inp, self.code_shape, is_training)
        self.std = tf.exp(_std)

        normal_samples = tf.random_normal(tf.shape(self.std))
        sampled_code = self.mean + (self.std * normal_samples)

        return self._decoder(sampled_code, inp.shape[1:], is_training)


def mnist_vae_render_hook(updater):
    # Run the network on a subset of the evaluation data, fetch the output
    N = 16

    env = updater.env
    feed_dict = env.make_feed_dict(N, 'val', True)
    images = feed_dict[env.x]

    sess = tf.get_default_session()
    sigmoids = sess.run(env.sigmoids, feed_dict=feed_dict)

    sqrt_N = int(np.ceil(np.sqrt(N)))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2*sqrt_N, sqrt_N, figsize=(20, 20))
    axes = np.array(axes).reshape(2*sqrt_N, sqrt_N)
    for n, (pred, gt) in enumerate(zip(sigmoids, images)):
        i = int(n / sqrt_N)
        j = int(n % sqrt_N)

        ax1 = axes[2*i, j]
        ax1.imshow(pred)
        ax1.set_title('prediction')

        ax2 = axes[2*i+1, j]
        ax2.imshow(gt)
        ax2.set_title('ground_truth')

    fig.suptitle('After {} experiences ({} updates, {} experiences per batch).'.format(
        updater.n_experiences, updater.n_updates, cfg.batch_size))

    fig.savefig(os.path.join(cfg.path, 'plots', 'reconstruction.pdf'))
    plt.close(fig)


config = Config(
    log_name="mnist_vae",
    build_env=build_env,
    get_updater=get_updater,
    min_chars=1,
    max_chars=1,
    characters=[0, 1],
    sub_image_shape=(28, 28),
    build_encoder=Encoder,
    build_decoder=Decoder,
    beta=1.0,
    xent_loss=True,

    render_hook=mnist_vae_render_hook,
    render_step=500,

    # image_shape=(28, 28),
    # code_shape=(4, 4, 4),
    image_shape=(40, 40),
    code_shape=(7, 7, 4),

    n_train=1e5,
    n_val=1e2,
    n_test=1e2,

    curriculum=[dict(lr_schedule=lr) for lr in [1e-4, 1e-5, 1e-6]],
    preserve_env=True,

    # training params
    batch_size=16,
    # batch_size=64,
    eval_step=100,
    max_steps=1e7,
    patience=10000,
    optimizer_spec="adam",
    use_gpu=True,
    gpu_allow_growth=True,
    seed=347405995,
    stopping_criteria="loss,min",
    threshold=-np.inf,
    max_grad_norm=1.0,
)