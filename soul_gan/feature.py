from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import torch
import torchvision
from pytorch_fid.inception import InceptionV3
from torch.nn.functional import adaptive_avg_pool2d
from torch.optim import SGD, Adam
from torchvision import transforms

from soul_gan.utils.metrics import batch_inception


# torch.autograd.set_detect_anomaly(True)


class AvgHolder(object):
    cnt: int = 0

    def __init__(self, init_val: Any = 0):
        self.val = init_val

    def upd(self, new_val: Any):
        self.cnt += 1
        alpha = 1.0 / self.cnt
        if isinstance(self.val, list):
            for i in range(len(self.val)):
                self.val[i] = self.val[i] * (1.0 - alpha) + new_val[i] * alpha
        else:
            self.val = self.val * (1.0 - alpha) + new_val * alpha

    def reset(self):
        if isinstance(self.val, list):
            self.val = [0] * len(self.val)
        else:
            self.val = 0
        self.cnt = 0

    @property
    def data(self) -> Any:
        return self.val


class Feature(ABC):
    def __init__(
        self,
        n_features: int = 1,
        callbacks: Optional[List] = None,
        inverse_transform=None,
        device="cuda",
        opt_params: Optional[Dict[str, Any]] = None,
    ):
        self.n_features = n_features
        self.device = device
        self.avg_weight = AvgHolder([0] * n_features)
        self.avg_feature = AvgHolder([0] * n_features)
        self.opt_params = (
            opt_params
            if opt_params is not None
            else {"name": "SGD", "params": {"momentum": 0.0, "nesterov": False}}
        )

        self.callbacks = callbacks if callbacks else []

        if not inverse_transform:
            self.inverse_transform = None  # transforms.Normalize((0, 0, 0), (1, 1, 1))
        else:
            self.inverse_transform = inverse_transform

        self.init_weight()
        self.init_optimizer()

    @classmethod
    def __name__(cls):
        return cls.__name__

    def init_optimizer(self):
        self.weight = [torch.nn.Parameter(w) for w in self.weight]

        if len(self.weight) == 0:
            self.opt = None
        elif self.opt_params["name"] == "Adam":
            self.opt = Adam(self.weight, lr=0.0)
        elif self.opt_params["name"] == "SGD":
            self.opt = SGD(self.weight, **self.opt_params["params"], lr=0.0)
        else:
            raise KeyError

    def init_weight(self):
        self.weight = [0]

    def log_prob(self, out: List[torch.FloatTensor]) -> torch.FloatTensor:
        lik_f = 0
        for feature_id in range(len(out)):
            # lik_f -= (out[feature_id] @ self.weight[feature_id][None, :]).sum(1)
            lik_f -= torch.einsum("ab,b->a", out[feature_id], self.weight[feature_id])

        return lik_f

    def weight_up(
        self, out: List[torch.FloatTensor], step: float, grad_norm: float = 0
    ):
        for i in range(len(self.weight)):
            grad = out[i]

            for group in self.opt.param_groups:
                group["lr"] = np.abs(step)

            if isinstance(grad, torch.Tensor):  # noqa: F632
                self.weight[i].grad = -grad
        if self.opt:
            self.opt.step()

        self.project_weight()

    def reset(self):
        for callback in self.callbacks:
            callback.reset()
        self.avg_weight.reset()
        self.avg_feature.reset()
        self.init_weight()
        self.init_optimizer()

    @staticmethod
    def average_feature(feature_method: Callable) -> Callable:
        # @wraps
        def with_avg(self, *args, **kwargs):
            out = feature_method(self, *args, **kwargs)
            self.avg_feature.upd([x.mean(0) for x in out])
            return out

        return with_avg

    @staticmethod
    def get_useful_info(
        x: torch.FloatTensor,
        feature_out: List[torch.FloatTensor],
        z: Optional[torch.FloatTensor] = None,
    ) -> Dict:
        return {f"feature_{i}": val.mean().item() for i, val in enumerate(feature_out)}

    @staticmethod
    def invoke_callbacks(feature_method: Callable) -> Callable:
        # @wraps
        def with_callbacks(self, x, z: Optional[torch.FloatTensor] = None, **kwargs):
            out = feature_method(self, x, z=z, **kwargs)
            if self.callbacks:
                info = self.get_useful_info(x, out, z)
                for callback in self.callbacks:
                    callback.invoke(info)
            return out

        return with_callbacks

    @abstractmethod
    def __call__(self, x: torch.FloatTensor, z: Optional[torch.FloatTensor] = None):
        raise NotImplementedError

    def project_weight(self):
        for i in range(len(self.weight)):
            self.weight[i].data = torch.clip(self.weight[i].data, -1e5, 1e5)


