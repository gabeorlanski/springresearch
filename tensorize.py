import logging
from typing import Optional, List

import transformers.utils.logging
from hydra import compose, initialize
import yaml
from omegaconf import OmegaConf, open_dict
import os
import click

from src.common import setup_global_logging, PROJECT_ROOT
from src.data.tensorize import tensorize
from src.data.stackoverflow import StackOverflowProcessor


@click.command()
@click.argument('name', metavar="<Name of this dataset>")
@click.argument('output_name', metavar="<Name of the output file>")
@click.argument('processor_name', metavar='<Processor to use>')
@click.argument('model_name', metavar='<Model to use>')
@click.argument('num_workers', type=int, metavar='<Number Of Workers>')
@click.option(
    '--debug',
    is_flag=True,
    default=False,
    help="Debug Mode"
)
@click.option(
    '--data-path', '-I',
    help='Path to the input data',
    default='data/dumps'
)
@click.option(
    '--out-path', '-O',
    help='Path for saving the data',
    default='data/tensorized'
)
@click.option(
    '--validation-file-name', '-val', default=None,
    help='Name of the validation raw data, if not provided, will use the {name}_val')
@click.option(
    '--config', 'config_file', default=None, help='Path to config file.')
@click.option(
    '--debug-samples', 'debug_samples', default=-1, help='Debug Samples to use.')
@click.option(
    '--override-str',
    help='Bash does not like lists of variable args. so pass as seperated list of overrides, seperated by spaces.',
    default=''
)
def tensorize_data(
        name: str,
        output_name: str,
        processor_name: str,
        model_name: str,
        num_workers: int,
        override_str: str,
        debug: bool,
        data_path,
        validation_file_name,
        config_file,
        out_path,
        debug_samples
):
    if config_file is None:
        override_list = [
            f"name={name}",
            f"processor={processor_name}"
        ]
        override_list.extend(override_str.split(' ') if override_str else [])
        initialize(config_path="conf", job_name="train")
        cfg = compose(config_name="tensorize", overrides=override_list)
    else:
        cfg = OmegaConf.create(yaml.load(
            PROJECT_ROOT.joinpath(config_file).open(),
            yaml.Loader
        ))
        with open_dict(cfg):
            cfg.name = name

    setup_global_logging(
        f'{output_name}_tensorize',
        PROJECT_ROOT.joinpath('logs'),
        rank=int(os.environ.get('LOCAL_RANK', '-1')),
        world_size=int(os.environ.get("WORLD_SIZE", 1)),
        debug=debug
    )
    transformers.utils.logging.set_verbosity_error()
    logger = logging.getLogger(f'{name}_tensorize')
    logger.info(f"Starting tensorize of {name}")
    logger.info(f"Using processor {processor_name}")
    logger.info(f"Using model {model_name}")
    logger.debug(f"Override string is {override_str}")
    logger.debug(f"Using {num_workers} workers")

    data_path = PROJECT_ROOT.joinpath(data_path)
    logger.info(f"Data path is {data_path}")
    out_path = PROJECT_ROOT.joinpath(out_path)
    logger.info(f"Output path is {out_path}")

    train_file_name = f"{name}.jsonl"
    validation_file = f"{validation_file_name or name + '_val'}.jsonl"

    if not out_path.exists():
        out_path.mkdir(parents=True)

    logger.debug(f"Initializing processor {cfg.processor.name}")

    logger.info("Processor arguments:")
    for k, v in cfg.processor.params.items():
        logger.info(f"{k:>32} = {v}")
    if cfg.processor.name == 'stackoverflow':
        processor = StackOverflowProcessor(
            **OmegaConf.to_object(cfg.processor.params)
        )
    else:
        raise ValueError(f'Unknown processor {cfg.processor.name}')
    tensorize(
        data_path.joinpath(train_file_name),
        out_path,
        output_name,
        num_workers,
        model_name,
        processor,
        cfg.tensorize_batch_size,
        debug_max_samples=debug_samples
    )
    tensorize(
        data_path.joinpath(validation_file),
        out_path,
        f"{output_name}.val",
        num_workers,
        model_name,
        processor,
        cfg.tensorize_batch_size,
        debug_max_samples=debug_samples
    )


if __name__ == "__main__":
    tensorize_data()
