from __future__ import absolute_import
from __future__ import division
import time
from contextlib import ExitStack
import tensorflow as tf
import numpy as np
from pprint import pformat
import datetime
import shutil
import dill

from spectral_dagger.utils.experiment import ExperimentStore
from dps import cfg
from dps.utils import (
    restart_tensorboard, EarlyStopHook, gen_seed,
    time_limit, uninitialized_variables_initializer, du)


def training_loop(curriculum, exp_name=''):
    loop = TrainingLoop(curriculum, exp_name)
    return loop.run()


class TrainingLoop(object):
    def __init__(self, curriculum, exp_name=''):
        self.curriculum = curriculum
        self.exp_name = exp_name

    def run(self):
        if cfg.start_tensorboard:
            restart_tensorboard(str(cfg.log_dir), cfg.tbport)

        value = self._run_core()

        if cfg.slim:
            print("`slim` is True, so deleting experiment directory {}.".format(self.exp_dir.path))
            try:
                shutil.rmtree(self.exp_dir.path)
            except FileNotFoundError:
                pass
            print("Size of {} after delete: {}.".format(cfg.log_dir, du(cfg.log_dir)))

        return value

    def _run_core(self):
        self.start = time.time()

        es = ExperimentStore(str(cfg.log_dir), max_experiments=cfg.max_experiments, delete_old=1)
        self.exp_dir = exp_dir = es.new_experiment(
            self.exp_name, use_time=1, force_fresh=1, update_latest=cfg.update_latest)

        print("Scratch pad is {}.".format(exp_dir.path))
        cfg.path = exp_dir.path

        print(cfg)

        with open(exp_dir.path_for('config.txt'), 'w') as f:
            f.write(str(cfg.freeze()))
        with open(exp_dir.path_for('config.pkl'), 'wb') as f:
            dill.dump(cfg.freeze(), f, protocol=dill.HIGHEST_PROTOCOL)

        batches_per_epoch = int(np.ceil(cfg.n_train / cfg.batch_size))
        self.max_epochs = int(np.ceil(cfg.max_steps / batches_per_epoch))

        threshold_reached = True
        stage = 1
        self.global_step = 0

        while True:
            if self.time_remaining <= 1:
                print("Time limit exceeded.")
                break

            with ExitStack() as stack:
                graph = tf.Graph()

                if not cfg.use_gpu:
                    stack.enter_context(graph.device("/cpu:0"))

                if cfg.save_summaries:
                    self.train_writer = tf.summary.FileWriter(exp_dir.path_for('train'), graph)
                    self.val_writer = tf.summary.FileWriter(exp_dir.path_for('val'))
                    print("Writing summaries to {}.".format(exp_dir.path))

                sess = tf.Session(graph=graph)

                stack.enter_context(graph.as_default())
                stack.enter_context(sess)
                stack.enter_context(sess.as_default())

                try:
                    stage_cfg, updater = next(self.curriculum)

                except StopIteration:
                    print("Curriculum complete after {} stage(s).".format(stage-1))
                    break

                stack.enter_context(stage_cfg)

                tf_seed = gen_seed()
                tf.set_random_seed(tf_seed)

                self.summary_op = tf.summary.merge_all()
                tf.contrib.framework.get_or_create_global_step()
                sess.run(uninitialized_variables_initializer())
                sess.run(tf.assert_variables_initialized())

                with time_limit(self.time_remaining, verbose=True) as limiter:
                    try:
                        threshold_reached, n_steps, reason = self._run_stage(stage, updater)
                    except KeyboardInterrupt:
                        reason = "User interrupt."

                if limiter.ran_out:
                    reason = "Time limit reached."

                if cfg.start_tensorboard:
                    restart_tensorboard(str(cfg.log_dir), cfg.tbport)

                print("Optimization complete. Reason: {}".format(reason))

                print("Loading best hypothesis from stage {} "
                      "from file {}...".format(stage, self.best_path))
                updater.restore(self.best_path)

                self.curriculum.end_stage()

                if threshold_reached or cfg.power_through:
                    stage += 1
                else:
                    print("Failed to reach error threshold on stage {} "
                          "of the curriculum, terminating.".format(stage))
                    break

        print(self.curriculum.summarize())
        history = self.curriculum.history()
        result = dict(
            config=cfg.freeze(),
            output=history,
            n_stages=len(history)
        )

        return result

    @property
    def elapsed_time(self):
        return time.time() - self.start

    @property
    def time_remaining(self):
        if cfg.max_time is None or cfg.max_time <= 0:
            return np.inf
        else:
            return cfg.max_time - self.elapsed_time

    def _run_stage(self, stage_idx, updater):
        """ Run a stage of a curriculum. """
        local_step = 0
        threshold_reached = False
        val_loss = np.inf
        reason = None
        total_train_time = 0.0

        print("Starting stage {} at {}.".format(stage_idx, datetime.datetime.now()))

        while True:
            n_epochs = updater.n_experiences / cfg.n_train
            if n_epochs >= self.max_epochs:
                reason = "Maximum number of steps reached."
                break

            evaluate = self.global_step % cfg.eval_step == 0
            display = self.global_step % cfg.display_step == 0

            if evaluate or display:
                start_time = time.time()
                train_summary, train_loss, val_summary, val_loss = updater.update(
                    cfg.batch_size, self.summary_op if evaluate else None)
                duration = time.time() - start_time

                total_train_time += duration
                time_per_example = total_train_time / ((local_step+1) * cfg.batch_size)
                time_per_batch = total_train_time / (local_step+1)

                if evaluate and cfg.save_summaries:
                    self.train_writer.add_summary(train_summary, self.global_step)
                    self.val_writer.add_summary(val_summary, self.global_step)

                if display:
                    print("Step(g: {}, l: {}): TLoss={:06.4f}, VLoss={:06.4f}, "
                          "Sec/Batch={:06.10f}, Sec/Example={:06.10f}, Epoch={:04.2f}.".format(
                              self.global_step, local_step, train_loss, val_loss, time_per_batch,
                              time_per_example, updater.env.completion))

                new_best, stop = self.curriculum.check(val_loss, self.global_step, local_step)

                if new_best:
                    checkpoint_file = self.exp_dir.path_for('best_stage={}'.format(stage_idx))
                    print("Storing new best on local step {} (global step {}) "
                          "with validation loss of {}.".format(
                              local_step, self.global_step, val_loss))
                    self.best_path = updater.save(checkpoint_file)

                if stop:
                    reason = "Early stopping triggered."
                    break

                if val_loss < cfg.threshold:
                    reason = "Validation loss threshold reached."
                    threshold_reached = True
                    break

            else:
                updater.update(cfg.batch_size)

            if cfg.checkpoint_step > 0 and self.global_step % cfg.checkpoint_step == 0:
                print("Checkpointing on global step {}.".format(self.global_step))
                checkpoint_file = self.exp_dir.path_for('model_stage={}'.format(stage_idx))
                updater.save(checkpoint_file, local_step)

            local_step += 1
            self.global_step += 1

        return threshold_reached, local_step, reason


class Curriculum(object):
    def __init__(self):
        self.prev_stage = -1
        self.stage = 0
        self.early_stop = EarlyStopHook(patience=cfg.patience)

    def __iter__(self):
        return self

    def __next__(self):
        return self.__call__()

    def __call__(self):
        pass

    def check(self, validation_loss, global_step, local_step=None):
        return self.early_stop.check(validation_loss, global_step, local_step)

    def end_stage(self):
        """ Should be called inside the same default graph, session
            and config as the previous call to ``__call__``. """
        self.early_stop.end_stage()

    def summarize(self):
        s = "\n"
        for stage, (bvgs, bvls, bv) in enumerate(self.early_stop._history):
            s += "Stage {} ".format(stage) + "*" * 30 + '\n'
            s += "* best value: {}\n".format(bv)
            s += "* global step: {}\n".format(bvgs)
            s += "* local step: {}\n".format(bvls)
            s += "* new config values: {}\n\n".format(pformat(cfg.curriculum[stage]))
        return s

    def history(self):
        return self.early_stop._history