class FeatureRegistry:
    registry: Dict = {}

    @classmethod
    def register(cls, name: Optional[str] = None) -> Callable:
        def inner_wrapper(wrapped_class: Feature) -> Callable:
            if name is None:
                name_ = wrapped_class.__name__
            else:
                name_ = name
            cls.registry[name_] = wrapped_class
            return wrapped_class

        return inner_wrapper

    @classmethod
    def create_feature(cls, name: str, **kwargs) -> Feature:
        exec_class = cls.registry[name]
        executor = exec_class(**kwargs)
        return executor


class SoulFeature(Feature):
    def __init__(
        self,
        ref_stats_path=None,
        inverse_transform=None,
        callbacks=None,
        device=0,
        ref_score=[torch.zeros(1)],
        **kwargs,
    ):
        self.device = device
        if ref_stats_path and Path(ref_stats_path).exists():
            ref_stats = np.load(Path(ref_stats_path).open("rb"))
            self.ref_feature = [torch.from_numpy(ref_stats["arr_0"]).float()]
        else:
            self.ref_feature = ref_score
            if isinstance(self.ref_feature, torch.Tensor):
                self.ref_feature = [self.ref_feature]
        super().__init__(
            n_features=1,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            opt_params=kwargs.get("opt_params", None),
        )

    def init_weight(self):
        self.weight = [
            torch.zeros(ref_feature.shape[0], dtype=torch.float32).to(self.device)
            for ref_feature in self.ref_feature
        ]

    def get_useful_info(
        self,
        x: torch.FloatTensor,
        feature_out: List[torch.FloatTensor],
        z: Optional[torch.FloatTensor] = None,
    ) -> Dict:
        # print(feature_out[0] + self.ref_feature[0].to(x.device), self.ref_feature[0])
        # print(z)
        return {
            "residual": feature_out[0].mean(0).norm(dim=0).item(),
            "out": (feature_out[0].cpu().mean(0) + self.ref_feature[0]).mean(0).item(),
            "dot_pr": torch.einsum("ab,b->a", feature_out[0], self.weight[0])
            .mean()
            .item(),
            "ref_score": self.ref_feature[0].mean().item(),
            "weight_norm": torch.norm(self.weight[0]).item(),
            "imgs": self.inverse_transform(x).detach().cpu().numpy(),
            "zs": z.detach().cpu(),
        }

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        return NotImplementedError

    @Feature.average_feature
    @Feature.invoke_callbacks
    def __call__(
        self, x: torch.FloatTensor, z: Optional[torch.FloatTensor] = None
    ) -> List[torch.FloatTensor]:
        result = self.apply(x)
        for i, ref in enumerate(self.ref_feature):
            result[i] = result[i] - ref[None, :].to(x.device)
        return result


@FeatureRegistry.register("inception_score")
class InceptionScoreFeature(Feature):
    def __init__(
        self,
        callbacks: Optional[List] = None,
        inverse_transform=None,
        mean=(0.485, 0.456, 0.406),
        std=(0.229, 0.224, 0.225),
        dp=False,
        **kwargs,
    ):
        self.device = kwargs.get("device", 0)
        super().__init__(
            n_features=1,
            callbacks=callbacks,
            inverse_transform=inverse_transform,
        )
        self.model = torchvision.models.inception.inception_v3(
            pretrained=True, transform_input=False
        ).to(self.device)
        if dp:
            self.model = torch.nn.DataParallel(self.model)
        self.model.eval()

        self.transform = transforms.Normalize(mean, std)

        self.ref_feature = kwargs.get("ref_score", [np.log(9.0)])

        self.pis_mean = None  # torch.zeros(1000).to(self.device)
        self.exp_avg_coef = 0.1

    def init_weight(self):
        self.weight = [torch.zeros(1).to(self.device)]

    # @staticmethod
    def get_useful_info(
        self, x: torch.FloatTensor, feature_out: List[torch.FloatTensor]
    ) -> Dict:
        return {
            "inception score": np.exp(
                feature_out[0].mean().item() + self.ref_feature[0]
            ),
            f"weight_{self.__class__.__name__}": torch.norm(self.weight[0]).item(),
            "imgs": self.inverse_transform(x).detach().cpu().numpy(),
        }

    @Feature.average_feature
    @Feature.invoke_callbacks
    def __call__(self, x) -> List[torch.FloatTensor]:
        x = self.inverse_transform(x)
        x = self.transform(x)
        pis = batch_inception(x, self.model, resize=True)

        if self.pis_mean is None:
            self.pis_mean = pis.mean(0).detach()
        else:
            self.pis_mean = (
                1.0 - self.exp_avg_coef
            ) * self.pis_mean + self.exp_avg_coef * pis.mean(0).detach()
        score = (
            (pis * (torch.log(pis) - torch.log(self.pis_mean[None, :])))
            .sum(1)
            .reshape(-1, 1)
        )
        score -= self.ref_feature[0]
        return [score]


