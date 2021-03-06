import argparse
import sys
from pathlib import Path

import ruamel.yaml
import torch
from torch.utils import data


sys.path.append("studiogan")

from maxent_gan.datasets.utils import get_dataset
from maxent_gan.models.studiogans import StudioDis, StudioGen  # noqa: F401
from maxent_gan.models.utils import GANWrapper, estimate_lipschitz_const
from maxent_gan.utils.general_utils import CONFIGS_DIR, DotConfig  # isort:block


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gan_configs", type=str, nargs="+")
    parser.add_argument("--all", "-a", dest="all", action="store_true")
    parser.add_argument("--except", dest="exc", type=str, nargs="+")
    parser.add_argument("--n_pts_mc", type=int, default=10000)
    parser.add_argument("--n_pts_lipschitz", type=int, default=10000)
    parser.add_argument("--batch_size", type=int, default=500)
    parser.add_argument("--upd_config", action="store_true")

    parser.add_argument("--device", type=int)

    args = parser.parse_args()
    return args


def main(args):
    device = torch.device("cpu" if args.device is None else args.device)

    if args.all:
        config_paths = [
            _.as_posix() for _ in Path(CONFIGS_DIR, "gan_configs").glob("*")
        ]
    else:
        config_paths = args.gan_configs

    if args.exc:
        config_paths = sorted(
            list(
                set(config_paths)
                - set([Path(_).resolve().as_posix() for _ in args.exc])
            )
        )

    for config_path in config_paths:
        config_path = Path(config_path)
        print(f"Config: {config_path.name}")
        raw_config = ruamel.yaml.round_trip_load(config_path.open("r"))
        config = DotConfig(raw_config["gan_config"])

        dataset = get_dataset(
            config.dataset,
            mean=config.train_transform.Normalize.mean,
            std=config.train_transform.Normalize.std,
        )
        dataloader = data.DataLoader(dataset, batch_size=args.batch_size, shuffle=True)

        # for thermalize in [False]:  # True, False]:
        #     print(f"Thermalize: {thermalize}")

        gan = GANWrapper(config, device=device)

        # log_norm_const = estimate_log_norm_constant(
        #     gen,
        #     dis,
        #     args.n_pts_mc,
        #     batch_size=args.batch_size,
        #     verbose=True,
        # )
        # print(f"\t log norm const: {log_norm_const:.3f}")

        # lipschitz_const = estimate_lipschitz_const(
        #     gen,
        #     dis,
        #     args.n_pts_lipschitz,
        #     batch_size=args.batch_size,
        #     verbose=True,
        # )
        # print(f"\t lipschitz const: {lipschitz_const:.3f}")

        n_batches = 100

        fake_score = 0
        for _ in range(0, n_batches):
            z = gan.prior.sample((args.batch_size,))
            fake_score += gan.dis(gan.gen(z)).squeeze().mean().item()
        fake_score /= n_batches

        real_score = 0
        for i, batch in enumerate(dataloader):
            if i == n_batches:
                break
            real_score += gan.dis(batch.to(device)).squeeze().mean().item()
        real_score /= i + 1

        print(f"Fake score: {fake_score}, Real score: {real_score}")

        if args.upd_config:
            # raw_config["gan_config"]["thermalize"][thermalize][
            #     "log_norm_const"
            # ] = log_norm_const
            # raw_config["gan_config"]["thermalize"][thermalize][
            #     "lipschitz_const"
            # ] = lipschitz_const
            raw_config["gan_config"]["fake_score"] = fake_score
            raw_config["gan_config"]["real_score"] = real_score
            raw_config["gan_config"]["mean_score"] = (real_score + fake_score) / 2.0
        ruamel.yaml.round_trip_dump(raw_config, config_path.open("w"))


if __name__ == "__main__":
    args = parse_arguments()
    main(args)
