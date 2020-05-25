import itertools
from numbers import Number
from typing import List, Callable, Union, Sequence

import numpy as np
import tensorflow_probability as tfp

from active_learning_ratio_estimation.util import ensure_2d, ensure_array, stack_repeat, build_simulator, concat_repeat

tfd = tfp.distributions


def build_unparameterized_input(x):
    return ensure_2d(x)


def build_singly_parameterized_input(x, theta_1s):
    return np.concatenate([ensure_2d(x), ensure_2d(theta_1s)], axis=1)


class ParamIterator:

    def __init__(self, values: List[np.ndarray]):
        self.values = [val.astype(np.float32) for val in values]

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, item):
        return self.values.__getitem__(item)

    def __len__(self):
        return len(self.values)


class SingleParamIterator(ParamIterator):
    # TODO: maybe delete

    def __init__(self, theta: Union[Number, np.array], n_samples: int = 1):
        if isinstance(theta, Number):
            theta = np.array([theta])

        values = list(itertools.repeat(theta, n_samples))
        super().__init__(values)


class ParamGrid(ParamIterator):

    def __init__(self, bounds: Sequence[Sequence[float]], num: Union[int, Sequence[int]]):
        if not isinstance(num, Sequence):
            num = [num]*len(bounds)
        self.linspaces = [np.linspace(*bounds_i, num=num[i]).astype(np.float32)
                          for i, bounds_i in enumerate(bounds)]
        values = itertools.product(*self.linspaces)
        values = map(np.array, values)
        values = list(values)
        super().__init__(values)

    def meshgrid(self, **kwargs):
        return np.meshgrid(*self.linspaces, **kwargs)


class DistributionParamIterator(ParamIterator):

    def __init__(self, theta_dist: tfd.Distribution, n_samples: int):
        arr = theta_dist.sample(n_samples).numpy().tolist()
        super().__init__(arr)


class RatioDataset:

    def __init__(self,
                 x: np.array,
                 theta_0s: np.array,
                 theta_1s: np.array,
                 y: np.array = None,
                 nllr: np.array = None,
                 shuffle: bool = True):
        self.x = ensure_2d(x)
        self.theta_0s = theta_0s
        self.theta_1s = theta_1s
        self.y = y
        self.nllr = nllr

        # check arrays have same length
        arrs = [self.x, self.theta_0s, self.theta_1s]
        for arr in (y, nllr):
            if arr is not None:
                arrs.append(arr)
        if len(set(map(len, arrs))) != 1:
            raise ValueError('Arrays have different lengths')

        if shuffle:
            self.shuffle()

    def shuffle(self):
        p = np.random.permutation(len(self.x))
        self.x = self.x[p]
        self.theta_0s = self.theta_0s[p]
        self.theta_1s = self.theta_1s[p]

        # shuffle y and nllr if they have been given
        for arr_name in ('y', 'nllr'):
            try:
                arr = getattr(self, arr_name)
                setattr(self, arr_name, arr[p])
            except TypeError:
                # arr is None
                pass

    def build_input(self):
        raise NotImplementedError

    def __len__(self):
        return len(self.x)


class UnparameterizedRatioDataset(RatioDataset):

    def __init__(self,
                 simulator_func: Callable,
                 theta_0: Union[Number, np.array],
                 theta_1: Union[Number, np.array],
                 n_samples_per_theta: int,
                 shuffle: bool = True):
        theta_0, theta_1 = map(ensure_array, [theta_0, theta_1])
        assert len(theta_0.shape) == 1 == len(theta_1.shape)
        self.theta_0 = theta_0
        self.theta_1 = theta_1

        sim0 = build_simulator(simulator_func, theta_0)
        sim1 = build_simulator(simulator_func, theta_1)
        x0 = sim0.sample(n_samples_per_theta).numpy()
        x1 = sim1.sample(n_samples_per_theta).numpy()
        y0 = np.zeros(len(x0))
        y1 = np.ones_like(y0)
        x = np.concatenate([x0, x1], axis=0)
        y = np.concatenate([y0, y1], axis=0)
        theta_0s = stack_repeat(theta_0, len(x))
        theta_1s = stack_repeat(theta_1, len(x))
        super().__init__(x=x, y=y, theta_0s=theta_0s, theta_1s=theta_1s, shuffle=shuffle)

    def build_input(self):
        return build_unparameterized_input(self.x)


class SinglyParameterizedRatioDataset(RatioDataset):

    def __init__(self,
                 simulator_func: Callable,
                 theta_0: Union[Number, np.ndarray],
                 theta_1_iterator: ParamIterator,
                 n_samples_per_theta: int,
                 shuffle: bool = True,
                 include_nllr: bool = True):

        theta_0 = ensure_array(theta_0)
        assert len(theta_0.shape) == 1
        self.theta_0 = theta_0

        sim0 = build_simulator(simulator_func, theta_0)
        x0 = sim0.sample(n_samples_per_theta*len(theta_1_iterator)).numpy()
        x0 = ensure_2d(x0)
        y0 = np.zeros(len(x0))
        x1 = np.zeros_like(x0)
        theta_1s = np.zeros((len(x1), len(theta_0)))

        if include_nllr:
            ll0_x0 = np.zeros_like(y0)
            ll0_x1 = np.zeros_like(y0)
            ll1_x0 = np.zeros_like(y0)
            ll1_x1 = np.zeros_like(y0)

        for i, theta_1 in enumerate(theta_1_iterator):
            # TODO: perhaps wrap this in a tf.function
            sim1 = build_simulator(simulator_func, theta_1)
            start = i*n_samples_per_theta
            stop = (i+1)*n_samples_per_theta
            x_i = sim1.sample(n_samples_per_theta).numpy()
            x1[start:stop, :] = ensure_2d(x_i)
            theta_1s[start:stop, :] = theta_1
            if include_nllr:
                ll0_x0[start:stop] = sim0.log_prob(x0[start:stop, :]).numpy().squeeze()
                ll0_x0[start:stop] = sim0.log_prob(x0[start:stop, :]).numpy().squeeze()
                ll1_x1[start:stop] = sim1.log_prob(x1[start:stop, :]).numpy().squeeze()
                ll1_x1[start:stop] = sim1.log_prob(x1[start:stop, :]).numpy().squeeze()

        y1 = np.ones_like(y0)
        x = np.concatenate([x0, x1], axis=0)
        y = np.concatenate([y0, y1], axis=0)
        theta_0s = stack_repeat(theta_0, len(x))
        theta_1s = concat_repeat(theta_1s, 2, axis=0)

        if include_nllr:
            ll0 = np.concatenate([ll0_x0, ll0_x1], axis=0)
            ll1 = np.concatenate([ll1_x0, ll1_x1], axis=0)
            nllr = -(ll1 - ll0)
        else:
            nllr = None

        super().__init__(x=x, y=y, theta_0s=theta_0s, theta_1s=theta_1s, nllr=nllr, shuffle=shuffle)

    def build_input(self):
        return build_singly_parameterized_input(x=self.x, theta_1s=self.theta_1s)


if __name__ == '__main__':
    simulator_func = lambda x, y: tfd.Normal(loc=x, scale=y)
    theta_0 = np.array([0.0, 1.0])
    theta_1_iter = ParamGrid(bounds=[(0, 1), (0, 10)], num=10)
    ds = SinglyParameterizedRatioDataset(
        simulator_func=simulator_func,
        theta_0=theta_0,
        theta_1_iterator=theta_1_iter,
        n_samples_per_theta=10
    )
    pass