@FeatureRegistry.register()
class DiscriminatorFeature(SoulFeature):
    def __init__(
        self, gan, inverse_transform=None, callbacks=None, ref_stats_path=None, **kwargs
    ):
        self.dis = gan.dis
        # ref_feature = kwargs.get("ref_score", 0.5 / (1 - 0.5))
        # if isinstance(ref_feature, float):
        #     ref_feature = [torch.FloatTensor([ref_feature])]
        super().__init__(
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            ref_stats_path=ref_stats_path,
            # ref_score=ref_feature,
        )

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        result = self.dis(x).view(-1, 1)

        # print(result.mean())
        return [result]


@FeatureRegistry.register()
class DiscriminatorFeature_v1(SoulFeature):
    def __init__(
        self, gan, inverse_transform=None, callbacks=None, ref_stats_path=None, **kwargs
    ):
        self.dis = gan.dis
        super().__init__(
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            ref_stats_path=ref_stats_path,
        )

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        self.activation = []

        def get_activation(name):
            def hook(model, input, output):
                self.activation.append(output)

            return hook

        hook = self.dis.module.main[-2].register_forward_hook(get_activation("avgpool"))
        # hook = self.dis.module.lrelu1.register_forward_hook(get_activation("avgpool"))
        self.dis(x)
        hook.remove()
        result = torch.cat([_.to(x.device) for _ in self.activation], 0).view(
            len(x), -1
        )
        result = torch.sigmoid(result)
        self.activation = []

        # print(result.mean())
        return [result]


@FeatureRegistry.register()
class IdentityFeature(SoulFeature):
    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        result = x.reshape(x.shape[0], -1)
        return [result]


