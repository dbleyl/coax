# ------------------------------------------------------------------------------------------------ #
# MIT License                                                                                      #
#                                                                                                  #
# Copyright (c) 2020, Microsoft Corporation                                                        #
#                                                                                                  #
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software    #
# and associated documentation files (the "Software"), to deal in the Software without             #
# restriction, including without limitation the rights to use, copy, modify, merge, publish,       #
# distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the    #
# Software is furnished to do so, subject to the following conditions:                             #
#                                                                                                  #
# The above copyright notice and this permission notice shall be included in all copies or         #
# substantial portions of the Software.                                                            #
#                                                                                                  #
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING    #
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND       #
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,     #
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,   #
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.          #
# ------------------------------------------------------------------------------------------------ #

from abc import ABC, abstractmethod

import jax
import jax.numpy as jnp
import haiku as hk
import optax

from .._base.mixins import RandomStateMixin
from .._core.base_policy import PolicyMixin
from .._core.value_v import V
from .._core.value_q import Q
from ..utils import get_grads_diagnostics
from ..value_losses import huber
from ..policy_regularizers import PolicyRegularizer


__all__ = (
    'BaseTDLearningV',
    'BaseTDLearningQ',
)


class BaseTDLearning(ABC, RandomStateMixin):
    def __init__(self, f, f_targ=None, optimizer=None, loss_function=None, policy_regularizer=None):

        self._f = f
        self._f_targ = f if f_targ is None else f_targ
        self.loss_function = huber if loss_function is None else loss_function

        if not isinstance(policy_regularizer, (PolicyRegularizer, type(None))):
            raise TypeError(
                f"policy_regularizer must be PolicyRegularizer, got: {type(policy_regularizer)}")
        self.policy_regularizer = policy_regularizer

        # optimizer
        self._optimizer = optax.adam(1e-3) if optimizer is None else optimizer
        self._optimizer_state = self.optimizer.init(self._f.params)

        def apply_grads_func(opt, opt_state, params, grads):
            updates, new_opt_state = opt.update(grads, opt_state, params)
            new_params = optax.apply_updates(params, updates)
            return new_opt_state, new_params

        self._apply_grads_func = jax.jit(apply_grads_func, static_argnums=0)

    @abstractmethod
    def target_func(self, target_params, target_state, rng, transition_batch):
        pass

    @property
    @abstractmethod
    def target_params(self):
        pass

    @property
    @abstractmethod
    def target_function_state(self):
        pass

    @property
    def hyperparams(self):
        return {}

    def update(self, transition_batch):
        r"""

        Update the model parameters (weights) of the underlying function approximator.

        Parameters
        ----------
        transition_batch : TransitionBatch

            A batch of transitions.

        Returns
        -------
        metrics : dict of scalar ndarrays

            The structure of the metrics dict is ``{name: score}``.

        """
        grads, function_state, metrics = self.grads_and_metrics(transition_batch)
        if any(jnp.any(jnp.isnan(g)) for g in jax.tree_leaves(grads)):
            raise RuntimeError(f"found nan's in grads: {grads}")
        self.update_from_grads(grads, function_state)
        return metrics

    def update_from_grads(self, grads, function_state):
        r"""

        Update the model parameters (weights) of the underlying function approximator given
        pre-computed gradients.

        This method is useful in situations in which computation of the gradients is deligated to a
        separate (remote) process.

        Parameters
        ----------
        grads : pytree with ndarray leaves

            A batch of gradients, generated by the :attr:`grads` method.

        function_state : pytree

            The internal state of the forward-pass function. See :attr:`Q.function_state
            <coax.Q.function_state>` and :func:`haiku.transform_with_state` for more details.

        """
        self._f.function_state = function_state
        self.optimizer_state, self._f.params = \
            self._apply_grads_func(self.optimizer, self.optimizer_state, self._f.params, grads)

    def grads_and_metrics(self, transition_batch):
        r"""

        Compute the gradients associated with a batch of transitions.

        Parameters
        ----------
        transition_batch : TransitionBatch

            A batch of transitions.

        Returns
        -------
        grads : pytree with ndarray leaves

            A batch of gradients.

        function_state : pytree

            The internal state of the forward-pass function. See :attr:`Q.function_state
            <coax.Q.function_state>` and :func:`haiku.transform_with_state` for more details.

        metrics : dict of scalar ndarrays

            The structure of the metrics dict is ``{name: score}``.

        """
        return self._grads_and_metrics_func(
            self._f.params, self.target_params, self._f.function_state, self.target_function_state,
            self._f.rng, transition_batch)

    def td_error(self, transition_batch):
        r"""

        Compute the TD-errors associated with a batch of transitions. We define the TD-error as the
        negative gradient of the :attr:`loss_function` with respect to the predicted value:

        .. math::

            \text{td_error}_i\ =\ -\frac{\partial L(y, \hat{y})}{\partial \hat{y}_i}

        Note that this reduces to the ordinary definition :math:`\text{td_error}=y-\hat{y}` when we
        use the :func:`coax.value_losses.mse` loss funtion.

        Parameters
        ----------
        transition_batch : TransitionBatch

            A batch of transitions.

        Returns
        -------
        td_errors : ndarray, shape: [batch_size]

            A batch of TD-errors.

        """
        return self._td_error_func(
            self._f.params, self.target_params, self._f.function_state, self.target_function_state,
            self._f.rng, transition_batch)

    @property
    def optimizer(self):
        return self._optimizer

    @optimizer.setter
    def optimizer(self, new_optimizer):
        new_optimizer_state_structure = jax.tree_structure(new_optimizer.init(self._f.params))
        if new_optimizer_state_structure != jax.tree_structure(self.optimizer_state):
            raise AttributeError("cannot set optimizer attr: mismatch in optimizer_state structure")
        self._optimizer = new_optimizer

    @property
    def optimizer_state(self):
        return self._optimizer_state

    @optimizer_state.setter
    def optimizer_state(self, new_optimizer_state):
        self._optimizer_state = new_optimizer_state


