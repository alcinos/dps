import shutil
import dill
from pathlib import Path
from zipfile import ZipFile
from collections import defaultdict
import argparse
from contextlib import contextmanager
import traceback
import pdb
import sys


class Operator(object):
    """
    Needs to be fully serializable since we're saving and loading these all the time.

    Parameters
    ----------
    name: str
    func_key: key
    inp_keys: list of key
    outp_keys: list of key
    metadata: dict

    """
    def __init__(self, name, func_key, inp_keys, outp_keys, metadata=None):
        self.name = name
        self.func_key = func_key
        self.inp_keys = inp_keys
        self.outp_keys = outp_keys
        self.metadata = metadata

    def __str__(self):
        from pprint import pformat
        return "Operator(\n    name={},\n    func_key={},\n    inp_keys={},\n    outp_keys={},\n    metadata={})".format(
            pformat(self.name), pformat(self.func_key), pformat(self.inp_keys),
            pformat(self.outp_keys), pformat(self.metadata))

    def __repr__(self):
        return str(self)

    def status(self, store):
        from pprint import pformat
        s = ["\n" + ("-" * 40)]
        s.append("Status report for\n {}.".format(self))
        is_ready = self.is_ready(store)
        s.append("Ready? {}".format(is_ready))
        if is_ready:
            s.append("Input values:")
            inputs = [store.load_object('data', ik) for ik in self.inp_keys]
            s.append(pformat(inputs))

        is_complete = self.is_complete(store)
        s.append("Complete? {}".format(is_complete))
        if is_complete:
            s.append("Output values:")
            outputs = [store.load_object('data', ok) for ok in self.outp_keys]
            s.append(pformat(outputs))
        return '\n'.join(s)

    def is_complete(self, store):
        return all([store.object_exists('data', ok) for ok in self.outp_keys])

    def is_ready(self, store):
        return all([store.object_exists('data', ik) for ik in self.inp_keys])

    def run(self, store):
        print("\n\n" + ("-" * 40))
        print("Running op {}".format(self.name))
        if self.is_complete(store):
            print("Skipping op {}, already complete.".format(self.name))
            return False

        if not self.is_ready(store):
            print("Skipping op {}, deps are not met.".format(self.name))
            return False

        inputs = [store.load_object('data', ik) for ik in self.inp_keys]
        func = store.load_object('function', self.func_key)

        outputs = func(*inputs)

        if len(self.outp_keys) == 1:
            store.save_object('data', self.outp_keys[0], outputs, force_unique=True)
        else:
            for o, ok in zip(outputs, self.outp_keys):
                store.save_object('data', ok, o, force_unique=True)

        print("Op complete.")

        return True


class Signal(object):
    def __init__(self, key, name):
        self.key = key
        self.name = name

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return "Signal(key={}, name={})".format(self.key, self.name)


class Job(object):
    def __init__(self, path):
        # A store for functions, data and operators.
        self.objects = FileSystemObjectStore(path)
        self.map_idx = 0
        self.reduce_idx = 0
        self.n_signals = 0

    def save_object(self, kind, key, obj, force_unique=True, clobber=False):
        self.objects.save_object(kind, key, obj, force_unique, clobber)

    def add_op(self, name, func, inputs, n_outputs):
        if not callable(func):
            func_key = func
        else:
            func_key = self.objects.get_unique_key('function')
            self.save_object('function', func_key, func, force_unique=False)

        inp_keys = []
        for inp in inputs:
            if not isinstance(inp, Signal):
                key = self.objects.get_unique_key('data')
                self.save_object('data', key, inp, force_unique=True)
            else:
                key = inp.key
            inp_keys.append(key)

        outputs = [
            Signal(key=self.objects.get_unique_key('data'), name="{}[{}]".format(name, i))
            for i in range(n_outputs)]
        outp_keys = [outp.key for outp in outputs]

        op = Operator(
            name=name, func_key=func_key,
            inp_keys=inp_keys, outp_keys=outp_keys)
        op_key = self.objects.get_unique_key('operator')
        self.save_object('operator', op_key, op, force_unique=True)

        return outputs

    def map(self, func, inputs, name=None):
        """ Currently restricted to fns with one input and one output. """
        op_name = name or 'map:{}'.format(self.map_idx)
        results = []

        func_key = self.objects.get_unique_key('function')
        self.save_object('function', func_key, func, force_unique=True)

        for idx, inp in enumerate(inputs):
            op_result = self.add_op('{}/app:{}'.format(op_name, idx), func_key, [inp], 1)
            results.append(op_result[0])

        self.map_idx += 1

        return results

    def reduce(self, func, inputs, name=None):
        op_name = name or 'reduce:{}'.format(self.reduce_idx)
        op_result = self.add_op(op_name, func, inputs, 1)
        self.reduce_idx += 1

        return op_result

    def summary(self):
        operators = list(self.objects.load_objects('operator').values())
        s = ["Job Summary\n-----------"]
        s.append("\nn_ops: {}".format(len(operators)))

        is_complete = [op.is_complete(self.objects) for op in operators]
        completed_ops = [op for i, op in enumerate(operators) if is_complete[i]]
        incomplete_ops = [op for i, op in enumerate(operators) if not is_complete[i]]

        s.append("\nn_completed_ops: {}".format(len(completed_ops)))
        for op in completed_ops:
            s.append(op.status(self.objects))

        is_ready = [op.is_ready(self.objects) for op in operators]
        ready_incomplete_ops = [op for i, op in enumerate(incomplete_ops) if is_ready[i]]
        not_ready_incomplete_ops = [op for i, op in enumerate(incomplete_ops) if not is_ready[i]]

        s.append("\nn_ready_incomplete_ops: {}".format(len(ready_incomplete_ops)))
        for op in ready_incomplete_ops:
            s.append(op.status(self.objects))

        s.append("\nn_not_ready_incomplete_ops: {}".format(len(not_ready_incomplete_ops)))
        for op in not_ready_incomplete_ops:
            s.append(op.status(self.objects))

        return '\n'.join(s)

    def run(self, pattern, indices):
        operators = list(self.objects.load_objects('operator').values())
        selected = KeywordMapping.batch([op.name for op in operators], pattern)
        operators = sorted((op for i, op in enumerate(operators) if selected[i]), key=lambda op: op.name)
        if not operators:
            return False

        if not indices:
            indices = set(range(len(operators)))

        return [op.run(self.objects) for i, op in enumerate(operators) if i in indices]


