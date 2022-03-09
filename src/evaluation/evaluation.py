import copy
import json
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import random
from typing import List, Union

import numpy as np
import wandb
from datasets import set_caching_enabled, Dataset
from omegaconf import DictConfig, open_dict, OmegaConf
from transformers import PreTrainedModel, DataCollatorForSeq2Seq, pipeline, StoppingCriteria, \
    MaxLengthCriteria, StoppingCriteriaList
import torch
import logging
from tqdm import tqdm
from src.config import get_device_from_cfg, load_task_from_cfg, get_config_for_tracking, \
    get_run_base_name_from_cfg

logger = logging.getLogger(__name__)


class EOSStoppingCriteria(StoppingCriteria):
    """Custom `StoppingCriteria` which checks if all generated functions in the batch are completed."""

    def __init__(self, tokenizer):
        self.eos_token = tokenizer.eos_token_id

    def __call__(self, input_ids, scores, **kwargs):
        """Returns true if all generated sequences contain any of the end-of-function strings."""

        return all(self.eos_token in row[1:] for row in input_ids)


def generate_code_predictions(
        model,
        objective,
        pipe,
        dataset: Union[List[dict], Dataset],
        tokenizer,
        batch_size,
        generation_kwargs,
        seq_per_sample,
        remove_input_ids_from_output,
        num_proc=1
):
    logger.info("Starting Generation")

    logger.info(f"Using batch size of {batch_size} and generating "
                f"{seq_per_sample} per sample")

    logger.info("Generation kwargs:")
    for k, v in generation_kwargs.items():
        logger.info(f"\t{k:>20} = {v}")

    generation_kwargs["stopping_criteria"] = StoppingCriteriaList(
        [EOSStoppingCriteria(tokenizer)])
    indices = []
    predictions = []
    labels = []
    generate_steps_per_sample, rem = divmod(seq_per_sample, batch_size)
    if rem > 0:
        logger.error(f"{seq_per_sample}/{batch_size} sequences had a "
                     f"remainder of {rem}")
        raise ValueError(
            "seq_per_sample must be divisible by generation_kwargs.num_return_sequences"
        )

    logger.debug(f"{generate_steps_per_sample} steps per sample")
    generation_kwargs['num_return_sequences'] = batch_size

    max_new_tokens = generation_kwargs.pop('max_new_tokens', 256)
    if objective != 'lm':
        generation_kwargs['max_length'] = generation_kwargs.get('max_length', 256)
        logger.info(
            f"Not in language modeling, using a max length of {generation_kwargs['max_length']}")
        # remove_input_ids_from_output = False
        generation_kwargs.pop('max_new_tokens', None)
    else:

        generation_kwargs['max_new_tokens'] = max_new_tokens
        generation_kwargs.pop('max_length', None)
    num_steps_needed = generate_steps_per_sample * len(dataset)
    logger.info(f"{num_steps_needed} total steps needed")

    def prep_col(ex):
        if objective == 'lm':
            ex['input_sequence'] = tokenizer.eos_token + ex['input_sequence']
        else:
            ex['input_sequence'] = tokenizer.bos_token + ex['input_sequence'] + tokenizer.eos_token
        ex['input_len'] = len(tokenizer.encode(ex['input_sequence']))
        return ex

    logger.info(f"Prepping the dataset")
    dataset = dataset.map(
        prep_col,
        num_proc=num_proc
    )

    model.eval()
    with torch.inference_mode():
        progress_bar = tqdm(total=num_steps_needed, desc='Generating')

        for sample in dataset:

            generated_for_current_sample = []

            for i in range(generate_steps_per_sample):
                generated_from_batch = pipe(sample['input_sequence'], **generation_kwargs)
                if remove_input_ids_from_output:
                    # Can chop all in the results from the generation as they
                    # all have the same prompt length.
                    generated_from_batch = [
                        g['generated_text'][sample['input_len']:] for g in generated_from_batch
                    ]

                generated_for_current_sample.extend(
                    generated_from_batch
                )

                progress_bar.update(1)

            assert len(generated_for_current_sample) == seq_per_sample
            predictions.append(generated_for_current_sample)
            labels.append(sample['target'])
            indices.append(sample['idx'])

        progress_bar.close()

    logger.info("Generating finished.")
    return {
        "indices"    : indices,
        "labels"     : labels,
        "predictions": predictions
    }