@FeatureRegistry.register()
class ClusterFeature(SoulFeature):
    def __init__(
        self,
        clusters_path,
        gan=None,
        ref_stats_path=None,
        inverse_transform=None,
        callbacks=None,
        version: str = "0",
        **kwargs,
    ):
        self.embedding_model = kwargs.get("embedding_model", None)
        self.device = kwargs.get("device", 0)
        clusters_info = np.load(Path(clusters_path).open("rb"))
        self.centroids = torch.from_numpy(clusters_info["centroids"]).float()
        self.sigmas = torch.from_numpy(clusters_info["sigmas"]).float()
        self.priors = torch.from_numpy(clusters_info["priors"]).float()
        self.n_clusters = len(clusters_info["sigmas"])
        #self.eps = torch.from_numpy(clusters_info["eps"]).float()
        self.version = version
        if gan:
            self.dis = gan.dis
        else:
            self.dis = None

        super().__init__(
            ref_stats_path=ref_stats_path,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            **kwargs,
        )
        if self.embedding_model:
            if self.embedding_model == "resnet34":
                model = torchvision.models.resnet34
            elif self.embedding_model == "resnet50":
                model = torchvision.models.resnet50
            elif self.embedding_model == "resnet101":
                model = torchvision.models.resnet101
            else:
                raise ValueError(f"Version {self.resnet_version} is not available")

            self.model = model(pretrained=True).to(self.device)
            self.activation = None

            def get_activation(name):
                def hook(model, input, output):
                    self.activation = output

                return hook

            self.model.avgpool.register_forward_hook(get_activation("avgpool"))
            # if dp:
            #     self.model = torch.nn.DataParallel(self.model)
            self.model.eval()
            self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        else:
            self.model = None

        self.ts = None

        self.h = torch.zeros_like(self.priors)  # N

        if self.version == "2":
            centr = self.centroids.to(self.device)
            theta = (centr.norm(dim=-1) ** 2).mean() / 10.0
            self.kernel = lambda x, y: torch.exp(
                -torch.norm(x - y, dim=-1, p=2) ** 2 / (2 * theta)
            )
            self.centr_self_corr = self.kernel(centr, centr)

        if self.version == "3":
            self.activation = []

            def get_activation(name):
                def hook(model, input, output):
                    self.activation.append(output)

                return hook

            hook = self.dis.layers[-3].register_forward_hook(get_activation("avgpool"))
            self.dis(self.centroids.to(self.device))
            self.embed_centr = torch.cat(
                [_.to(self.device) for _ in self.activation], 0
            ).view(*self.centroids.shape[:-1], -1)
            self.activation = []
            hook.remove()

            theta = (self.embed_centr.norm(dim=-1) ** 2).mean() / 10.0
            self.kernel = lambda x, y: torch.exp(
                -torch.norm(x - y, dim=-1, p=2) ** 2 / (2 * theta)
            )
            self.centr_self_corr = self.kernel(self.embed_centr, self.embed_centr)

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        if self.model:
            self.model(self.transform(self.inverse_transform(x)))
            out = self.activation.to(self.device)
            self.activation = None
            x = out.squeeze(3).squeeze(2)

        if self.version == "0":
            dists = torch.norm(
                x.reshape(x.shape[0], -1)[:, None, :]
                - self.centroids[None, ...].to(x.device),
                dim=-1,
            )
            sigmas = self.sigmas[None, :].to(x.device)
            result = torch.sigmoid(dists - 2 * sigmas)

        elif self.version == "1":
            # #result = dists.min(1)[0].reshape(-1, 1) # - good but not for mode collapse
            # result = torch.exp(-dists ** 2 / (2. * sigmas **2 + 1.)) * self.priors[None, :].to(x.device)
            # #result = result / (result.sum(1)[:, None] + 1e-10) * self.n_clusters # self.n_clusters - constant for stability (helps for some reason)
            # result = torch.log(result + 1e-10)
            # result = dists
            # result[dists < sigmas] = result[dists < sigmas]**2
            # result = torch.exp(-result / sigmas)
            # result = 1 * torch.exp(-(dists / h) ** 2)
            # result = (dists / sigmas * sigmas.mean())[np.arange(len(x)), self.ts].reshape(-1, 1)

            # rff
            # w = torch.randn((len(x), len(self.centroids), self.centroids.shape[-1]), device=x.device)
            # w = torch.randn((len(self.centroids), self.centroids.shape[-1]), device=x.device)
            # prod = (self.w[None, :, :].to(x.device) * (x.reshape(len(x), -1)[:, None, :] - self.centroids[None, :, :].to(x.device))).sum(-1)
            # prod = (self.w[None, :, :].to(x.device) * x.reshape(len(x), -1)[:, None, :]).sum(-1) / (self.w.norm(dim=-1).to(x.device) + 0.1)

            # hyperbolic distance
            # sigmas = self.sigmas[None, :].to(x.device)
            # dists = torch.norm(
            #     x.reshape(x.shape[0], -1)[:, None, :]
            #     - self.centroids[None, ...].to(x.device),
            #     dim=-1, p=1
            # ) / 100
            # # result = torch.exp(dists) # not bad         #torch.exp(-dists / sigmas)
            # sigmas = self.sigmas[None, :, None].to(x.device)
            # result = (torch.exp(-(x.reshape(len(x), -1)[:, None, :] - self.centroids[None, ...].to(x.device))**2) / 2. ).mean(1)
            # result = torch.abs(x.reshape(len(x), -1)[:, None, :] - self.centroids[None, ...].to(x.device)).mean(-1) # not bad

            result = (
                (
                    x.reshape(len(x), -1)[:, None, :]
                    - self.centroids[None, ...].to(x.device)
                )
                ** 2
            ).sum(-1)
            result /= len(self.centroids)

        elif self.version == "2":
            x_flat = x.reshape(len(x), -1)
            centr = self.centroids[None, :, :].to(x.device)
            centr_corr = self.centr_self_corr[None, :].to(x.device)
            ids = np.random.choice(np.arange(len(x)), size=len(x), replace=True)
            result = (
                self.kernel(x_flat, x_flat[ids].detach())[:, None]
                + centr_corr
                - 2.0 * self.kernel(x_flat[:, None, :], centr)
            )

            # result = dists ** 2
            # result = torch.exp(-dists**2 / sigmas **2)
            # result = result.masked_fill(result != result.min(-1)[0][:, None], 0)

            # x_norm = x.reshape(len(x), -1).norm(dim=-1)
            # centr_norm = self.centroids.norm(dim=-1).to(x.device)
            # z = 1. + 2 * dists**2 / (1. - x_norm**2)[:, None] / (1. - centr_norm**2)[None, :]
            # result = torch.arccosh(z)
            # sigmas = self.sigmas[None, :, None].to(x.device)
            # result = ((torch.clip(x.reshape(x.shape[0], -1)[:, None, :] - self.centroids[None, ...].to(x.device), min=2*sigmas) / sigmas)**2).sum(1)
            # result = torch.exp(-(x.reshape(x.shape[0], -1)[:, None, :] - self.centroids[None, ...].to(x.device))**2).mean(1)

        elif self.version == "3":

            def get_activation(name):
                def hook(model, input, output):
                    self.activation.append(output)

                return hook

            hook = self.dis.layers[-3].register_forward_hook(get_activation("avgpool"))
            self.activation = []
            self.dis(x)
            embed_x = torch.cat([_.to(x.device) for _ in self.activation], 0).view(
                len(x), -1
            )
            self.activation = []
            hook.remove()

            ids = np.random.choice(np.arange(len(x)), size=len(x), replace=True)
            result = (
                self.kernel(embed_x, embed_x[ids].detach())[:, None]
                + self.centr_self_corr[None, :].to(x.device)
                - 2.0
                * self.kernel(
                    embed_x[:, None, :], self.embed_centr[None, :, :].to(x.device)
                )
            )

        else:
            raise KeyError

        return [result]