class ObjectStore(object):
    def __init__(self):
        pass

    def object_exists(self, kind, key):
        raise NotImplementedError("Abstract method.")

    def save_object(self, kind, key, obj, force_unique=True, clobber=False):
        raise NotImplementedError("Abstract method.")

    def load_object(self, kind, key):
        raise NotImplementedError("Abstract method.")

    def load_objects(self, kind):
        raise NotImplementedError("Abstract method.")

    def n_objects(self, kind=None):
        raise NotImplementedError("Abstract method.")


def split_path(path, root):
    _path = Path(path).relative_to(root)
    kind = str(_path.parent)
    key = _path.stem
    return kind, key


class FileSystemObjectStore(ObjectStore):
    def __init__(self, directory, force_fresh=False):
        self.used_keys = defaultdict(list)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=not force_fresh)
        super(FileSystemObjectStore, self).__init__()

    def path_for(self, kind=None, key=None):
        path = self.directory
        if kind:
            path /= kind
        if key:
            path /= '{}.key'.format(key)
        return path

    def get_unique_key(self, kind):
        unique_key = max(self.used_keys[kind], default=0) + 1
        assert unique_key not in self.used_keys[kind]
        self.used_keys[kind].append(unique_key)
        return unique_key

    def object_exists(self, kind, key):
        return self.path_for(kind, key).exists()

    def save_object(self, kind, key, obj, force_unique=True, clobber=False):
        if self.object_exists(kind, key):
            if force_unique:
                raise ValueError("Trying to save object {} with kind {} and key {}, "
                                 "but an object ({}) already exists at that location and "
                                 "``force_unique`` is True.".format(obj, kind, key, self.load_object(kind, key)))
            if not clobber:
                return

        path = self.path_for(kind, key)
        path.parent.mkdir(exist_ok=True, parents=True)

        with path.open('wb') as f:
            dill.dump(obj, f, protocol=dill.HIGHEST_PROTOCOL, recurse=True)

    def load_object(self, kind, key):
        path = self.path_for(kind, key)
        if not self.object_exists(kind, key):
            raise ValueError("No object with kind {} and key {}.".format(kind, key))
        with path.open('rb') as f:
            obj = dill.load(f)
        return obj

    def load_objects(self, kind):
        directory = self.path_for(kind)
        objects = {}
        for obj_path in directory.glob('**/*.key'):
            with obj_path.open('rb') as f:
                obj = dill.load(f)
            objects[split_path(obj_path, self.directory)] = obj
        return objects

    def n_objects(self, kind=None):
        return len(self.keys(kind))

    def keys(self, kind):
        """ Returns list of tuples of form (kind, key) """
        directory = self.path_for(kind)
        return list(split_path(p, self.directory) for p in directory.glob('**/*.key'))

    def zip(self, archive_name):
        # Within the archive, all entries are contained inside
        # a directory with a name given by ``base_dir``.
        shutil.make_archive(
            str(archive_name), 'zip', root_dir=str(self.directory.parent),
            base_dir=str(self.directory.name))


