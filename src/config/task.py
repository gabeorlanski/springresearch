"""
Config related util functions.
"""
import inspect
import os
from typing import List, Dict, Tuple, Callable
import logging
from functools import partial
from transformers import AutoTokenizer
from omegaconf import DictConfig, OmegaConf

from tio import Task, Metric, Preprocessor, Postprocessor

logger = logging.getLogger(__name__)
__all__ = [
    "load_processors_from_cfg",
    "load_task_from_cfg",
    "load_tokenizer_from_cfg"
]


def load_processors_from_cfg(cfg: DictConfig) -> Tuple[List[Callable], List[Callable]]:
    """
    Create the pre- and post- processors from a given config.

    Args:
        cfg (DictConfig): The config to use.

    Returns:
        Tuple[List[Callable], List[Callable]]: The created preprocessors and
            postprocessors.
    """
    logger.info("Loading processors")

    def _create_processors(processor_cls, processor_list):

        return [
            partial(processor_cls.by_name(name), **func_kwargs)
            for name, func_kwargs in map(lambda d: next(iter(d.items())), processor_list)
        ]

    preprocessor_list = list(cfg.get('preprocessors', []))
    postprocessor_list = list(cfg.get('postprocessors', []))

    task_preprocessors = list(cfg.task.get('preprocessors', []))
    task_postprocessors = list(cfg.task.get('postprocessors', []))
    logger.info(f"{len(task_preprocessors)} task preprocessors")
    logger.info(f"{len(task_postprocessors)} task postprocessors")

    model_type_preprocessors = list(cfg.get('model_type', {}).get('preprocessors', []))
    model_type_postprocessors = list(cfg.get('model_type', {}).get('postprocessors', []))
    logger.info(
        f"Found {len(model_type_preprocessors)} preprocessors specific to the model type"
    )
    logger.info(
        f"Found {len(model_type_postprocessors)} postprocessors specific to the model type"
    )
    if cfg.task.get('override_preprocessors', False):
        logger.warning("Overriding preprocessors with task processors.")
        preprocessor_list = task_preprocessors
    else:
        logger.info('Using all preprocessors')
        preprocessor_list = preprocessor_list + task_preprocessors + model_type_preprocessors

    if cfg.task.get('override_postprocessors', False):
        logger.warning("Overriding postprocessors with task postprocessors.")
        postprocessor_list = task_postprocessors
    else:
        postprocessor_list = postprocessor_list + task_postprocessors + model_type_postprocessors

    preprocessors = _create_processors(Preprocessor, preprocessor_list)
    postprocessors = _create_processors(Postprocessor, postprocessor_list)

    logger.info(f"{len(preprocessors)} total preprocessors")
    logger.info(f"{len(postprocessors)} total postprocessors")

    return preprocessors, postprocessors


def load_task_from_cfg(
        cfg: DictConfig,
        tokenizer_kwargs=None
) -> Task:
    """
    Create a Task from a cfg

    Args:
        cfg (DictConfig): The config to use.

    Returns:
        Task: The created task object.
    """
    logger.info(f"Initializing task registered to name '{cfg['task']['name']}'")
    preprocessors, postprocessors = load_processors_from_cfg(cfg)
    logger.info(f"Metrics are {list(cfg.get('metrics', []))}")
    metrics = []
    for metric in cfg.get('metrics'):
        if isinstance(metric, dict):
            metric_name, metric_dict = list(metric.items())
        else:
            metric_name = metric
            metric_dict = {}
        metrics.append(Metric.from_dict(metric_name, metric_dict))

    task_sig = set(inspect.signature(Task).parameters)
    cls_sig = set(inspect.signature(Task.by_name(cfg['task']['name'])).parameters)
    additional_kwargs = {
        k: v for k, v in cfg.task.items()
        if k in cls_sig.difference(task_sig)
    }

    return Task.get_task(
        name=cfg["task"]["name"],
        tokenizer=load_tokenizer_from_cfg(cfg, tokenizer_kwargs),
        preprocessors=preprocessors,
        postprocessors=postprocessors,
        metric_fns=metrics,
        split_mapping=cfg.task.get('split_mapping', {}),
        additional_kwargs=additional_kwargs
    )


def load_tokenizer_from_cfg(cfg, tokenizer_kwargs=None):
    if tokenizer_kwargs is None:
        tokenizer_kwargs = {}
    return AutoTokenizer.from_pretrained(
        cfg['model'],
        use_fast=os.environ.get('DISABLE_FAST_TOK', 'false') != 'true',
        **tokenizer_kwargs
    )