@FeatureRegistry.register()
class MMDFeature(SoulFeature):
    def __init__(
        self,
        gan=None,
        ref_stats_path=None,
        inverse_transform=None,
        callbacks=None,
        **kwargs,
    ):
        self.embedding_model = kwargs.get("embedding_model", None)
        self.device = kwargs.get("device", 0)

        if gan:
            self.dis = gan.dis
        else:
            self.dis = None

        super().__init__(
            ref_stats_path=ref_stats_path,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            **kwargs,
        )
        if self.embedding_model:
            if self.embedding_model == "resnet34":
                model = torchvision.models.resnet34
            elif self.embedding_model == "resnet50":
                model = torchvision.models.resnet50
            elif self.embedding_model == "resnet101":
                model = torchvision.models.resnet101
            else:
                raise ValueError(f"Version {self.resnet_version} is not available")

            self.model = model(pretrained=True).to(self.device)
            self.activation = None

            def get_activation(name):
                def hook(model, input, output):
                    self.activation = output

                return hook

            self.model.avgpool.register_forward_hook(get_activation("avgpool"))
            # if dp:
            #     self.model = torch.nn.DataParallel(self.model)
            self.model.eval()
            self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        else:
            self.model = None

        theta = (self.embed_centr.norm(dim=-1) ** 2).mean() / 10.0
        self.kernel = lambda x, y: torch.exp(
            -torch.norm(x - y, dim=-1, p=2) ** 2 / (2 * theta)
        )
        self.centr_self_corr = self.kernel(self.embed_centr, self.embed_centr)

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        if self.model:
            self.model(self.transform(self.inverse_transform(x)))
            out = self.activation.to(self.device)
            self.activation = None
            x = out.squeeze(3).squeeze(2)

            def get_activation(name):
                def hook(model, input, output):
                    self.activation.append(output)

                return hook

        embed_x = torch.cat([_.to(x.device) for _ in self.activation], 0).view(
            len(x), -1
        )

        ids = np.random.choice(np.arange(len(x)), size=len(x), replace=True)
        result = (
            self.kernel(embed_x, embed_x[ids].detach())[:, None]
            + self.centr_self_corr[None, :].to(x.device)
            - 2.0
            * self.kernel(
                embed_x[:, None, :], self.embed_centr[None, :, :].to(x.device)
            )
        )

        return [result]


@FeatureRegistry.register()
class PCAFeature(SoulFeature):
    def __init__(
        self,
        info_path,
        ref_stats_path=None,
        inverse_transform=None,
        callbacks=None,
        version=0,
        **kwargs,
    ):
        self.embedding_model = kwargs.get("embedding_model", None)
        self.device = kwargs.get("device", 0)
        info = np.load(Path(info_path).open("rb"))
        self.components = torch.from_numpy(info["components"]).float()
        self.mean = torch.from_numpy(info["mean"]).float()
        self.cov_eigs = torch.from_numpy(info["cov_eigs"]).float()
        self.version = version

        super().__init__(
            ref_stats_path=ref_stats_path,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            **kwargs,
        )
        if self.embedding_model:
            if self.embedding_model == "resnet34":
                model = torchvision.models.resnet34
            elif self.embedding_model == "resnet50":
                model = torchvision.models.resnet50
            elif self.embedding_model == "resnet101":
                model = torchvision.models.resnet101
            else:
                raise ValueError(f"Version {self.resnet_version} is not available")

            self.model = model(pretrained=True).to(self.device)
            self.activation = None

            def get_activation(name):
                def hook(model, input, output):
                    self.activation = output

                return hook

            self.model.avgpool.register_forward_hook(get_activation("avgpool"))
            # if dp:
            #     self.model = torch.nn.DataParallel(self.model)
            self.model.eval()
            self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        else:
            self.model = None

        self.ts = None

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        if self.model:
            self.model(self.transform(self.inverse_transform(x)))
            out = self.activation.to(self.device)
            self.activation = None
            x = out.squeeze(3).squeeze(2)

        result = (
            (x.reshape(len(x), -1) - self.mean[None, :].to(x.device))
            @ self.components.to(x.device).T
            / self.cov_eigs.to(x.device)[None, :]
        )
        # result = torch.cat([result, result.norm(dim=-1)[:, None]], 1)
        # result = torch.cat([result, x.reshape(len(x), -1)], 1)

        return [result]