class ZipObjectStore(ObjectStore):
    """ A read-only object store based on zip file. Avoids ever unzipping the entire file. """
    def __init__(self, zip_path):
        self._zip = ZipFile(str(zip_path), 'r')
        self._zip_root = Path(zip_root(zip_path))

    def __enter__(self):
        pass

    def __exit__(self):
        self._zip.close()

    def path_for(self, kind=None, key=None):
        path = self._zip_root
        if kind:
            path /= kind
        if key:
            path /= '{}.key'.format(key)
        return path

    def object_exists(self, kind, key):
        return str(self.path_for(kind, key)) in self._zip.namelist()

    def save_object(self, kind, key, obj, force_unique, clobber):
        raise NotImplementedError("Read-only object store.")

    def load_object(self, kind, key):
        path = self.path_for(kind, key)
        if not self.object_exists(kind, key):
            raise ValueError("No object with kind {} and key {}.".format(kind, key))
        with self._zip.open(str(path), 'r') as f:
            obj = dill.load(f)
        return obj

    def load_objects(self, kind):
        directory = str(self.path_for(kind))
        object_files = [
            s for s in self._zip.namelist()
            if s.startswith(directory) and s.endswith('.key')]
        objects = {}
        for o in object_files:
            with self._zip.open(o, 'r') as f:
                obj = dill.load(f)
            objects[split_path(o, self._zip_root)] = obj
        return objects

    def n_objects(self, kind=None):
        raise NotImplementedError("Abstract method.")

    def keys(self, kind):
        directory = str(self.path_for(kind))
        _keys = [
            split_path(s, self._zip_root) for s in self._zip.namelist()
            if s.startswith(directory) and s.endswith('.key')]
        return _keys


def zip_root(zipfile):
    """ Get the name of the root directory of a zip file, if it has one. """
    if isinstance(zipfile, str):
        zipfile = ZipFile(zipfile, 'r')
    assert isinstance(zipfile, ZipFile)
    zip_root = min(
        [z.filename for z in zipfile.infolist()],
        key=lambda s: len(s))
    return zip_root


# From py.test
class KeywordMapping(object):
    """ Provides a local mapping for keywords.

        Can be used to implement user-friendly name selection
        using boolean expressions.

        names=[orange], pattern = "ora and e" -> True
        names=[orange], pattern = "orang" -> True
        names=[orange], pattern = "orane" -> False
        names=[orange], pattern = "ora and z" -> False
        names=[orange], pattern = "ora or z" -> True

        Given a list of names, map any string that is a substring
        of one of those names to True.

        ``names`` are the things we are trying to select, ``pattern``
        is the thing we are using to select them. Note that supplying
        multiple names does not mean "apply the pattern to each one
        separately". Rather, we are selecting the list as a whole,
        which doesn't seem that useful. The different names should be
        thought of as different names for a single object.

    """
    def __init__(self, names):
        self._names = names

    def __getitem__(self, subname):
        if subname is "_":
            return True

        for name in self._names:
            if subname in name:
                return True
        return False

    def eval(self, pattern):
        return eval(pattern, {}, self)

    @staticmethod
    def batch(batch, pattern):
        """ Apply a single pattern to a batch of names. """
        return [KeywordMapping([b]).eval(pattern) for b in batch]


def run_command(args):
    job = Job(args.path)
    job.run(args.pattern, args.indices)


def view_command(args):
    job = Job(args.path)
    print(job.summary())


def parallel_cl():
    parser = argparse.ArgumentParser(description='Run jobs and view their statuses.')

    parser.add_argument(
        '--pdb', action='store_true', help="If supplied, enter post-mortem debugging on error.")
    parser.add_argument(
        '-v', '--verbose', action='count', default=0, help="Increase verbosity.")

    subparsers = parser.add_subparsers()

    run_parser = subparsers.add_parser('run', help='Run a job.')
    run_parser.add_argument('path', type=str)
    run_parser.add_argument('pattern', type=str)
    run_parser.add_argument('indices', nargs='*', type=int)

    run_parser.set_defaults(func=run_command)

    view_parser = subparsers.add_parser('view', help='View status of a job.')
    view_parser.add_argument('path', type=str)

    view_parser.set_defaults(func=view_command)

    args = parser.parse_args()

    if args.pdb:
        with pdb_postmortem():
            args.func(args)
    else:
        args.func(args)


@contextmanager
def pdb_postmortem():
    try:
        yield
    except:
        type, value, tb = sys.exc_info()
        traceback.print_exc()
        pdb.post_mortem(tb)


if __name__ == "__main__":
    directory = Path('/tmp/test_job/test')
    try:
        shutil.rmtree(str(directory))
    except:
        pass
    job = Job(directory)
    x = range(10)
    z = job.map(lambda y: y + 1, x)
    final = job.reduce(lambda *inputs: sum(inputs), z)
    print(job.summary())
    # for i in range(10):
    #     job.run("map", i)
    # job.run("reduce", 0)
    # print(job.summary())
