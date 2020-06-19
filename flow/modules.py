"""
Miscellaneous Flows.
"""

import numpy as np
import torch
from torch import nn

from .flow import Flow
from .utils import softplus, softplus_inv, logsigmoid


class Affine(Flow):
    """Learnable Affine Flow.

    Applies weight[i] * x[i] + bias[i], 
    where weight and bias are learnable parameters.
    """

    def __init__(self, weight=None, bias=None, **kwargs):
        """
        Args:
            weight (torch.Tensor): initial value for the weight parameter. 
                If None, initialized to torch.ones(1, self.dim).
            bias (torch.Tensor): initial value for the bias parameter. 
                If None, initialized to torch.zeros(1, self.dim).
        """
        super().__init__(**kwargs)

        if weight is None:
            weight = torch.ones(1, self.dim)

        assert (weight > 0).all()
        self.log_weight = nn.Parameter(torch.log(weight))

        if bias is None:
            bias = torch.zeros(1, self.dim)

        self.bias = nn.Parameter(bias)

    def _log_det(self):
        """Used to compute _log_det for _transform."""
        return self.log_weight.sum(dim=1)

    def _h(self):
        """Compute the parameters for this flow."""
        return torch.exp(self.log_weight), self.bias

    def _transform(self, x, log_det=False, **kwargs):
        weight, bias = self._h()

        u = weight * x + bias

        if log_det:
            return u, self._log_det()
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        weight, bias = self._h()

        x = (u - bias) / weight

        if log_det:
            return x, -self._log_det()
        else:
            return x


class Sigmoid(Flow):
    """Sigmoid Flow."""

    def __init__(self, alpha=1., **kwargs):
        r"""
        Args:
            alpha (float): alpha parameter for the sigmoid function: 
                \(s(x, \alpha) = \frac{1}{1 + e^{-\alpha x}}\).
                Must be bigger than 0.
        """
        super().__init__(**kwargs)

        self.alpha = alpha

    def _log_det(self, x):
        """Return log|det J_T|, where T: x -> u."""

        return (
            np.log(self.alpha) + 
            2 * logsigmoid(x, alpha=alpha) +
            -self.alpha * x
        ).sum(dim=1)

    # Override methods
    def _transform(self, x, log_det=False, **kwargs):
        u = torch.sigmoid(self.alpha * x)

        if log_det:
            return u, self._log_det(x)
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        x = -torch.log(1 / self.alpha / u - 1)

        if log_det:
            return x, -self._log_det(x)
        else:
            return x


class Softplus(Flow):
    """Softplus Flow."""

    def __init__(self, threshold=20., eps=1e-6, **kwargs):
        """
        Args:
            threshold (float): values above this revert to a linear function. 
                Default: 20.
            eps (float): lower-bound to the softplus output.
        """
        super().__init__(**kwargs)

        assert threshold > 0 and eps > 0
        self.threshold = threshold
        self.eps = eps

    def _log_det(self, x):
        return logsigmoid(x).sum(dim=1)

    # Override methods
    def _transform(self, x, log_det=False, **kwargs):
        u = softplus(x, threshold=self.threshold, eps=self.eps)

        if log_det:
            return u, self._log_det(x)
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        x = softplus_inv(u, threshold=self.threshold, eps=self.eps)

        if log_det:
            return x, -self._log_det(x)
        else:
            return x 


class LogSigmoid(Flow):
    """LogSigmoid Flow, defined for numerical stability."""

    def __init__(self, alpha=1., **kwargs):
        """
        Args:
            alpha (float): alpha parameter used by the `Sigmoid`.
        """
        super().__init__(**kwargs)

        self.alpha = alpha

    def _log_det(self, x):
        """Return log|det J_T|, where T: x -> u."""

        return logsigmoid(-self.alpha * x) + np.log(self.alpha)

    # Override methods
    def _transform(self, x, log_det=False, **kwargs):
        u = logsigmoid(x, alpha=self.alpha)

        if log_det:
            return u, self._log_det(x)
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        x = -softplus_inv(-u) / self.alpha

        if log_det:
            return x, -self._log_det(x)
        else:
            return x
    

class LeakyReLU(Flow):
    """LeakyReLU Flow."""

    def __init__(self, negative_slope=0.01, **kwargs):
        """
        Args:
            negative_slope (float): slope used for those x < 0,
        """
        super().__init__(**kwargs)

        self.negative_slope = negative_slope

    def _log_det(self, x):
        return torch.where(
            x >= 0, 
            torch.zeros_like(x), 
            torch.ones_like(x) * np.log(self.negative_slope)
        )


    # Override methods
    def _transform(self, x, log_det=False, **kwargs):
        u = torch.where(x >= 0, x, x * self.negative_slope)

        if log_det:
            return u, self._log_det(x)
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        x = torch.where(u >= 0, u, u / self.negative_slope)

        if log_det:
            return x, -self._log_det(x)
        else:
            return x


class BatchNorm(Flow):
    """Perform BatchNormalization as a Flow class.

    Note that if you want ActNorm, you can set momentum to 0
    so that batch_statistics only take into account the first batch 
    passed through the system (including the one from warm_start).
    """

    def __init__(self, momentum=.1, eps=1e-5, **kwargs):
        """
        Args:
            momentum (float): value used for the moving average
                of batch statistics. Must be between 0 and 1.
            eps (float): lower-bound for the batch-scale tensor.
        """
        super().__init__(**kwargs)

        assert 0 <= momentum and momentum <= 1

        self.register_buffer('eps', torch.tensor(eps))
        self.register_buffer('momentum', torch.tensor(momentum))

        self.register_buffer('updates', torch.tensor(0))

        self.register_buffer('batch_loc', torch.zeros(1, self.dim))
        self.register_buffer('batch_scale', torch.ones(1, self.dim))

        self.loc = nn.Parameter(torch.zeros(1, self.dim))
        self.scale = nn.Parameter(torch.zeros(1, self.dim))

    def _update(self, x):
        with torch.no_grad():
            bloc = x.mean(0, keepdim=True)
            bscale = x.std(0, keepdim=True) + self.eps

            if self.updates.data == 0:
                self.batch_loc = bloc
                self.batch_scale = bscale
            else:
                m = self.momentum
                self.batch_loc = (1 - m) * self.batch_loc + m * bloc
                self.batch_scale = (1 - m) * self.batch_scale + m * bscale

            self.updates += 1

    def warm_start(self, x):
        self._update(x)

        return self

    def _activation(self, x=None, update=None):
        if update:
            assert x is not None
            self._update(x)

        bloc, bscale = self.batch_loc, self.batch_scale
        loc, scale = self.loc, self.scale

        # Note that batch_scale does not use activation,
        # since it should be set by warm_start directly.
        scale = torch.exp(scale) + self.eps

        return bloc, bscale, loc, scale

    def _log_det(self, scale, bscale):
        return (torch.log(scale) - torch.log(bscale)).sum(dim=1)

    def _transform(self, x, log_det=False, **kwargs):
        bloc, bscale, loc, scale = self._activation(x, update=self.training)
        u = scale * ((x - bloc) / bscale) + loc
        
        if log_det:
            log_det = self._log_det(scale, bscale)
            return u, log_det
        else:
            return u

    def _invert(self, u, log_det=False, **kwargs):
        bloc, bscale, loc, scale = self._activation(update=False)
        x = (u - loc) / scale * bscale + bloc
        
        if log_det:
            log_det = -self._log_det(scale, bscale)
            return x, log_det
        else:
            return x