@FeatureRegistry.register()
class KernelPCAFeature(SoulFeature):
    def __init__(
        self,
        info_path,
        ref_stats_path=None,
        inverse_transform=None,
        callbacks=None,
        version=0,
        **kwargs,
    ):
        self.embedding_model = kwargs.get("embedding_model", None)
        self.device = kwargs.get("device", 0)
        info = np.load(Path(info_path).open("rb"))
        self.x = torch.from_numpy(info["x"]).float()
        self.scaled_alphas = torch.from_numpy(info["scaled_alphas"]).float()
        self.gamma = info["gamma"]
        self.version = version

        super().__init__(
            ref_stats_path=ref_stats_path,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            **kwargs,
        )
        if self.embedding_model:
            if self.embedding_model == "resnet34":
                model = torchvision.models.resnet34
            elif self.embedding_model == "resnet50":
                model = torchvision.models.resnet50
            elif self.embedding_model == "resnet101":
                model = torchvision.models.resnet101
            else:
                raise ValueError(f"Version {self.resnet_version} is not available")

            self.model = model(pretrained=True).to(self.device)
            self.activation = None

            def get_activation(name):
                def hook(model, input, output):
                    self.activation = output

                return hook

            self.model.avgpool.register_forward_hook(get_activation("avgpool"))
            # if dp:
            #     self.model = torch.nn.DataParallel(self.model)
            self.model.eval()
            self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
        else:
            self.model = None

        self.ts = None

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        if self.model:
            self.model(self.transform(self.inverse_transform(x)))
            out = self.activation.to(self.device)
            self.activation = None
            x = out.squeeze(3).squeeze(2)
        device = x.device
        K = torch.exp(
            -self.gamma
            * torch.norm(
                x.reshape(len(x), -1)[:, None, :] - self.x[None, ...].to(device), dim=-1
            )
            ** 2
        )
        result = K @ self.scaled_alphas.to(device)

        return [result]


@FeatureRegistry.register()
class DiscriminatorGradientFeature(SoulFeature):
    def __init__(
        self, gan, ref_stats_path=None, inverse_transform=None, callbacks=None, **kwargs
    ):
        self.dis = gan.dis
        super().__init__(
            ref_stats_path=ref_stats_path,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
            **kwargs,
        )

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        # result = self.dis(x).view(-1, 1)
        x.requires_grad_(True)
        result = torch.autograd.grad(self.dis(x).sum(), x, retain_graph=True)[0].view(
            len(x), -1
        )
        return [result]


# @FeatureRegistry.register()
# class IMLEFeature(SoulFeature):
#     def __init__(
#         self,
#         clusters_path,
#         ref_stats_path=None,
#         inverse_transform=None,
#         callbacks=None,
#         **kwargs,
#     ):
#         self.embedding_model = kwargs.get("embedding_model", None)
#         self.device = kwargs.get("device", 0)
#         clusters_info = np.load(Path(clusters_path).open("rb"))
#         self.centroids = torch.from_numpy(clusters_info["centroids"]).float()
#         self.sigmas = torch.from_numpy(clusters_info["sigmas"]).float()
#         self.n_clusters = len(clusters_info["sigmas"])

#         super().__init__(
#             ref_stats_path=ref_stats_path,
#             inverse_transform=inverse_transform,
#             callbacks=callbacks,
#             **kwargs,
#         )
#         if self.embedding_model:
#             if self.embedding_model == "resnet34":
#                 model = torchvision.models.resnet34
#             elif self.embedding_model == "resnet50":
#                 model = torchvision.models.resnet50
#             elif self.embedding_model == "resnet101":
#                 model = torchvision.models.resnet101
#             else:
#                 raise ValueError(f"Version {self.resnet_version} is not available")

