from pathlib import Path
from typing import Union

import studiogan
import studiogan.config
import studiogan.configs
import studiogan.utils
import studiogan.utils.misc
import torch
from torch.nn import functional as F

from maxent_gan.models.base import BaseDiscriminator, BaseGenerator, ModelRegistry


configs = Path(studiogan.configs.__path__[0])


@ModelRegistry.register()
class StudioGen(BaseGenerator):
    def __init__(self, mean, std, config, label=None):
        super().__init__(mean, std)
        cfg = studiogan.config.Configurations(Path(configs, "CIFAR10", config))
        self.cfg = cfg
        self.n_classes = cfg.DATA.num_classes

        module = __import__(
            "models.{backbone}".format(backbone=cfg.MODEL.backbone),
            fromlist=["something"],
        )

        if cfg.MODEL.backbone == "stylegan2":
            try:  # HACK
                cfg.STYLEGAN = cfg.STYLEGAN2
            except Exception:
                pass
            channel_base, channel_max = (
                32768
                if cfg.MODEL.backbone == "stylegan3"
                or cfg.DATA.img_size >= 512
                or cfg.DATA.name in ["CIFAR10", "CIFAR100"]
                else 16384,
                512,
            )
            gen_c_dim = cfg.DATA.num_classes if cfg.MODEL.g_cond_mtd == "cAdaIN" else 0
            dis_c_dim = (
                cfg.DATA.num_classes
                if cfg.MODEL.d_cond_mtd in cfg.STYLEGAN.cond_type
                else 0
            )
            # if RUN.mixed_precision:
            #     num_fp16_res = 4
            #     conv_clamp = 256
            # else:
            num_fp16_res = 0
            conv_clamp = None

            self.gen = module.Generator(
                z_dim=cfg.MODEL.z_dim,
                c_dim=gen_c_dim,
                w_dim=cfg.MODEL.w_dim,
                img_resolution=cfg.DATA.img_size,
                img_channels=cfg.DATA.img_channels,
                mapping_kwargs={"num_layers": cfg.STYLEGAN.mapping_network},
                synthesis_kwargs={
                    "channel_base": channel_base,
                    "channel_max": channel_max,
                    "num_fp16_res": num_fp16_res,
                    "conv_clamp": conv_clamp,
                },
                # MODEL=cfg.MODEL,
            )

        else:
            self.gen = module.Generator(
                z_dim=cfg.MODEL.z_dim,
                g_shared_dim=cfg.MODEL.g_shared_dim,
                img_size=cfg.DATA.img_size,
                g_conv_dim=cfg.MODEL.g_conv_dim,
                apply_attn=cfg.MODEL.apply_attn,
                attn_g_loc=cfg.MODEL.attn_g_loc,
                g_cond_mtd=cfg.MODEL.g_cond_mtd,
                num_classes=cfg.DATA.num_classes,
                g_init=cfg.MODEL.g_init,
                g_depth=cfg.MODEL.g_depth,
                mixed_precision=False,  # cfg.RUN.mixed_precision,
                MODULES=cfg.MODULES,
                # MODEL=cfg.MODEL,
            )
        self.z_dim = self.gen.z_dim
        self.label = label

    def load_state_dict(self, state_dict, strict: bool = True):
        out = self.gen.load_state_dict(state_dict["state_dict"], strict=strict)

        if False:  # self.cfg.RUN.batch_statistics:
            self.gen.apply(studiogan.utils.misc.set_bn_trainable)
            self.gen.apply(studiogan.utils.misc.untrack_bn_statistics)
        self.gen.apply(studiogan.utils.misc.set_deterministic_op_trainable)

        return out

    def forward(self, x, label=None):
        label = label.to(x.device) if label is not None else self.label.to(x.device)

        if self.cfg.MODEL.backbone.startswith("stylegan"):
            label = F.one_hot(label, num_classes=self.cfg.DATA.num_classes)
        return self.gen.forward(x, label)

    def sample_label(self, batch_size: int, device: Union[int, str]):
        return torch.randint(0, self.n_classes - 1, (batch_size,), device=device)