class BaseTDLearningV(BaseTDLearning):
    def __init__(self, v, v_targ=None, optimizer=None, loss_function=None, policy_regularizer=None):

        if not isinstance(v, V):
            raise TypeError(f"v must be a coax.V, got: {type(v)}")
        if not isinstance(v_targ, (V, type(None))):
            raise TypeError(f"v_targ must be a coax.Q or None, got: {type(v_targ)}")

        super().__init__(
            f=v,
            f_targ=v_targ,
            optimizer=optimizer,
            loss_function=loss_function,
            policy_regularizer=policy_regularizer)

        def loss_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S = transition_batch.S
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            V, state_new = self.v.function(params, state, next(rngs), S, True)

            # add policy regularization term to target
            if self.policy_regularizer is not None:
                dist_params, _ = self.policy_regularizer.pi.function(
                    target_params['reg_pi'], target_state['reg_pi'], next(rngs), S, False)
                reg = self.policy_regularizer.function(dist_params, **target_params['reg_hparams'])
                assert reg.shape == G.shape, f"bad shape: {G.shape} != {reg.shape}"
                G += -reg  # flip sign (typical example: reg = -beta * entropy)

            loss = self.loss_function(G, V)
            return loss, (loss, G, V, S, state_new)

        def grads_and_metrics_func(
                params, target_params, state, target_state, rng, transition_batch):

            rngs = hk.PRNGSequence(rng)
            grads, (loss, G, V, S, state_new) = jax.grad(loss_func, has_aux=True)(
                params, target_params, state, target_state, next(rngs), transition_batch)

            # target-network estimate
            V_targ, _ = self.v_targ.function(target_params['v_targ'], state, next(rngs), S, False)

            # residuals: estimate - better_estimate
            err = V - G
            err_targ = V_targ - V

            name = self.__class__.__name__
            metrics = {
                f'{name}/loss': loss,
                f'{name}/bias': jnp.mean(err),
                f'{name}/rmse': jnp.sqrt(jnp.mean(jnp.square(err))),
                f'{name}/bias_targ': jnp.mean(err_targ),
                f'{name}/rmse_targ': jnp.sqrt(jnp.mean(jnp.square(err_targ)))}

            # add some diagnostics of the gradients
            metrics.update(get_grads_diagnostics(grads, key_prefix=f'{name}/grads_'))

            return grads, state_new, metrics

        def td_error_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S = transition_batch.S
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            V, _ = self.v.function(params, state, next(rngs), S, False)

            # add policy regularization term to target
            if self.policy_regularizer is not None:
                dist_params, _ = self.policy_regularizer.pi.function(
                    target_params['reg_pi'], target_state['reg_pi'], next(rngs), S, False)
                reg = self.policy_regularizer.function(dist_params, **target_params['reg_hparams'])
                assert reg.shape == G.shape, f"bad shape: {G.shape} != {reg.shape}"
                G += -reg  # flip sign (typical example: reg = -beta * entropy)

            dL_dV = jax.grad(self.loss_function, argnums=1)
            return -dL_dV(G, V)

        self._grads_and_metrics_func = jax.jit(grads_and_metrics_func)
        self._td_error_func = jax.jit(td_error_func)

    @property
    def v(self):
        return self._f

    @property
    def v_targ(self):
        return self._f_targ

    @property
    def target_params(self):
        return hk.data_structures.to_immutable_dict({
            'v': self.v.params,
            'v_targ': self.v_targ.params,
            'reg_pi': getattr(getattr(self.policy_regularizer, 'pi', None), 'params', None),
            'reg_hparams': getattr(self.policy_regularizer, 'hyperparams', None)})

    @property
    def target_function_state(self):
        return hk.data_structures.to_immutable_dict({
            'v': self.v.function_state,
            'v_targ': self.v_targ.function_state,
            'reg_pi':
                getattr(getattr(self.policy_regularizer, 'pi', None), 'function_state', None)})