#             self.model = model(pretrained=True).to(self.device)
#             self.activation = None

#             def get_activation(name):
#                 def hook(model, input, output):
#                     self.activation = output

#                 return hook

#             self.model.avgpool.register_forward_hook(get_activation("avgpool"))
#             # if dp:
#             #     self.model = torch.nn.DataParallel(self.model)
#             self.model.eval()
#             self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
#         else:
#             self.model = None

#     def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
#         if self.model:
#             self.model(self.transform(self.inverse_transform(x)))
#             out = self.activation.to(self.device)
#             self.activation = None
#             x = out.squeeze(3).squeeze(2)

#         dists = torch.norm(
#             x.reshape(x.shape[0], -1)[:, None, :]
#             - self.centroids[None, ...].to(x.device),
#             dim=-1,
#         )
#         result = torch.exp(-2 * dists ** 2)

#         return [result]


@FeatureRegistry.register()
class InceptionV3MeanFeature(Feature):
    IDX_TO_DIM = {0: 64, 1: 192, 2: 768, 3: 2048}

    def __init__(self, inverse_transform=None, callbacks=None, dp=False, **kwargs):
        self.block_ids = kwargs.get("block_ids", [3])
        self.feature_dims = [self.IDX_TO_DIM[idx] for idx in self.block_ids]
        self.device = kwargs.get("device", 0)
        super().__init__(
            n_features=1,
            inverse_transform=inverse_transform,
            callbacks=callbacks,
        )
        self.data_stat_path = kwargs.get("data_stat_path")

        mean = torch.from_numpy(np.load(Path(self.data_stat_path))["mu"]).to(
            self.device
        )

        self.model = InceptionV3(self.block_ids).to(self.device)
        if dp:
            self.model = torch.nn.DataParallel(self.model)
        self.model.eval()
        self.ref_feature = [mean]

    def init_weight(self):
        self.weight = [
            torch.zeros(dim, device=self.device) for dim in self.feature_dims
        ]

    def get_useful_info(
        self, x: torch.FloatTensor, feature_out: List[torch.FloatTensor]
    ) -> Dict:
        return {
            "feature": feature_out[0].mean().item()
            + self.ref_feature[0].mean().item(),  # noqa: W503
            f"weight_{self.__class__.__name__}": torch.norm(self.weight[0]).item(),
            "imgs": self.inverse_transform(x).detach().cpu().numpy(),
        }

    @Feature.invoke_callbacks
    @Feature.average_feature
    def __call__(self, x) -> List[torch.FloatTensor]:
        x = self.inverse_transform(x)
        pred = self.model(x)[0]

        if pred.size(2) != 1 or pred.size(3) != 1:
            pred = adaptive_avg_pool2d(pred, output_size=(1, 1))

        out = [pred.squeeze(3).squeeze(2)]

        for i in range(len(out)):
            out[i] = (out[i] - self.ref_feature[i][None, :]).float()

        return out


@FeatureRegistry.register()
class DumbFeature(Feature):
    def __init__(
        self,
        n_features: int = 1,
        callbacks: Optional[List] = None,
        inverse_transform=None,
        **kwargs,
    ):
        super().__init__(
            n_features=n_features,
            callbacks=callbacks,
            inverse_transform=inverse_transform,
        )

        self.avg_weight = AvgHolder([] * n_features)
        self.avg_feature = AvgHolder([] * n_features)

        self.ref_feature = []

    def init_weight(self):
        self.weight = []

    def get_useful_info(
        self,
        x: torch.FloatTensor,
        feature_out: List[torch.FloatTensor],
        z: Optional[torch.FloatTensor] = None,
    ) -> Dict:
        return {
            "imgs": self.inverse_transform(x).detach().cpu().numpy(),
            "zs": z.detach().cpu(),
        }

    @Feature.invoke_callbacks
    @Feature.average_feature
    def __call__(
        self, x, z: Optional[torch.FloatTensor] = None
    ) -> List[torch.FloatTensor]:
        return []


