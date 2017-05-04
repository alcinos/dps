from __future__ import absolute_import
from __future__ import division
import time
from contextlib import ExitStack
import tensorflow as tf
import numpy as np
import sys

from spectral_dagger.utils.experiment import ExperimentStore
from dps.utils import restart_tensorboard, EarlyStopHook, gen_seed


def uninitialized_variables_initializer():
    """ init only uninitialized variables - from
        http://stackoverflow.com/questions/35164529/
        in-tensorflow-is-there-any-way-to-just-initialize-uninitialised-variables """
    uninitialized_vars = []
    sess = tf.get_default_session()
    for var in tf.global_variables():
        try:
            sess.run(var)
        except tf.errors.FailedPreconditionError:
            uninitialized_vars.append(var)
    uninit_init = tf.variables_initializer(uninitialized_vars)
    return uninit_init


def _training_loop(
        curriculum, log_dir, config,
        max_experiments=5, start_tensorboard=True, exp_name='',
        reset_global_step=False):

    es = ExperimentStore(log_dir, max_experiments=max_experiments, delete_old=1)
    exp_dir = es.new_experiment(exp_name, use_time=1, force_fresh=1)
    config.path = exp_dir.path

    print(config)

    with open(exp_dir.path_for('config'), 'w') as f:
        f.write(str(config))

    batches_per_epoch = int(np.ceil(config.n_train / config.batch_size))
    max_epochs = int(np.ceil(config.max_steps / batches_per_epoch))

    early_stop = EarlyStopHook(patience=config.patience)

    train_writer = None
    val_writer = None

    threshold_reached = True
    stage = 1
    global_step = 0

    while True:
        # tf.reset_default_graph()
        graph = tf.Graph()
        sess = tf.Session(graph=graph)

        with ExitStack() as stack:
            stack.enter_context(graph.as_default())
            stack.enter_context(sess)
            stack.enter_context(sess.as_default())
            stack.enter_context(config.as_default())

            tf_seed = gen_seed()
            tf.set_random_seed(tf_seed)

            try:
                updater = next(curriculum)

                print("\nStarting stage {} of the curriculum. "
                      "New updater is \n{}\n".format(stage, updater))

            except StopIteration:
                print("Curriculum complete after {} stage(s).".format(stage-1))
                break

            if train_writer is None:
                train_writer = tf.summary.FileWriter(exp_dir.path_for('train'), graph)
                val_writer = tf.summary.FileWriter(exp_dir.path_for('val'))
                print("Writing summaries to {}.".format(exp_dir.path))

            with tf.name_scope('stage'):
                tf_stage = tf.constant(stage)
                tf.summary.scalar('stage', tf_stage)

            summary_op = tf.summary.merge_all()
            tf.contrib.framework.get_or_create_global_step()
            sess.run(uninitialized_variables_initializer())
            sess.run(tf.assert_variables_initialized())

            threshold_reached = False
            val_loss = np.inf
            local_step = 0

            while True:
                n_epochs = updater.n_experiences / config.n_train
                if n_epochs >= max_epochs:
                    print("Optimization complete, maximum number of epochs reached.")
                    break

                evaluate = global_step % config.eval_step == 0
                display = global_step % config.display_step == 0

                if evaluate or display:
                    start_time = time.time()
                    train_summary, train_loss, val_summary, val_loss = updater.update(
                        config.batch_size, summary_op if evaluate else None)
                    duration = time.time() - start_time

                    if evaluate:
                        train_writer.add_summary(train_summary, global_step)
                        val_writer.add_summary(val_summary, global_step)

                    if display:
                        print("Step(global: {}, local: {}): Minibatch Loss={:06.4f}, Validation Loss={:06.4f}, "
                              "Minibatch Duration={:06.4f} seconds, Epoch={:04.2f}.".format(
                                  global_step, local_step, train_loss, val_loss, duration, updater.env.completion))

                    new_best, stop = early_stop.check(global_step, local_step, val_loss)

                    if new_best:
                        checkpoint_file = exp_dir.path_for('best_stage={}'.format(stage))
                        print("Storing new best in on local step {} (global step {}) "
                              "with validation loss of {}.".format(
                                  local_step, global_step, val_loss))
                        best_path = updater.save(checkpoint_file)
                    if stop:
                        print("Optimization complete, early stopping triggered.")
                        break
                else:
                    updater.update(config.batch_size)

                if global_step % config.checkpoint_step == 0:
                    print("Checkpointing on global step {}.".format(global_step))
                    checkpoint_file = exp_dir.path_for('model_stage={}'.format(stage))
                    updater.save(checkpoint_file, local_step)

                if val_loss < config.threshold:
                    print("Optimization complete, validation loss threshold reached.")
                    threshold_reached = True
                    break

                local_step += 1
                global_step += 1

            if not new_best:
                print("Loading best hypothesis from stage {} from file {}...".format(stage, best_path))
                updater.restore(best_path)

            early_stop.end_stage()
            curriculum.end_stage()

            if threshold_reached:
                stage += 1
            else:
                print("Failed to reach error threshold on stage {} of the curriculum, terminating.".format(stage))
                break

    print(early_stop.summarize())


def training_loop(
        curriculum, log_dir, config,
        max_experiments=5, start_tensorboard=True, exp_name='',
        reset_global_step=False):

    kwargs = locals().copy()

    try:
        _training_loop(**kwargs)
    except KeyboardInterrupt:
        if start_tensorboard:
            restart_tensorboard(log_dir)

        et, ei, tb = sys.exc_info()
        raise ei.with_traceback(tb)

    if start_tensorboard:
        restart_tensorboard(log_dir)


class Curriculum(object):
    def __init__(self, base_kwargs, curriculum):
        self.base_kwargs = base_kwargs
        self.curriculum = list(curriculum)
        self.prev_stage = -1
        self.stage = 0

    def __iter__(self):
        return self

    def __next__(self):
        return self.__call__()

    def __call__(self):
        raise NotImplementedError()

    def end_stage(self):
        """ Should be called inside the same default graph, session
            and config as the previous call to ``__call__``. """
        raise NotImplementedError()


def build_and_visualize(build_psystem, mode, n_rollouts, sample):
    graph = tf.Graph()
    sess = tf.Session(graph=graph)

    with ExitStack() as stack:
        stack.enter_context(graph.as_default())
        stack.enter_context(sess)
        stack.enter_context(sess.as_default())

        tf_seed = gen_seed()
        tf.set_random_seed(tf_seed)

        psystem = build_psystem()

        sess.run(uninitialized_variables_initializer())
        sess.run(tf.assert_variables_initialized())

        start_time = time.time()
        psystem.visualize(mode, n_rollouts, sample)
        duration = time.time() - start_time
        print("Took {} seconds.".format(duration))
