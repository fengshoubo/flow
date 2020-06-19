"""
Implementations for Flow-transformers.

Of particular interest are:

* `Affine`: affine transformation.
* `DSF`: Deep Sigmoidal Flow.
"""

from functools import partial

import torch
from torch import nn, optim
import torch.nn.functional as F

from .flow import Transformer
from .modules import LogSigmoid, LeakyReLU, softplus_inv
from .utils import *


class Affine(Transformer):
    """Affine Transformer.
    """

    def __init__(self, eps=1e-6, **kwargs):
        """
        Args:
            eps (float): lower-bound for scale parameter.
        """

        _h_dim = 2
        h_dim = kwargs.pop('h_dim', _h_dim)
        assert h_dim == _h_dim, f'Received h_dim={h_dim} but expected {_h_dim}'

        super().__init__(h_dim=h_dim, **kwargs)

        self.eps = eps

    def _log_det(self, scale):
        return torch.log(scale).sum(dim=1)

    def _activation(self, h):
        """Returns (loc, scale) parameters."""
        assert not h.size(1) % self.h_dim

        loc, scale = h[:, ::2], h[:, 1::2]
        scale = F.softplus(scale) + self.eps

        return loc, scale

    def _transform(self, x, *h, log_det=False, **kwargs):
        loc, scale = h
        u = x * scale + loc

        if log_det:
            return u, self._log_det(scale)
        else:
            return u

    def _invert(self, u, *h, log_det=False, **kwargs):
        loc, scale = h
        x = (u - loc) / scale

        if log_det:
            return x, -self._log_det(scale)
        else:
            return x


class IncreasingMonotonicTransformer(Transformer):
    """Abstract Transformer that inverts using Bijection Search, 
        specific for increasing monotonic transformers.

    Note that using this method, inversion will not be differentiable.
    Uses `flow.utils.monotonic_increasing_bijective_search`.
    """

    def __init__(self, inv_eps=1e-3, inv_steps=1000, **kwargs):
        """
        Args:
            inv_eps (float): minimum difference between f(u) and x
                allowed to stop the inversion.
            inv_steps (int): maximum number of iterations 
                before halting execution. If 0 (default) no maximum defined.
            inv_alpha (float): alpha parameter for the inversion method.
        """

        super().__init__(**kwargs)

        self.inv_eps = inv_eps
        self.inv_steps = inv_steps

    def _invert(self, u, *h, log_det=False, **kwargs):
        x = monotonic_increasing_bijective_search(
            # use _transform, but without log_det
            self._transform, u, *h, **kwargs,
            eps=self.inv_eps, max_steps=self.inv_steps
        )

        if log_det:
            _, log_det = self._transform(u, *h, log_det=True, **kwargs)
            return x, -log_det
        else:
            return x


class NewtonInvTransformer(Transformer):
    """Abstract Transformer that inverts using the Newton-Raphson method.

    Note that using this method, inversion will not be differentiable.
    """

    def __init__(self, inv_eps=1e-3, inv_steps=1000, inv_init=None, lr=1., **kwargs):
        """        
        Args:
            inv_eps (float): minimum difference between f(u) and x
                allowed to stop the inversion.
            inv_steps (int): maximum number of iterations 
                before halting execution. If 0 (default) no maximum defined.
            inv_init (function): function used to inicialize u.
                If None, u = x.
        """

        super().__init__(reversed=reversed, **kwargs)

        self.inv_eps = inv_eps
        self.inv_steps = inv_steps
        self.inv_init = inv_init
        self.lr = lr

    def _invert(self, u, *h, log_det=False, **kwargs):
        # Inversion by Newton-Raphson: f(x) - u = 0
        if self.inv_init is not None:
            x = self.inv_init(u, *h, **kwargs)
        else:
            x = monotonic_increasing_bijective_search(
                self._transform,
                u, *h, **kwargs,
                eps=self.inv_eps, max_steps=10
            )

        step = 1
        with torch.no_grad():
            while True:
                f_x, log_det_i = self._transform(
                    x, *h, log_det=True, **kwargs
                )

                if (f_x - u).abs().max() < self.inv_eps or (
                    self.inv_steps and step >= self.inv_steps
                ):
                    break
                else:
                    x = x - self.lr * (f_x - u) / \
                        torch.exp(log_det_i.unsqueeze(1))
                    step += 1

            if log_det:
                # Execute transform again to recover log_det
                _, log_det = self._transform(
                    x, *h, log_det=True, **kwargs
                )
                return x, -log_det
            else:
                return x