@FeatureRegistry.register()
class SumFeature(Feature):
    def __init__(
        self,
        callbacks: Optional[List] = None,
        inverse_transform=None,
        **kwargs,
    ):
        super().__init__(
            n_features=0,
            callbacks=callbacks,
            inverse_transform=inverse_transform,
        )
        self.features = []

        for feature in kwargs["features"]:
            feature_kwargs = feature["params"]
            if "gan" in feature["params"]:
                feature_kwargs["gan"] = kwargs.get("gan")
            feature = FeatureRegistry.create_feature(
                feature["name"],
                inverse_transform=inverse_transform,
                **feature_kwargs,
            )
            self.features.append(feature)
            self.n_features += feature.n_features

        self.avg_feature = AvgHolder([0] * self.n_features)

    @property
    def weight(self):
        weight = []
        for feature in self.features:
            weight.extend(feature.weight)
        return weight

    def init_weight(self):
        for feature in self.features:
            feature.init_weight()

    @Feature.invoke_callbacks
    @Feature.average_feature
    def __call__(self, x: torch.FloatTensor):
        outs = []
        for feature in self.features:
            outs.extend(feature(x))
        return outs

    def get_useful_info(
        self, x: torch.FloatTensor, feature_out: List[torch.FloatTensor]
    ) -> Dict:
        info = {}
        k = 0
        for feature in self.features:
            info.update(
                feature.get_useful_info(x, feature_out[k : k + feature.n_features])
            )
            k += feature.n_features
        return info

    def weight_up(self, out: List[torch.FloatTensor], step: float):
        k = 0
        for feature in self.features:
            feature.weight_up(out[k : k + feature.n_features], step)
            k += feature.n_features


@FeatureRegistry.register()
class EfficientNetFeature(Feature):
    def __init__(self, inverse_transform=None, callbacks=None, dp=False, **kwargs):
        self.data_stat_path = kwargs.get("data_stat_path")
        mean = torch.from_numpy(np.load(Path(self.data_stat_path))["mu"]).to(
            self.device
        )
        self.feature_dim = mean.shape[0]
        self.device = kwargs.get("device", 0)
        super().__init__(
            n_features=1,
            callbacks=callbacks,
            inverse_transform=inverse_transform,
        )
        self.model = torchvision.models.efficientnet_b3(pretrained=True).to(self.device)
        self.activation = None

        def get_activation(name):
            def hook(model, input, output):
                self.activation = output

            return hook

        self.model.avgpool.register_forward_hook(get_activation("avgpool"))
        # if dp:
        #     self.model = torch.nn.DataParallel(self.model)
        self.model.eval()
        self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))

        self.ref_feature = [mean]

    def init_weight(self):
        self.weight = [torch.zeros(self.feature_dim, device=self.device)]

    def get_useful_info(
        self, x: torch.FloatTensor, feature_out: List[torch.FloatTensor]
    ) -> Dict:
        return {
            "feature": feature_out[0].mean().item()
            + self.ref_feature[0].mean().item(),  # noqa: W503
            f"weight_{self.__class__.__name__}": torch.norm(self.weight[0]).item(),
            "imgs": self.inverse_transform(x).detach().cpu().numpy(),
        }

    @Feature.invoke_callbacks
    @Feature.average_feature
    def __call__(self, x) -> List[torch.FloatTensor]:
        x = self.inverse_transform(x)
        x = self.transform(x)
        self.model(x)
        out = self.activation.to(self.device)
        self.activation = None

        out = [out.squeeze(3).squeeze(2)]

        for i in range(len(out)):
            out[i] = (out[i] - self.ref_feature[i][None, :]).float()

        return out


@FeatureRegistry.register()
class ResnetFeature(SoulFeature):
    def __init__(
        self,
        inverse_transform=None,
        ref_stats_path=None,
        callbacks=None,
        dp=False,
        **kwargs,
    ):
        self.resnet_version = kwargs.get("resnet_version", 34)

        super().__init__(
            ref_stats_path=ref_stats_path,
            callbacks=callbacks,
            inverse_transform=inverse_transform,
            **kwargs,
        )
        if self.resnet_version == 18:
            model = torchvision.models.resnet18
        elif self.resnet_version == 34:
            model = torchvision.models.resnet34
        elif self.resnet_version == 50:
            model = torchvision.models.resnet50
        elif self.resnet_version == 101:
            model = torchvision.models.resnet101
        else:
            raise ValueError(f"Version {self.resnet_version} is not available")

        self.model = model(pretrained=True).to(self.device)
        self.activation = None

        def get_activation(name):
            def hook(model, input, output):
                self.activation = output

            return hook

        self.model.avgpool.register_forward_hook(get_activation("avgpool"))
        # if dp:
        #     self.model = torch.nn.DataParallel(self.model)
        self.model.eval()
        self.transform = transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))

    def apply(self, x: torch.FloatTensor) -> List[torch.FloatTensor]:
        x = self.inverse_transform(self.transform(x))
        self.model(x)
        out = self.activation.to(self.device)
        self.activation = None
        out = [out.squeeze(3).squeeze(2)]

        return out
