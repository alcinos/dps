import tensorflow as tf
import numpy as np
import abc
from future.utils import with_metaclass


class RegisterSpec(with_metaclass(abc.ABCMeta, object)):
    """ Specification of a set of registers.

    Inherit from this class in order to create specific register specifications.

    Parameters
    ----------
    registers: list of union(ndarray, tuple(shape, function))
        Specification of the shape and initial values of the registers.  Each entry
        is either an ndarray or (s, f), where s is a the shape for that register
        and f if a function that accepts a shape and a random state and returns an ndarray.
    visible: list of bool
        If True, corresponding register is visible to the controller.

    """
    dtype = tf.float32

    @abc.abstractproperty
    def visible(self):
        raise NotImplementedError()

    @abc.abstractproperty
    def initial_values(self):
        raise NotImplementedError()

    @abc.abstractproperty
    def namedtuple(self):
        raise NotImplementedError()

    @abc.abstractproperty
    def input_names(self):
        raise NotImplementedError()

    @abc.abstractproperty
    def output_names(self):
        raise NotImplementedError()

    @property
    def names(self):
        return self.namedtuple._fields

    def get_initial_values(self, np_random=None, **kwargs):
        """ Returns a list of ndarray's giving initial values for each of the registers. """
        init = []
        for name, t in zip(self.names, self.initial_values):
            if name in kwargs:
                init_value = kwargs[name].copy()
            elif isinstance(t, np.ndarray):
                init_value = t.copy()
            else:
                shape = t[0]
                f = t[1]
                init_value = f(shape, np_random)
            init.append(init_value)
        return init

    def shapes(self, visible_only=False):
        return [
            (None,) + (t.shape if isinstance(t, np.ndarray) else t[0])
            for t, v in zip(self.initial_values, self.visible)
            if v or not visible_only]

    def instantiate(self, batch_size=1, np_random=None, **kwargs):
        values = [self.get_initial_values(np_random, **kwargs) for i in range(batch_size)]
        values = [np.concatenate(v, axis=0) for v in zip(*values)]
        return self.wrap(*values)

    def wrap(self, *args, **kwargs):
        return self.namedtuple(*args, **kwargs)

    def build_placeholders(self, dtype=tf.float32):
        """ Get a list of tensorflow placeholders suitable for feeding as input to this RegisterSpec. """
        shapes = self.shapes()
        return self.wrap(*[tf.placeholder(dtype, shape=s, name=n) for n, s in zip(self.names, shapes)])

    def get_register_values(self, registers, *names, as_obs=True, axis=1):
        values = []
        names = names or self.names
        for name in names:
            try:
                value = getattr(registers, name)
            except AttributeError:
                try:
                    value = registers[name]
                except (TypeError, KeyError):
                    try:
                        value = registers[self.names.index(name)]
                        if isinstance(value, tuple):
                            value = value[0]
                    except (TypeError, IndexError):
                        raise Exception(
                            "{} could not be interpreted as an instance of {}, "
                            "it is missing a key/attribute named {}.".format(
                                registers, self, name))
            values.append(value)

        if as_obs:
            if len(values) > 1:
                if isinstance(values[0], np.ndarray):
                    obs = np.concatenate(values, axis=axis)
                elif isinstance(values[0], (tf.Tensor, tf.TensorArray)):
                    obs = tf.concat(values, axis=axis)
                else:
                    raise Exception("Register value 0 is not recognized datatype: {}.".format(values[0]))
            else:
                obs = values[0]
        else:
            obs = tuple(values)
        return obs

    def as_obs(self, registers, visible_only=False, axis=1):
        """ Concatenate values of registers, giving a single ndarray or Tensor representing the entire register.

        Parameters
        ----------
        registers: instance of self.namedtuple
            The object to interpret. For every register in self,
            registers must have an attribute or key with the same name.
        visible_only: bool
            If True, will only include visible register in the returned array.

        """
        if visible_only:
            names = [n for n, v in zip(self.names, self.visible) if v]
        else:
            names = self.names
        return self.get_register_values(registers, *names, as_obs=1, axis=axis)

    def from_obs(self, obs, axis=1):
        """ Unpack on observation as a register.

        Parameters
        ----------
        obs: ndarray or Tensor
            The observation to unpack. Must have enough dimensions to fill
            all the registers.
        tf: bool
            Whether to use tensorflow or numpy.

        Returns
        -------
        instance of self.namedtuple

        """
        split_locs = np.cumsum([shape[1] for shape in self.shapes()])
        if isinstance(obs, np.ndarray):
            values = np.split(obs, split_locs[:-1], axis=axis)
        elif isinstance(obs, tf.Tensor):
            values = tf.split(obs, split_locs[:-1], axis=axis)
        else:
            raise Exception("``obs`` is not recognized datatype, should be either ndarray or Tensor but got: {}.".format(obs))
        return self.wrap(*values)

    def state_size(self):
        """ Appropriate for use as the return value of ``RNNCell.state_size()``. """
        ss = []
        for iv in self.initial_values:
            if isinstance(iv, np.ndarray):
                shape = iv.shape
            else:
                shape = iv[0]
            ss.append(shape)
        return ss