def evaluate_model(
        cfg: DictConfig,
        model: PreTrainedModel
):
    """
    Evaluate a model with a reader on a file
    Args:
        cfg (DictConfig): The config to use.
        model (PreTrainedModel): The pretrained huggingface model to use.
    """
    task = load_task_from_cfg(cfg)
    logger.info(f"Reading data from '{cfg['data_path']}'")
    gen_kwargs = OmegaConf.to_object(cfg.get('generation', {}))
    if cfg.objective == 'lm':
        if task.tokenizer.pad_token is None:
            task.tokenizer.pad_token = task.tokenizer.eos_token
        model.config.eos_token_id = task.tokenizer.eos_token_id
        model.config.pad_token_id = task.tokenizer.pad_token_id
        model.config.bos_token_id = task.tokenizer.bos_token_id or task.tokenizer.eos_token

        if cfg.task.name == 'human_eval':
            def prepend_token(sample):
                sample['input_sequence'] = task.tokenizer.eos_token + sample['input_sequence']
                return sample

            task.preprocessors.append(prepend_token)

    if cfg.training.fp16:
        model = model.half()

    logger.info(f"Getting the data for split {cfg.split}")
    dataset = task.preprocess(cfg.split)
    logger.info(f"{len(dataset)} total samples found")
    debug_num_samples = cfg.get('debug_num_samples', None)
    if debug_num_samples is not None:
        logger.warning(f"DEBUG NUMBER OF SAMPLES={debug_num_samples}")
        dataset = dataset.select(list(range(debug_num_samples)))

    pipe = pipeline(
        "text-generation" if cfg.objective == 'lm' else 'text2text-generation',
        model=model,
        tokenizer=task.tokenizer,
        device=cfg.device
    )
    logger.info(f"Model is on {model.device}")
    logger.info(f"{type(dataset)=}")

    generation_results = generate_code_predictions(
        model,
        pipe=pipe,
        objective=cfg.objective,
        dataset=dataset,
        tokenizer=task.tokenizer,
        batch_size=cfg.batch_size,
        generation_kwargs=gen_kwargs,
        seq_per_sample=cfg.seq_per_sample,
        remove_input_ids_from_output=cfg.get("remove_input_ids", False),
        num_proc=cfg.get('num_proc', 1)
    )

    labels = list(map(task.postprocess, generation_results['labels']))
    predictions = list(
        map(lambda pl: list(map(task.postprocess, pl)), generation_results['predictions'])
    )
    indices = generation_results['indices']

    metrics = task.evaluate(predictions, labels)
    # Get the full metrics suite for the predictions and the labels
    logger.info("Results:")
    for k, v in metrics.items():
        logger.info(f"\t{k:>20} = {v:0.3f}")

    serialized_predictions = []
    serialize_generator = task.serialize_predictions(cfg.split, indices, predictions)
    for serialized_dict in tqdm(serialize_generator, total=len(indices), desc="Serializing"):
        serialized_predictions.append(serialized_dict)

    return metrics, serialized_predictions


def evaluate(
        cfg,
        model,
        out_path: Path,
        dry_run: bool,
):
    seed = cfg["seed"]
    logger.debug(f"Setting the seed to {seed}")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    logger.debug(f"Starting eval loop")
    start_time = datetime.utcnow()

    splits_to_use = cfg.splits
    logger.info(f"Using split '{splits_to_use}' for task '{cfg.task.name}'")

    pred_dir = Path(out_path).joinpath('predictions')
    if not pred_dir.exists():
        pred_dir.mkdir()
    all_metrics = {}
    split_paths = []

    set_caching_enabled(not cfg.get('disable_cache', False))

    if not dry_run:
        for split in splits_to_use:
            logger.info(f"Evaluating split {split}")
            with open_dict(cfg):
                cfg.split = split
            metrics, predictions = evaluate_model(
                copy.deepcopy(cfg),
                model=model
            )

            all_metrics.update({f"{split}/{k}": v for k, v in metrics.items()})
            split_path = pred_dir.joinpath(f'{cfg.split}.jsonl')
            split_paths.append(split_path)
            logger.info(f"Saving predictions to '{split_path}'")
            with split_path.open("w", encoding="utf-8") as f:
                for serialized_dict in predictions:
                    f.write(json.dumps(serialized_dict) + '\n')

    end_time = datetime.utcnow() - start_time
    logger.info(f"Total time spent on evaluation: {end_time}")
    all_metrics['runtime'] = str(end_time)
    if not dry_run:
        with out_path.joinpath('eval_metrics.json').open('w', encoding='utf-8') as f:
            json.dump(all_metrics, f)

    run_id = os.getenv('WANDB_RUN_ID')
    with open_dict(cfg):
        cfg.run_id = run_id
        cfg.eval_run_name = os.getenv('WANDB_RUN_NAME')

    with out_path.joinpath(f'eval_config.yaml').open('w') as f:
        f.write(OmegaConf.to_yaml(cfg, resolve=True, sort_keys=True))
    #####################################################################
    # TRACKING CODE TO REMOVE ON RELEASE                                #
    #####################################################################

    if (
            isinstance(cfg.tracking, (dict, DictConfig))
            and int(os.environ.get("LOCAL_RANK", "-1")) <= 0
    ):
        run = wandb.init(
            job_type='evaluate',
            name=os.getenv('WANDB_RUN_NAME'),
            project=os.getenv('WANDB_PROJECT'),
            group=f"{cfg.group}[eval]",
            entity=os.getenv('WANDB_ENTITY'),
            config=get_config_for_tracking(cfg),
            id=run_id
        )

        run.config.update(get_config_for_tracking(cfg))

        if dry_run and out_path.joinpath('eval_metrics.json').exists():
            all_metrics = json.loads(out_path.joinpath('eval_metrics.json').read_text('utf-8'))
            print(all_metrics)
        run.log({f"eval/{k}": v for k, v in all_metrics.items()}, step=1)
        preds_artifact = wandb.Artifact(get_run_base_name_from_cfg(cfg, "preds"),
                                        type='predictions')

        preds_artifact.add_dir(str(pred_dir.resolve().absolute()))
        preds_artifact.add_file(
            str(out_path.joinpath(f'eval_config.yaml').resolve().absolute()))
        run.log_artifact(preds_artifact)
        run.finish()
    logger.info("Finished Evaluation")
