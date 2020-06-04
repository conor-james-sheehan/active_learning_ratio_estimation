import logging
from numbers import Number
from typing import Union, Callable, List, Dict

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import WhiteKernel, RBF
import tensorflow as tf

from active_learning_ratio_estimation.active_learning.acquisition_functions import acquisition_functions
from active_learning_ratio_estimation.dataset import ParamGrid, SinglyParameterizedRatioDataset, SingleParamIterator, \
    ParamIterator, build_singly_parameterized_input
from active_learning_ratio_estimation.model import SinglyParameterizedRatioModel
from active_learning_ratio_estimation.util import ensure_array, ideal_classifier_probs

logger = logging.getLogger(__name__)


def _get_best_epoch_information(keras_model: tf.keras.Model):
    history = pd.DataFrame(keras_model.history.history)
    best_epoch = history[history['val_loss'] == history['val_loss'].max()]
    return best_epoch.squeeze().to_dict()


class ActiveLearner:
    def __init__(self,
                 simulator_func: Callable,
                 theta_0: Union[Number, np.ndarray],
                 theta_1_iterator: ParamIterator,
                 n_samples_per_theta: int,
                 ratio_model: SinglyParameterizedRatioModel,
                 total_param_grid: ParamGrid,
                 test_dataset: SinglyParameterizedRatioDataset = None,
                 acquisition_function: Union[str, Callable] = 'entropy',
                 ucb_kappa: float = 1.0,
                 mc_samples: float = 100,
                 validation_mode: bool = False,
                 gp_kwargs: Dict = None
                 ):
        self.theta_0 = theta_0
        self.n_samples_per_theta = n_samples_per_theta
        self.dataset = SinglyParameterizedRatioDataset.from_simulator(
            simulator_func=simulator_func,
            theta_0=theta_0,
            theta_1_iterator=theta_1_iterator,
            n_samples_per_theta=n_samples_per_theta,
        )
        self.ratio_model = ratio_model
        self.model_fit()
        self.param_grid = total_param_grid
        self._trialed_mask = np.array([np.array(total_param_grid.values) == theta
                                       for theta in theta_1_iterator]).all(axis=2).sum(axis=0).astype(bool)
        self.simulator_func = simulator_func
        self.test_dataset = test_dataset
        if test_dataset is not None:
            if test_dataset.log_prob_0 is None or test_dataset.log_prob_1 is None:
                raise RuntimeError('Test dataset must have log probabilities of data points; '
                                   'pass include_log_probs=True to its from_simulator constructor.')
        self._train_history = []
        self._test_history = []
        if isinstance(acquisition_function, str) and acquisition_function != 'random':
            acquisition_function = acquisition_functions[acquisition_function]
        self.acquisition_function = acquisition_function
        self.ucb_kappa = ucb_kappa

        self.mc_samples = mc_samples

        self.validation_mode = validation_mode
        if validation_mode:
            self.full_dataset = SinglyParameterizedRatioDataset.from_simulator(
                simulator_func=simulator_func,
                theta_0=theta_0,
                theta_1_iterator=self.param_grid,
                n_samples_per_theta=n_samples_per_theta
            )
        else:
            self.full_dataset = None

        self.gp = None
        self.gp_kwargs = gp_kwargs
        self.acquisition_history = []

    def fit(self, n_iter: int):
        for i in range(n_iter):
            logger.info(f'Active learning iteration {i + 1}/{n_iter}')
            self.step()
        return self

    def model_fit(self):
        self.ratio_model.fit(self.dataset)

    def model_eval(self):
        probs = self.ratio_model.predict_proba_dataset(self.test_dataset)
        l0, l1 = map(np.exp, [self.test_dataset.log_prob_0, self.test_dataset.log_prob_1])
        ideal_probs = ideal_classifier_probs(l0, l1)
        ideal_probs = np.hstack([1 - ideal_probs, ideal_probs])
        squared_error = (probs - ideal_probs) ** 2
        return squared_error.mean()

    @property
    def all_thetas(self):
        return self.param_grid.array

    @property
    def trialed_thetas(self):
        return self.param_grid.array[self._trialed_mask]

    @property
    def remaining_thetas(self):
        return self.param_grid.array[~self._trialed_mask]

    @property
    def test_history(self):
        return pd.DataFrame(self._test_history)

    @property
    def train_history(self):
        return pd.DataFrame(self._train_history)

    def plot_acquisition_history_item(self, item=-1, ax=None):
        acq_hist_item = self.acquisition_history[item]
        if ax is None:
            _, ax = plt.subplots()
        acq_hist_item['Training'].plot(marker='o', color='r', ax=ax)
        acq_hist_item['Prediction'].plot(color='b', ax=ax)
        try:
            acq_hist_item['Validation'].plot(ax=ax)
        except KeyError:
            pass
        ax.legend()
        return ax

    def step(self):
        next_theta_index = self.choose_next_theta_index()
        next_theta = self.all_thetas[next_theta_index]
        logger.info(f'Adding theta = {next_theta} to labeled data.')

        new_ds = SinglyParameterizedRatioDataset.from_simulator(
            simulator_func=self.simulator_func,
            theta_0=self.theta_0,
            theta_1_iterator=SingleParamIterator(next_theta),
            n_samples_per_theta=self.n_samples_per_theta,
        )
        self.dataset += new_ds
        self.dataset.shuffle()

        logger.info('Fitting ratio model')
        self.model_fit()
        training_info = _get_best_epoch_information(self.ratio_model.keras_model_)
        self._train_history.append(training_info)
        logger.info('Finished fitting ratio model. Best epoch information: '
                    + ', '.join([f'{name}={val:.2E}' for name, val in training_info.items()]))

        if self.test_dataset is not None:
            logger.info('Evaluating MSE on test dataset')
            mse = self.model_eval()
            logger.info(f'Test MSE: {mse:.2E}')
            self._test_history.append(dict(mse=mse))

        assert self._trialed_mask[next_theta_index] == 0
        self._trialed_mask[next_theta_index] = 1

    def calculate_marginalised_acquisition(self, dataset: SinglyParameterizedRatioDataset):
        U_theta = []
        for theta in np.unique(dataset.theta_1s, axis=0):
            mask = dataset.theta_1s == theta
            x = dataset.x[mask]
            # TODO: the following section assumes that the ratio model
            #  a) is Bayesian
            #  b) has a StandardScaler
            #  But this is not ideal as we might want to test some of the acquisition functions with a regular NN
            theta_1s = dataset.theta_1s[mask]
            model_input = build_singly_parameterized_input(x=x, theta_1s=theta_1s)
            scaler, clf = self.ratio_model.estimator
            model_input = scaler.transform(model_input)
            sampled_probs = clf.sample_predictive_distribution(model_input, samples=self.mc_samples)

            U_theta_x = self.acquisition_function(sampled_probs)
            assert U_theta_x.shape == (len(x),)
            U_theta.append(U_theta_x.mean())
        return np.array(U_theta)

    def _gp(self):
        if self.gp_kwargs is None:
            length_scale = np.array([linspace[-1] - linspace[0] for linspace in self.param_grid.linspaces])
            rbf = RBF(length_scale=length_scale / 10, length_scale_bounds='fixed')
            white_kernel = WhiteKernel()
            kwargs = dict(kernel=1 * rbf + white_kernel,
                          alpha=0,
                          n_restarts_optimizer=5,
                          normalize_y=True)
        else:
            kwargs = self.gp_kwargs
        return GaussianProcessRegressor(**kwargs)

    def choose_next_theta_index(self):
        if self.acquisition_function == 'random':
            choice_weights = 1 - self._trialed_mask
            choice_probs = choice_weights/choice_weights.sum()
            next_theta_index = np.random.choice(np.arange(len(self.all_thetas)), p=choice_probs)
        else:
            self.gp = self._gp()
            U_theta_train = self.calculate_marginalised_acquisition(self.dataset)
            self.gp.fit(self.trialed_thetas, U_theta_train)
            U_theta_pred, std = self.gp.predict(self.all_thetas, return_std=True)
            ucb = U_theta_pred + self.ucb_kappa * std
            ucb[self._trialed_mask] = -np.inf  # don't choose same theta twice
            next_theta_index = np.argmax(ucb)
            self._record_acq_history(U_theta_pred, U_theta_train)

        return next_theta_index

    def _record_acq_history(self, U_theta_pred, U_theta_train):
        # TODO: the following only works for 1D theta
        training_info = pd.Series(
            index=pd.Index(self.trialed_thetas.squeeze(), name='theta'),
            data=U_theta_train,
            name='U_theta_train'
        )
        prediction_info = pd.Series(
            index=pd.Index(self.all_thetas.squeeze(), name='theta'),
            data=U_theta_pred,
            name='U_theta_pred'
        )
        acquisition_hist_item = dict(Training=training_info, Prediction=prediction_info)
        if self.validation_mode:
            U_theta_true = self.calculate_marginalised_acquisition(self.full_dataset)
            validation_info = pd.Series(
                index=pd.Index(self.all_thetas.squeeze(), name='theta'),
                data=U_theta_true,
                name='U_theta_true'
            )
            acquisition_hist_item['Validation'] = validation_info

        self.acquisition_history.append(acquisition_hist_item)


def _gp_debug(X_train, X_test, y_train, mean, std):
    plt.figure()
    plt.plot(X_train, y_train, 'ro')
    plt.plot(X_test, mean)
    plt.plot(X_test, mean - std, 'g')
    plt.plot(X_test, mean + std, 'g')
