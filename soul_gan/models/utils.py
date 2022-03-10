from pathlib import Path
from typing import Tuple

import numpy as np
import torch
from torch import nn
from tqdm import trange

from soul_gan.utils.general_utils import ROOT_DIR, DotConfig

from .base import BaseDiscriminator, BaseGenerator, ModelRegistry


# def stabilize_dis(dis, im_size=32, iters=5000, device=0):
#     for _ in trange(iters):
#         x = torch.rand(10, 3, im_size, im_size, device=device)
#         label = torch.LongTensor(np.random.randint(0, 10 - 1, len(x))).to(x.device)
#         dis.label = label
#         _ = dis(x)


# def stabilize_gen(gen, iters=500):
#     for _ in trange(iters):
#         x = gen.prior.sample((100,))
#         label = torch.LongTensor(np.random.randint(0, 10 - 1, len(x))).to(x.device)
#         gen.label = label
#         _ = gen(x)


# def load_gan(
#     config: DotConfig, device: torch.device, thermalize: bool=False
# ) -> Tuple[BaseGenerator, BaseDiscriminator]:
#     state_dict = torch.load(
#         Path(ROOT_DIR, config.generator.ckpt_path), map_location=device
#     )
#     gen = ModelRegistry.create_model(
#         config.generator.name, **config.generator.params
#     ).to(device)
#     gen.load_state_dict(state_dict, strict=True)

#     state_dict = torch.load(
#         Path(ROOT_DIR, config.discriminator.ckpt_path), map_location=device
#     )
#     dis = ModelRegistry.create_model(
#         config.discriminator.name, **config.discriminator.params
#     ).to(device)
#     dis.load_state_dict(state_dict, strict=True)

#     if config.dp:
#         gen = torch.nn.DataParallel(gen)
#         dis = torch.nn.DataParallel(dis)
#         dis.transform = dis.module.transform
#         dis.output_layer = dis.module.output_layer
#         gen.inverse_transform = gen.module.inverse_transform
#         gen.z_dim = gen.module.z_dim
#         gen.sample_label = gen.module.sample_label

#         if hasattr(gen.module, "label"):
#             gen.label = gen.module.label
#         if hasattr(dis.module, "label"):
#             dis.label = dis.module.label

#     if config.prior == "normal":
#         prior = torch.distributions.multivariate_normal.MultivariateNormal(
#             torch.zeros(gen.z_dim).to(device), torch.eye(gen.z_dim).to(device)
#         )
#         prior.project = lambda z: z
#     elif config.prior == "uniform":
#         prior = torch.distributions.uniform.Uniform(
#             -torch.ones(gen.z_dim).to(device), torch.ones(gen.z_dim).to(device)
#         )
#         prior.project = lambda z: torch.clip(z, -1 + 1e-9, 1 - 1e-9)
#         prior.log_prob = lambda z: torch.zeros(z.shape[0], device=z.device)
#     else:
#         raise KeyError
#     gen.prior = prior

#     dis.real_score = config.real_score
#     dis.fake_score = config.fake_score

#     for param in gen.parameters():
#         param.requires_grad = True
#     for param in dis.parameters():
#         param.requires_grad = True

#     # if thermalize:
#     #     stabilize_dis(dis, device=device)
#     #     stabilize_gen(gen)
#     gen.eval()
#     dis.eval()

#     return gen, dis


class GANWrapper:
    def __init__(self, config: DotConfig, device: torch.device):
        self.config = config
        self.device = device

        self.gen = ModelRegistry.create_model(
            config.generator.name, **config.generator.params
        ).to(device)
        self.dis = ModelRegistry.create_model(
            config.discriminator.name, **config.discriminator.params
        ).to(device)

        self.load_weights()

        if config.dp:
            self.gen = torch.nn.DataParallel(self.gen)
            self.dis = torch.nn.DataParallel(self.dis)
            self.dis.transform = self.dis.module.transform
            self.dis.output_layer = self.dis.module.output_layer
            self.gen.inverse_transform = self.gen.module.inverse_transform
            self.gen.z_dim = self.gen.module.z_dim
            self.gen.sample_label = self.gen.module.sample_label

            if hasattr(self.gen.module, "label"):
                self.gen.label = self.gen.module.label
            if hasattr(self.dis.module, "label"):
                self.dis.label = self.dis.module.label

        self.eval()
        self.define_prior()

        self.label = None

    def load_weights(self):
        state_dict = torch.load(
            Path(ROOT_DIR, self.config.generator.ckpt_path), map_location=self.device
        )
        self.gen.load_state_dict(state_dict, strict=True)

        state_dict = torch.load(
            Path(ROOT_DIR, self.config.discriminator.ckpt_path),
            map_location=self.device,
        )
        self.dis.load_state_dict(state_dict, strict=True)

    def eval(self):
        for param in self.gen.parameters():
            param.requires_grad = True
        for param in self.dis.parameters():
            param.requires_grad = True
        self.gen.eval()
        self.dis.eval()

    def get_latent_code_dim(self):
        return self.gen.z_dim

    def define_prior(self):
        if self.config.prior == "normal":
            prior = torch.distributions.multivariate_normal.MultivariateNormal(
                torch.zeros(self.gen.z_dim).to(self.device),
                torch.eye(self.gen.z_dim).to(self.device),
            )
            prior.project = lambda z: z
        elif self.config.prior == "uniform":
            prior = torch.distributions.uniform.Uniform(
                -torch.ones(self.gen.z_dim).to(self.device),
                torch.ones(self.gen.z_dim).to(self.device),
            )
            prior.project = lambda z: torch.clip(z, -1 + 1e-9, 1 - 1e-9)
            prior.log_prob = lambda z: torch.zeros(z.shape[0], device=z.device)
        else:
            raise KeyError
        self.gen.prior = prior

    @property
    def transform(self):
        return self.dis.transform

    @property
    def inverse_transform(self):
        return self.gen.inverse_transform

    @property
    def prior(self):
        return self.gen.prior

    def set_label(self, label):
        self.gen.label = label
        self.dis.label = label


def estimate_lipschitz_const(
    gen: nn.Module,
    dis: nn.Module,
    n_pts: int,
    batch_size: int,
    verbose: bool = False,
) -> float:
    lipschitz_const_est = 0
    if verbose:
        bar = trange
    else:
        bar = range

    for _ in bar(0, n_pts, batch_size):
        z = gen.prior.sample((batch_size,)).requires_grad_(True)
        label = gen.sample_label(batch_size, z.device)
        gen.label = label
        dis.label = label

        x_fake = gen(z)
        dis_fake = dis(x_fake).squeeze()
        energy = gen.prior.log_prob(z) + dis_fake
        grad = torch.autograd.grad(energy.sum(), z)[0]
        grad_norm = torch.norm(grad, dim=1, p=2).sum()
        lipschitz_const_est += grad_norm.item() / n_pts

    return lipschitz_const_est