class BaseTDLearningQ(BaseTDLearning):
    def __init__(self, q, q_targ=None, optimizer=None, loss_function=None, policy_regularizer=None):

        if not isinstance(q, Q):
            raise TypeError(f"q must be a coax.Q, got: {type(q)}")
        if not isinstance(q_targ, (Q, type(None), list, tuple)):
            raise TypeError(f"q_targ must be a coax.Q or None, got: {type(q_targ)}")

        super().__init__(
            f=q,
            f_targ=q_targ,
            optimizer=optimizer,
            loss_function=loss_function,
            policy_regularizer=policy_regularizer)

        def loss_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S, A = transition_batch[:2]
            A = self.q.action_preprocessor(A)
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            Q, state_new = self.q.function_type1(params, state, next(rngs), S, A, True)

            # add policy regularization term to target
            if self.policy_regularizer is not None:
                dist_params, _ = self.policy_regularizer.pi.function(
                    target_params['reg_pi'], target_state['reg_pi'], next(rngs), S, False)
                reg = self.policy_regularizer.function(dist_params, **target_params['reg_hparams'])
                assert reg.shape == G.shape, f"bad shape: {G.shape} != {reg.shape}"
                G += -reg  # flip sign (typical example: reg = -beta * entropy)

            loss = self.loss_function(G, Q)
            return loss, (loss, G, Q, S, A, state_new)

        def grads_and_metrics_func(
                params, target_params, state, target_state, rng, transition_batch):

            rngs = hk.PRNGSequence(rng)
            grads, (loss, G, Q, S, A, state_new) = jax.grad(loss_func, has_aux=True)(
                params, target_params, state, target_state, next(rngs), transition_batch)

            # target-network estimate
            Q_targ, _ = self.q_targ.function_type1(
                target_params['q_targ'], target_state['q_targ'], next(rngs), S, A, False)

            # residuals: estimate - better_estimate
            err = Q - G
            err_targ = Q_targ - Q

            name = self.__class__.__name__
            metrics = {
                f'{name}/loss': loss,
                f'{name}/bias': jnp.mean(err),
                f'{name}/rmse': jnp.sqrt(jnp.mean(jnp.square(err))),
                f'{name}/bias_targ': jnp.mean(err_targ),
                f'{name}/rmse_targ': jnp.sqrt(jnp.mean(jnp.square(err_targ)))}

            # add some diagnostics of the gradients
            metrics.update(get_grads_diagnostics(grads, key_prefix=f'{name}/grads_'))

            return grads, state_new, metrics

        def td_error_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S = transition_batch.S
            A = self.q.action_preprocessor(transition_batch.A)
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            Q, _ = self.q.function_type1(params, state, next(rngs), S, A, False)

            # add policy regularization term to target
            if self.policy_regularizer is not None:
                dist_params, _ = self.policy_regularizer.pi.function(
                    target_params['reg_pi'], target_state['reg_pi'], next(rngs), S, False)
                reg = self.policy_regularizer.function(dist_params, **target_params['reg_hparams'])
                assert reg.shape == G.shape, f"bad shape: {G.shape} != {reg.shape}"
                G += -reg  # flip sign (typical example: reg = -beta * entropy)

            dL_dQ = jax.grad(self.loss_function, argnums=1)
            return -dL_dQ(G, Q)

        def apply_grads_func(opt, opt_state, params, grads):
            updates, new_opt_state = opt.update(grads, opt_state)
            new_params = optax.apply_updates(params, updates)
            return new_opt_state, new_params

        self._apply_grads_func = jax.jit(apply_grads_func, static_argnums=0)
        self._grads_and_metrics_func = jax.jit(grads_and_metrics_func)
        self._td_error_func = jax.jit(td_error_func)

    @property
    def q(self):
        return self._f

    @property
    def q_targ(self):
        return self._f_targ

    @property
    def target_params(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.params,
            'q_targ': self.q_targ.params,
            'reg_pi': getattr(getattr(self.policy_regularizer, 'pi', None), 'params', None),
            'reg_hparams': getattr(self.policy_regularizer, 'hyperparams', None)})

    @property
    def target_function_state(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.function_state,
            'q_targ': self.q_targ.function_state,
            'reg_pi':
                getattr(getattr(self.policy_regularizer, 'pi', None), 'function_state', None)})


class BaseTDLearningQWithTargetPolicy(BaseTDLearningQ):
    def __init__(
            self, q, pi_targ, q_targ=None, optimizer=None,
            loss_function=None, policy_regularizer=None):

        if not isinstance(pi_targ, (PolicyMixin, type(None))):
            raise TypeError(f"pi_targ must be a Policy, got: {type(pi_targ)}")

        self.pi_targ = pi_targ
        super().__init__(
            q=q,
            q_targ=q_targ,
            optimizer=optimizer,
            loss_function=loss_function,
            policy_regularizer=policy_regularizer)

    @property
    def target_params(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.params,
            'q_targ': self.q_targ.params,
            'pi_targ': getattr(self.pi_targ, 'params', None),
            'reg_pi': getattr(getattr(self.policy_regularizer, 'pi', None), 'params', None),
            'reg_hparams': getattr(self.policy_regularizer, 'hyperparams', None)})

    @property
    def target_function_state(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.function_state,
            'q_targ': self.q_targ.function_state,
            'pi_targ': getattr(self.pi_targ, 'function_state', None),
            'reg_pi':
                getattr(getattr(self.policy_regularizer, 'pi', None), 'function_state', None)})