@ModelRegistry.register()
class StudioDis(BaseDiscriminator):
    def __init__(self, mean, std, output_layer, config, label=None):
        super().__init__(mean, std, output_layer)
        self.config_name = config[: -len(".yaml")]
        cfg = studiogan.config.Configurations(Path(configs, "CIFAR10", config))
        self.n_classes = cfg.DATA.num_classes

        module = __import__(
            "models.{backbone}".format(backbone=cfg.MODEL.backbone),
            fromlist=["something"],
        )

        if cfg.MODEL.backbone == "stylegan2":
            try:  # HACK
                cfg.STYLEGAN = cfg.STYLEGAN2
            except Exception:
                pass
            channel_base, channel_max = (
                32768
                if cfg.MODEL.backbone == "stylegan3"
                or cfg.DATA.img_size >= 512
                or cfg.DATA.name in ["CIFAR10", "CIFAR100"]
                else 16384,
                512,
            )
            gen_c_dim = cfg.DATA.num_classes if cfg.MODEL.g_cond_mtd == "cAdaIN" else 0
            dis_c_dim = (
                cfg.DATA.num_classes
                if cfg.MODEL.d_cond_mtd in cfg.STYLEGAN.cond_type
                else 0
            )
            # if RUN.mixed_precision:
            #     num_fp16_res = 4
            #     conv_clamp = 256
            # else:
            num_fp16_res = 0
            conv_clamp = None

            self.dis = module.Discriminator(
                c_dim=dis_c_dim,
                img_resolution=cfg.DATA.img_size,
                img_channels=cfg.DATA.img_channels,
                architecture=cfg.STYLEGAN.d_architecture,
                channel_base=channel_base,
                channel_max=channel_max,
                num_fp16_res=num_fp16_res,
                conv_clamp=conv_clamp,
                cmap_dim=None,
                d_cond_mtd=cfg.MODEL.d_cond_mtd,
                aux_cls_type=cfg.MODEL.aux_cls_type,
                d_embed_dim=cfg.MODEL.d_embed_dim,
                num_classes=cfg.DATA.num_classes,
                normalize_d_embed=cfg.MODEL.normalize_d_embed,
                block_kwargs={},
                mapping_kwargs={},
                epilogue_kwargs={
                    "mbstd_group_size": cfg.STYLEGAN.d_epilogue_mbstd_group_size
                },
                # MODEL=cfg.MODEL,
            )

        else:
            self.dis = module.Discriminator(
                img_size=cfg.DATA.img_size,
                d_conv_dim=cfg.MODEL.d_conv_dim,
                apply_d_sn=cfg.MODEL.apply_d_sn,
                apply_attn=cfg.MODEL.apply_attn,
                attn_d_loc=cfg.MODEL.attn_d_loc,
                d_cond_mtd=cfg.MODEL.d_cond_mtd,
                aux_cls_type=cfg.MODEL.aux_cls_type,
                d_embed_dim=cfg.MODEL.d_embed_dim,
                num_classes=cfg.DATA.num_classes,
                normalize_d_embed=cfg.MODEL.normalize_d_embed,
                d_init=cfg.MODEL.d_init,
                d_depth=cfg.MODEL.d_depth,
                mixed_precision=False,  # cfg.RUN.mixed_precision,
                MODULES=cfg.MODULES,
                # MODEL=cfg.MODEL,
            )
        self.label = label

    def load_state_dict(self, state_dict, strict: bool = True):
        if self.config_name == "DCGAN":
            self.dis.conv = self.dis.conv1
            del self.dis.conv1
            self.dis.bn = self.dis.bn1
            del self.dis.bn1

        out = self.dis.load_state_dict(state_dict["state_dict"], strict=strict)

        if self.config_name == "DCGAN":
            self.dis.conv1 = self.dis.conv
            del self.dis.conv
            self.dis.bn1 = self.dis.bn
            del self.dis.bn

        return out

    def forward(self, x, label=None):
        label = label.to(x.device) if label is not None else self.label.to(x.device)
        return self.dis.forward(x, label)["adv_output"]
