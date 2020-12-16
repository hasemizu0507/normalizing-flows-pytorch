import torch
import torch.nn as nn
import torch.autograd

from .glow import Actnorm
from .modules import LinearSN


class InvResBlock(nn.Module):
    def __init__(self, in_out_channels, base_filters=32):
        super(InvResBlock, self).__init__()
        self.linear = nn.Sequential(
            nn.ELU(),
            LinearSN(in_out_channels, base_filters),
            nn.ELU(),
            LinearSN(base_filters, base_filters),
            nn.ELU(),
            LinearSN(base_filters, in_out_channels),
        )

    def forward(self, y, log_det_jacobians):
        n_iters = 8  # number of terms for approximating infinite series
        n_samples = 1  # number of samples for Hutchinson approximation

        B, C = y.size()
        g_y = self.linear(y)

        v = torch.randn([B, n_samples, g_y.size(1)]).to(y.device)
        w_J_fn = lambda w, y=y, g_y=g_y: torch.autograd.grad(
            g_y, y, grad_outputs=w, retain_graph=True, create_graph=True)[0]

        log_det = 0.0
        w = v.clone()
        for k in range(1, n_iters + 1):
            new_w = [w_J_fn(w[:, i, :]) for i in range(n_samples)]
            w = torch.stack(new_w, dim=1)

            inner = torch.einsum('bnd,bnd->bn', w, v)
            if (k + 1) % 2 == 0:
                log_det += inner / k
            else:
                log_det -= inner / k

        z = y + g_y
        log_det_jacobians += torch.mean(log_det, dim=1)
        return z, log_det_jacobians

    def backward(self, z, log_det_jacobians):
        n_iters = 100
        y = z.clone()
        for k in range(n_iters):
            g_y = self.linear(y)
            y, prev_y = z - g_y, y

            if torch.all(torch.abs(y - prev_y) < 1.0e-4):
                break

        log_det = torch.zeros_like(log_det_jacobians)
        _, log_det = self.forward(y, log_det)

        return y, log_det_jacobians - log_det


class InvResNet(nn.Module):
    def __init__(self, n_dims, n_layers=8):
        super(InvResNet, self).__init__()

        self.n_dims = n_dims
        self.n_layers = n_layers

        actnorms = []
        layers = []
        for i in range(self.n_layers):
            actnorms.append(Actnorm(n_dims))
            layers.append(InvResBlock(n_dims))

        self.actnorms = nn.ModuleList(actnorms)
        self.layers = nn.ModuleList(layers)

    def forward(self, y):
        z = y
        log_det_jacobians = torch.zeros_like(y[:, 0])
        for i in range(self.n_layers):
            z, log_det_jacobians = self.actnorms[i](z, log_det_jacobians)
            z, log_det_jacobians = self.layers[i](z, log_det_jacobians)

        return z, log_det_jacobians

    def backward(self, z):
        y = z
        log_det_jacobians = torch.zeros_like(z[:, 0])
        for i in reversed(range(self.n_layers)):
            y, log_det_jacobians = self.layers[i].backward(y, log_det_jacobians)
            y, log_det_jacobians = self.actnorms[i].backward(y, log_det_jacobians)

        return y, log_det_jacobians