class AdamInvTransformer(Transformer):
    """Abstract Transformer that inverts using the Adam optimizer.

    Note that using this method, inversion will not be differentiable.

    **CAUTION**: for any inheriting Transformers, 
    if you need to pass tensors as **kwargs to _invert, don't pass them inside
    lists or any another collection, pass them directly.
    Otherwise, _invert would run through their graph multiple times 
    and result in an Exception. See _invert for more details.
    """

    def __init__(
        self, 
        inv_lr=1e-1, inv_eps=1e-3, inv_steps=1000, 
        inv_init=None, **kwargs
    ):
        """
        Args:
            inv_lr (float): learning rate for the Adam optimizer.
                Quite high by default (1e-1) in order to make sampling fast.
                For more precision, use inv_lr=1e-3 and inv_steps >= 10000
            inv_eps (float): minimum difference between f(u) and x squared
                allowed to stop the inversion.
            inv_steps (int): maximum number of iterations 
                before halting execution. If 0 (default) no maximum defined.
            inv_init (function): function used to inicialize u.
                If None, u = torch.randn_like(x).
        """

        super().__init__(reversed=reversed, **kwargs)

        self.inv_lr = inv_lr
        self.inv_eps = inv_eps
        self.inv_steps = inv_steps
        self.inv_init = inv_init

    def _invert(self, u, *h, log_det=False, **kwargs):
        # _invert should be called inside a torch.no_grad(), 
        # since this operation will not be invertible
        with torch.no_grad():
            # Avoid running twice through the graph
            u = u.clone()
            h = tuple(hi.clone() for hi in h)

            for k, v in kwargs.items():
                if isinstance(v, torch.Tensor):
                    kwargs[k] = v.clone()

            if self.inv_init is None:
                x = nn.Parameter(torch.randn_like(u))
            else:
                x = nn.Parameter(self.inv_init(u, *h, **kwargs))
            
            # Howewer, we do need to enable gradients here to use the optimizer.
            with torch.enable_grad(): 
                optimizer = optim.Adam([x], lr=self.inv_lr)
                
                for _ in range(self.inv_steps):
                    loss = (
                        (u - self._transform(x, *h, **kwargs)) ** 2
                    ).mean()
                    
                    if loss.item() < self.inv_eps:
                        break
                    
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

                x = x.data # get the data from the parameter
                
            if log_det:
                _, log_det = self._transform(
                    x, *h, **kwargs, log_det=True
                )
                log_det = -log_det # we're inverting
                
                return x, log_det
            else:
                return x


class NonAffine(AdamInvTransformer):
    '''Non-affine transformer.

    https://arxiv.org/abs/1912.02762
    '''

    def __init__(self, k=16, nl=LeakyReLU, eps=1e-6, **kwargs):
        """
        Args:
            k (int): number of components of the conic combination.
            nl (class): non-linearity Flow to use in each component.
                Defaults to `flow.modules.LeakyReLU`.
            eps (float): lower-bound to strictly-positive h parameters.
        """

        _h_dim = 3 * k + 1
        h_dim = kwargs.pop('h_dim', _h_dim)
        assert h_dim == _h_dim, f'Received h_dim={h_dim} but expected {_h_dim}'

        super().__init__(h_dim=h_dim, **kwargs)

        self.k = k
        self.nl = nl()
        self.eps = eps

    def _activation(self, h):
        """Returns (weight, loc, scale, bias) parameters."""
        assert not h.size(1) % self.h_dim

        h = h.view(h.size(0), -1, self.h_dim)
        weight, loc, scale = h[..., :-1:3], h[..., 1:-1:3], h[..., 2:-1:3]
        bias = h[..., -1]

        scale = F.softplus(scale) + self.eps
        weight = F.softplus(weight) + self.eps

        return weight, loc, scale, bias

    def _transform(self, u, *h, log_det=False, **kwargs):
        weight, loc, scale, bias = h
        z = u.unsqueeze(2) * scale + loc
        
        nl_res = self.nl(z, log_det=log_det)
        if log_det:
            nl_z, log_det_i = nl_res

            log_det_i = log_sum_exp_trick(
                torch.log(weight) + log_det_i + torch.log(scale)
            ).sum(dim=1)
        else:
            nl_z = nl_res

        x = (nl_z * weight).sum(dim=2) + bias

        if log_det:
            return x, log_det_i
        else:
            return x


class DSF(AdamInvTransformer):
    """Deep Sigmoidal Flow.

    https://arxiv.org/abs/1804.00779
    """

    def __init__(self, k=16, eps=1e-6, alpha=1., **kwargs):
        """
        Args:
            - k (int): number of components of the conic combination.
            - eps (float): lower-bound to strictly-positive h parameters.
            - alpha (float): alpha parameter for the sigmoid. Defaults to 1.
        """

        _h_dim = 3 * k
        h_dim = kwargs.pop('h_dim', _h_dim)
        assert h_dim == _h_dim, f'Received h_dim={h_dim} but expected {_h_dim}'

        super().__init__(h_dim=h_dim, **kwargs)

        self.k = k
        self.eps = eps
        self.ls = LogSigmoid(dim=self.dim, alpha=alpha, eps=eps)

    def _activation(self, h):
        """Returns (loc, scale, w, loc_post, scale_post) parameters."""
        assert not h.size(1) % self.h_dim, (h.size(1), self.h_dim)

        h = h.view(h.size(0), -1, self.h_dim)

        loc, scale, w = h[..., ::3], h[..., 1::3], h[..., 2::3]
        
        scale = F.softplus(scale) + self.eps
        log_w = F.log_softmax(w, dim=2)
        
        return loc, scale, log_w

    def _transform(self, x, *h, log_det=False, **kwargs):
        # TODO: Avoid computing log_det if not requested
        loc, scale, log_w = h

        z, log_det_z = self.ls(scale * x.unsqueeze(2) + loc, log_det=True)
        z2 = log_sum_exp_trick(log_w + z)
        u, log_det_u = self.ls(z2, invert=True, log_det=True)

        if log_det:
            log_det = (
                log_det_u +
                -z2 +
                log_sum_exp_trick(log_w + z + log_det_z + torch.log(scale))
            ).sum(dim=1)

            return u, log_det
        else:
            return u